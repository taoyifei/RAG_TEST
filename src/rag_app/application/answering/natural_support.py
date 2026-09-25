"""自然答复的一次批量支持检查与确定性发布。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from rag_app.application.answering.natural_answer import (
    NaturalAnswerPort,
    NaturalMessage,
    NaturalReference,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import ProviderCall, SourceSpan
from rag_app.core.models.chunk import SourceSpanKind
from rag_app.core.ports import CancellationPort
from rag_app.core.tokenization import estimate_tokens

SUPPORT_REVIEW_REVISION = "natural-support-cq1-v1"
_CITATION = re.compile(r"\[S[1-9][0-9]*\]")
_HANDLE = re.compile(r"c[1-9][0-9]*\Z")
_LIST_ITEM = re.compile(r"^\s*(?:[-*]|[1-9][0-9]*[.)、])\s+(?P<body>\S.*)$")
_DEPENDENT_ITEM = re.compile(r"^(?:其中|上述|该|这些|此外|它|其)[，,：: ]")
_DEPENDENT_PARAGRAPH = re.compile(
    r"^(?:其中|上述|这些|此外|对此|基于以上|因此|所以|该|其|它)"
)
_MAX_REVIEW_RESPONSE_CHARS = 24000
_MAX_QUOTE_CHARS = 1000
_MAX_EVIDENCE_PER_UNIT = 8
_MESSAGE_OVERHEAD = 16
_MIN_TABLE_ROWS = 2
_SYSTEM = (
    "你是企业知识问答的证据核查员。只能使用本次 sources，"
    "逐个核查完整答复单位是否回答本题，"
    "以及单位内的每个事实是否被原文直接支持。特别检查主体是否适用、条件及例外、否定、"
    "金额和比例、工作日和自然日、起算点、模板示例与正式要求、复合结论的每一部分。"
    "一般职责不能证明未提及的具体项目归属。只输出一个 JSON 对象："
    '{"coverage":"complete","units":[{"unit_id":"u1","verdict":"supported",'
    '"evidence":[{"source_handle":"c1","quote":"原文逐字摘录"}],'
    '"reason_code":"DIRECT_SUPPORT"}]}。'
    "verdict 仅可为 supported、unsupported、contradicted、unclear。"
    "一个单位有任何无法核实的事实就不能标 supported。"
    "quote 必须从对应 source 原样复制，并足以包含条件、表头和目标行；"
    "重复文字请提供更长的唯一摘录。不输出分析过程。"
    "顶层另输出 coverage，取 complete、partial 或 none；"
    "只有全部子问都有依据才选 complete。"
)


@dataclass(frozen=True, slots=True)
class SupportPassage:
    """已实际送给草稿模型的来源正文。"""

    reference: NaturalReference
    text: str


@dataclass(frozen=True, slots=True)
class AnswerUnit:
    """草稿中不可再拆的完整段落及原始字符范围。"""

    unit_id: str
    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class SupportPublication:
    """不依赖第二次改写的最终发布决定。"""

    status: Literal[
        "GROUNDED_ANSWER",
        "GROUNDED_PARTIAL",
        "INSUFFICIENT_EVIDENCE",
        "EXECUTION_ERROR",
    ]
    answer: str | None
    references: tuple[NaturalReference, ...]
    reason_code: str
    packet_sha256: str
    reviewed_units: int
    supported_units: int
    provider_calls: tuple[ProviderCall, ...] = ()
    review_model: str | None = None
    review_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def split_answer_units(draft: str) -> tuple[AnswerUnit, ...]:
    """按完整段落切分；代码块和表格整体审核，避免丢失条件。"""
    segments: tuple[tuple[int, int], ...]
    if "```" in draft or "~~~" in draft or re.search(r"(?m)^\s*\|", draft):
        segments = ((0, len(draft)),)
    else:
        boundaries = tuple(re.finditer(r"\n\s*\n", draft))
        starts = (0, *(match.end() for match in boundaries))
        ends = (*(match.start() for match in boundaries), len(draft))
        segments = tuple(zip(starts, ends, strict=True))
    units: list[AnswerUnit] = []
    for start, end in segments:
        raw = draft[start:end]
        text = raw.strip()
        if not text:
            continue
        lines = text.splitlines()
        list_items = tuple(_LIST_ITEM.match(line) for line in lines)
        if (
            len(lines) > 1
            and all(list_items)
            and not any(
                _DEPENDENT_ITEM.match(match["body"])
                for match in list_items
                if match is not None
            )
        ):
            cursor = start
            for line in lines:
                line_start = draft.find(line, cursor, end)
                units.append(
                    AnswerUnit(
                        f"u{len(units) + 1}",
                        line,
                        line_start,
                        line_start + len(line),
                    )
                )
                cursor = line_start + len(line)
        else:
            offset = raw.index(text)
            units.append(
                AnswerUnit(
                    f"u{len(units) + 1}",
                    text,
                    start + offset,
                    start + offset + len(text),
                )
            )
    return tuple(units)


def review_and_publish(  # noqa: PLR0913
    *,
    question: str,
    draft: str,
    passages: tuple[SupportPassage, ...],
    source_scope_digest: str,
    index_revision_id: str,
    model: NaturalAnswerPort,
    source_identities: tuple[tuple[str, str], ...],
    cancellation: CancellationPort,
    input_limit: int,
) -> SupportPublication:
    """一次模型审核后检查所有句柄、逐字摘录和来源位置。"""
    units = split_answer_units(draft)
    packet_sha256 = canonical_sha256(
        {
            "revision": SUPPORT_REVIEW_REVISION,
            "question": question,
            "scope": source_scope_digest,
            "index_revision": index_revision_id,
            "draft": draft,
            "passages": tuple(
                (
                    passage.reference.alias,
                    passage.reference.document_id,
                    passage.reference.document_version_id,
                    passage.text,
                    tuple(
                        span.model_dump(mode="json")
                        for span in passage.reference.source_spans
                    ),
                )
                for passage in passages
            ),
        }
    )
    if not units:
        return _failure("SUPPORT_REVIEW_EMPTY_DRAFT", packet_sha256)
    handles = {f"c{index}": item for index, item in enumerate(passages, 1)}
    sources = [
        {"source_handle": handle, "text": passage.text}
        for handle, passage in handles.items()
    ]
    messages = (
        NaturalMessage(role="system", content=_SYSTEM),
        NaturalMessage(
            role="user",
            content=json.dumps(
                {
                    "question": question,
                    "scope_digest": source_scope_digest,
                    "units": [
                        {"unit_id": unit.unit_id, "text": unit.text}
                        for unit in units
                    ],
                    "sources": sources,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    )
    estimated_input = sum(
        estimate_tokens(item.content) + _MESSAGE_OVERHEAD for item in messages
    )
    if estimated_input > input_limit:
        return _failure("SUPPORT_REVIEW_BUDGET_EXCEEDED", packet_sha256)
    started = time.monotonic()
    completion = model.complete_natural(
        messages,
        source_identities=source_identities,
        cancellation=cancellation,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if completion.finish_reason != "stop":
        return _failure(
            "SUPPORT_REVIEW_TRUNCATED",
            packet_sha256,
            calls=completion.provider_calls,
            model=completion.model,
            review_ms=elapsed_ms,
        )
    review = _parse_verdicts(completion.text, units)
    if review is None:
        return _failure(
            "SUPPORT_REVIEW_FORMAT_INVALID",
            packet_sha256,
            calls=completion.provider_calls,
            model=completion.model,
            review_ms=elapsed_ms,
        )
    coverage, verdicts = review
    references: list[NaturalReference] = []
    published: list[str] = []
    previous_published = False
    for unit in units:
        verdict = verdicts[unit.unit_id]
        if verdict["verdict"] != "supported" or (
            unit.unit_id != "u1"
            and _DEPENDENT_PARAGRAPH.match(unit.text)
            and not previous_published
        ):
            previous_published = False
            continue
        approved = _approve_evidence(unit, verdict["evidence"], handles)
        if not approved:
            previous_published = False
            continue
        aliases: list[str] = []
        for reference in approved:
            alias = f"S{len(references) + 1}"
            references.append(
                reference.model_copy(
                    update={"alias": alias, "answer_unit_id": unit.unit_id}
                )
            )
            aliases.append(f"[{alias}]")
        content = _CITATION.sub("", unit.text).rstrip()
        published.append(f"{content}{''.join(aliases)}")
        previous_published = True
    if not published or coverage == "none":
        return SupportPublication(
            "INSUFFICIENT_EVIDENCE",
            None,
            (),
            "SUPPORT_REVIEW_NO_VERIFIED_UNITS",
            packet_sha256,
            len(units),
            0,
            completion.provider_calls,
            completion.model,
            elapsed_ms,
            completion.prompt_tokens,
            completion.completion_tokens,
        )
    complete = len(published) == len(units) and coverage == "complete"
    answer = "\n\n".join(published)
    if not complete:
        answer += "\n\n其余内容本轮尚未形成可核对的答案。"
    return SupportPublication(
        "GROUNDED_ANSWER" if complete else "GROUNDED_PARTIAL",
        answer,
        tuple(references),
        "SUPPORT_REVIEW_COMPLETE" if complete else "SUPPORT_REVIEW_PARTIAL",
        packet_sha256,
        len(units),
        len(published),
        completion.provider_calls,
        completion.model,
        elapsed_ms,
        completion.prompt_tokens,
        completion.completion_tokens,
    )


def _parse_verdicts(  # noqa: PLR0911, PLR0912
    content: str, units: tuple[AnswerUnit, ...]
) -> tuple[str, dict[str, dict[str, object]]] | None:
    """本地校验单位完整性、枚举和基础 JSON 类型。"""
    if len(content) > _MAX_REVIEW_RESPONSE_CHARS:
        return None
    try:
        parsed = json.loads(content.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"coverage", "units"}:
        return None
    coverage = parsed["coverage"]
    if not isinstance(coverage, str) or coverage not in {
        "complete",
        "partial",
        "none",
    }:
        return None
    rows = parsed["units"]
    if not isinstance(rows, list) or len(rows) != len(units):
        return None
    expected = {unit.unit_id for unit in units}
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "unit_id",
            "verdict",
            "evidence",
            "reason_code",
        }:
            return None
        unit_id = row["unit_id"]
        if (
            not isinstance(unit_id, str)
            or unit_id not in expected
            or unit_id in result
        ):
            return None
        if not isinstance(row["verdict"], str) or row["verdict"] not in {
            "supported",
            "unsupported",
            "contradicted",
            "unclear",
        }:
            return None
        evidence = row["evidence"]
        if (
            not isinstance(evidence, list)
            or len(evidence) > _MAX_EVIDENCE_PER_UNIT
        ):
            return None
        if row["verdict"] == "supported" and not evidence:
            return None
        if not isinstance(row["reason_code"], str) or not re.fullmatch(
            r"[A-Z_]{1,64}", row["reason_code"]
        ):
            return None
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {
                "source_handle",
                "quote",
            }:
                return None
            if not isinstance(
                item["source_handle"], str
            ) or not _HANDLE.fullmatch(item["source_handle"]):
                return None
            if not isinstance(item["quote"], str) or not (
                1 <= len(item["quote"]) <= _MAX_QUOTE_CHARS
            ):
                return None
        result[unit_id] = row
    return coverage, result


def _approve_evidence(  # noqa: PLR0911
    unit: AnswerUnit,
    evidence: object,
    handles: dict[str, SupportPassage],
) -> tuple[NaturalReference, ...]:
    if not isinstance(evidence, list):
        return ()
    cited = frozenset(_CITATION.findall(unit.text))
    approved: list[NaturalReference] = []
    for item in evidence:
        if not isinstance(item, dict):
            return ()
        handle = item.get("source_handle")
        quote = item.get("quote")
        passage = handles.get(handle) if isinstance(handle, str) else None
        if passage is None or not isinstance(quote, str):
            return ()
        if cited and f"[{passage.reference.alias}]" not in cited:
            return ()
        positions = _exact_positions(passage.text, quote)
        if len(positions) != 1:
            return ()
        start = positions[0]
        end = start + len(quote)
        spans = _mapped_spans(passage, start, end)
        if not spans:
            return ()
        approved.append(
            passage.reference.model_copy(
                update={
                    "source_spans": spans,
                    "excerpt": quote,
                    "support_start_char": start,
                    "support_end_char": end,
                }
            )
        )
    return tuple(approved)


def _mapped_spans(  # noqa: PLR0911
    passage: SupportPassage, start: int, end: int
) -> tuple[SourceSpan, ...]:
    """要求摘录覆盖的每段正文都有可引用映射，表格含完整关系。"""
    selected = tuple(
        sorted(
            (
                span
                for span in passage.reference.source_spans
                if span.chunk_start_char < end and start < span.chunk_end_char
            ),
            key=lambda span: span.chunk_start_char,
        )
    )
    if not selected or any(not span.is_citable for span in selected):
        return ()
    if (
        start < selected[0].chunk_start_char
        or end > selected[-1].chunk_end_char
    ):
        return ()
    for previous, current in pairwise(selected):
        if previous.chunk_end_char > current.chunk_start_char:
            return ()
        gap = passage.text[previous.chunk_end_char : current.chunk_start_char]
        if gap.strip(" \t\r\n|:：,，;；-"):
            return ()
    table_paths = tuple(
        path
        for span in selected
        for path in span.structural_path
        if path.startswith(("tbl:", "table:"))
    )
    if table_paths:
        if len(table_paths) != len(selected) or len(set(table_paths)) != 1:
            return ()
        rows = {
            path
            for span in selected
            for path in span.structural_path
            if path.startswith(("tr:", "row:"))
        }
        # 单行摘录只能接受自带字段名的完整结构行，不能只引单元格值。
        if len(rows) < _MIN_TABLE_ROWS and (
            len(selected) != 1
            or passage.text[
                selected[0].chunk_start_char : selected[0].chunk_end_char
            ]
            != passage.text[start:end]
            or not re.search(r"[:：|]", passage.text[start:end])
        ):
            return ()
    return tuple(
        _narrow_span(
            span,
            max(start, span.chunk_start_char),
            min(end, span.chunk_end_char),
        )
        for span in selected
    )


def _exact_positions(text: str, quote: str) -> tuple[int, ...]:
    """包含重叠位置；重复原文不能靠首次出现猜定原件位置。"""
    positions: list[int] = []
    start = 0
    while (found := text.find(quote, start)) >= 0:
        positions.append(found)
        start = found + 1
    return tuple(positions)


def _narrow_span(span: SourceSpan, start: int, end: int) -> SourceSpan:
    """只在一比一映射时收窄原文坐标，其他映射保留原跨度。"""
    source_start = span.source_start_char
    source_end = span.source_end_char
    if (
        span.span_type
        not in {
            SourceSpanKind.ORIGINAL_TEXT,
            SourceSpanKind.REPEATED_CONTEXT,
        }
        or source_start is None
        or source_end is None
        or source_end - source_start
        != span.chunk_end_char - span.chunk_start_char
    ):
        return span
    offset = start - span.chunk_start_char
    return SourceSpan.model_validate(
        {
            **span.model_dump(mode="python"),
            "chunk_start_char": start,
            "chunk_end_char": end,
            "source_start_char": source_start + offset,
            "source_end_char": source_start + offset + end - start,
        }
    )


def _failure(
    code: str,
    packet_sha256: str,
    *,
    calls: tuple[ProviderCall, ...] = (),
    model: str | None = None,
    review_ms: int = 0,
) -> SupportPublication:
    return SupportPublication(
        "EXECUTION_ERROR",
        None,
        (),
        code,
        packet_sha256,
        0,
        0,
        calls,
        model,
        review_ms,
    )


__all__ = [
    "SUPPORT_REVIEW_REVISION",
    "AnswerUnit",
    "SupportPassage",
    "SupportPublication",
    "review_and_publish",
    "split_answer_units",
]
