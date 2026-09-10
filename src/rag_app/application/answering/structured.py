"""基于最小充分支持集的确定性结构化回答渲染。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_app.core.errors import ValidationFailed
from rag_app.core.models import (
    EvidenceItem,
    QueryAnalysis,
    RequestedAnswerType,
)
from rag_app.core.models.chunk import SourceSpanKind

_TABLE_ROW = re.compile(r"^tr:(\d+)$")
_TABLE_COLUMN = re.compile(r"^tc:(\d+)$")
_DANGLING_LEAD_IN = re.compile(
    r"(?:具体如下|包括以下(?:内容|事项|步骤)?|包括|包含|分为|如下)\s*[：:]?\s*$"
)
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class RenderedAnswer:
    """已经过确定性重算校验的回答与实际引用集合。"""

    text: str
    published_support_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RenderedLine:
    """一个仅由显式支持片段及其结构标签组成的回答行。"""

    text: str
    support_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Cell:
    """同一逻辑表格单元格的完整、有序来源片段。"""

    row: int
    column: int
    text: str
    support_ids: tuple[str, ...]


class DeterministicAnswerRenderer:
    """只消费经 Evidence 门确认完整的支持集，不补充外部事实。"""

    def render(
        self,
        analysis: QueryAnalysis,
        evidence: tuple[EvidenceItem, ...],
    ) -> RenderedAnswer:
        """按所问答案类型渲染并重算验证最终引用。

        Args:
            analysis: 检索、Evidence 与回答共同消费的最终查询分析。
            evidence: 已闭合且通过置信门的最小充分支持集。

        Returns:
            只引用本次支持集的结构化回答。

        Raises:
            ValidationFailed: 支持集不可发布、结构不完整或渲染结果不一致。

        """
        _validate_publishable_evidence(evidence)
        lines = _render_lines(analysis, evidence)
        if not lines:
            raise ValidationFailed(
                "最小支持集无法形成完整结构化回答。",
                stage="answer.render",
                code="STRUCTURED_RENDER_EMPTY",
            )
        rendered = _materialize(lines)
        validate_rendered_answer(rendered, analysis, evidence)
        return rendered


def validate_rendered_answer(
    rendered: RenderedAnswer,
    analysis: QueryAnalysis,
    evidence: tuple[EvidenceItem, ...],
) -> None:
    """通过纯函数重算验证 renderer 没有引入支持集外内容。

    Args:
        rendered: 待发布的结构化回答。
        analysis: 形成回答时的最终查询分析。
        evidence: 本次最小充分支持集。

    Returns:
        无返回值；通过后回答才可发布。

    Raises:
        ValidationFailed: 文本、引用或来源结构与确定性重算不一致。

    """
    _validate_publishable_evidence(evidence)
    expected = _materialize(_render_lines(analysis, evidence))
    known = {item.support_id for item in evidence}
    if (
        rendered != expected
        or not rendered.text.strip()
        or not rendered.published_support_ids
        or any(item not in known for item in rendered.published_support_ids)
    ):
        raise ValidationFailed(
            "结构化回答与最小支持集的确定性投影不一致。",
            stage="answer.validate",
            code="STRUCTURED_RENDER_VALIDATION_FAILED",
        )


def _validate_publishable_evidence(
    evidence: tuple[EvidenceItem, ...],
) -> None:
    if not evidence:
        raise ValidationFailed(
            "结构化回答缺少最小支持集。",
            stage="answer.render",
            code="STRUCTURED_SUPPORT_EMPTY",
        )
    for item in evidence:
        support = dict(item.metadata).get("answer_support")
        if (
            not item.publishable
            or not item.source_spans
            or any(
                not span.is_citable
                or span.span_type is SourceSpanKind.SEPARATOR
                for span in item.source_spans
            )
            or not isinstance(support, dict)
            or support.get("status") != "SUPPORTED"
        ):
            raise ValidationFailed(
                "结构化回答只能使用可发布的直接支持证据。",
                stage="answer.render",
                code="STRUCTURED_SUPPORT_INVALID",
            )


def _render_lines(
    analysis: QueryAnalysis,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[_RenderedLine, ...]:
    semantics = analysis.semantics
    answer_type = semantics.answer_type
    if answer_type is RequestedAnswerType.DEFINITION:
        return _render_definition(analysis, evidence)
    if answer_type is RequestedAnswerType.DUTIES:
        return _render_duties(analysis, evidence)
    if answer_type is RequestedAnswerType.RESPONSIBLE_PARTY:
        return _render_responsible_party(analysis, evidence)
    if answer_type in {
        RequestedAnswerType.ENUMERATION,
        RequestedAnswerType.COUNT,
        RequestedAnswerType.ORDINAL_ITEM,
        RequestedAnswerType.PROCEDURE,
    }:
        return _render_sequence(analysis, evidence)
    if answer_type in {
        RequestedAnswerType.PURPOSE,
        RequestedAnswerType.SECTION_SUMMARY,
    }:
        return _render_sections(analysis, evidence)
    return tuple(
        _RenderedLine(item.citation_text, (item.support_id,))
        for item in evidence
        if item.citation_text.strip()
    )


def _render_definition(
    analysis: QueryAnalysis,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[_RenderedLine, ...]:
    cells = _table_cells(evidence)
    target = _label(analysis.semantics.target or "定义")
    if not cells:
        return tuple(
            _RenderedLine(f"{target}：{item.citation_text}", (item.support_id,))
            for item in evidence
            if item.citation_text.strip()
        )
    target_cell = next(
        (
            cell
            for cell in cells
            if cell.column == 0
            and _normalized_label(cell.text) == _normalized_label(target)
        ),
        None,
    )
    if target_cell is None:
        return _render_table_fallback(target, cells)
    values = tuple(
        cell
        for cell in cells
        if cell.row == target_cell.row and cell.column > 0
    )
    if not values:
        return ()
    headers = {
        cell.column: cell for cell in cells if cell.row == 0 and cell.column > 0
    }
    lines: list[_RenderedLine] = []
    for value in values:
        header = headers.get(value.column)
        label = f"{header.text}：" if header is not None else ""
        ids = tuple(
            dict.fromkeys(
                (
                    *target_cell.support_ids,
                    *(header.support_ids if header is not None else ()),
                    *value.support_ids,
                )
            )
        )
        lines.append(_RenderedLine(f"{target}｜{label}{value.text}", ids))
    return tuple(lines)


def _render_table_fallback(
    target: str, cells: tuple[_Cell, ...]
) -> tuple[_RenderedLine, ...]:
    values = tuple(cell for cell in cells if cell.row > 0)
    return tuple(
        _RenderedLine(f"{target}：{cell.text}", cell.support_ids)
        for cell in values
        if cell.text.strip()
    )


def _render_duties(
    analysis: QueryAnalysis,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[_RenderedLine, ...]:
    target = _label(analysis.semantics.target or "职责")
    subject = tuple(
        item
        for item in evidence
        if _normalized_label(item.citation_text) == _normalized_label(target)
    )
    subject_ids = tuple(item.support_id for item in subject)
    duties = tuple(item for item in evidence if item not in subject)
    if not duties:
        duties = evidence
        subject_ids = ()
    return tuple(
        _RenderedLine(
            f"{target}｜{index}. {item.citation_text}",
            tuple(dict.fromkeys((*subject_ids, item.support_id))),
        )
        for index, item in enumerate(duties, 1)
        if item.citation_text.strip()
    )


def _render_responsible_party(
    analysis: QueryAnalysis,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[_RenderedLine, ...]:
    target = _label(analysis.semantics.target or "该事项")
    return tuple(
        _RenderedLine(
            f"{target}｜责任角色：{item.citation_text}",
            (item.support_id,),
        )
        for item in evidence
        if item.citation_text.strip()
    )


def _render_sequence(
    analysis: QueryAnalysis,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[_RenderedLine, ...]:
    semantics = analysis.semantics
    support_reason = _support_reason(evidence[0])
    stage_set = support_reason == "SECTION_STAGE_SET"
    structured_list = support_reason in {
        "STRUCTURED_LIST_RELATION",
        "SOURCE_CORRECTS_COUNT_PREMISE",
    }
    items = evidence[1:] if structured_list and len(evidence) > 1 else evidence
    if not items:
        return ()
    if len(items) == 1 and not stage_set and not structured_list:
        item = items[0]
        heading = _heading_label(item)
        prefix = f"{heading}｜" if heading else ""
        return (_RenderedLine(prefix + item.citation_text, (item.support_id,)),)
    if (
        semantics.answer_type is RequestedAnswerType.COUNT
        or support_reason == "SOURCE_CORRECTS_COUNT_PREMISE"
    ):
        ids = tuple(item.support_id for item in items)
        lines: list[_RenderedLine] = [
            _RenderedLine(f"共 {len(items)} 项。", ids)
        ]
    else:
        lines = []
    for index, item in enumerate(items, 1):
        if stage_set:
            label = _heading_label(item) or f"第 {index} 项"
        elif semantics.answer_type is RequestedAnswerType.ORDINAL_ITEM:
            label = f"第 {semantics.ordinal or index} 项"
        elif semantics.answer_type is RequestedAnswerType.PROCEDURE:
            label = f"步骤 {index}"
        else:
            label = f"{index}."
        separator = "：" if not label.endswith(".") else " "
        lines.append(
            _RenderedLine(
                f"{label}{separator}{item.citation_text}",
                (item.support_id,),
            )
        )
    return tuple(lines)


def _render_sections(
    analysis: QueryAnalysis,
    evidence: tuple[EvidenceItem, ...],
) -> tuple[_RenderedLine, ...]:
    target = _label(analysis.semantics.target or "相关章节")
    return tuple(
        _RenderedLine(
            "｜".join(
                part
                for part in (target, _heading_label(item), item.citation_text)
                if part
            ),
            (item.support_id,),
        )
        for item in evidence
        if item.citation_text.strip()
    )


def _table_cells(evidence: tuple[EvidenceItem, ...]) -> tuple[_Cell, ...]:
    grouped: dict[tuple[int, int], list[EvidenceItem]] = {}
    for item in evidence:
        coordinate = _table_coordinate(item)
        if coordinate is None:
            return ()
        grouped.setdefault(coordinate, []).append(item)
    cells: list[_Cell] = []
    for (row, column), items in sorted(grouped.items()):
        ordered = sorted(items, key=_source_order)
        cells.append(
            _Cell(
                row=row,
                column=column,
                text=_join_fragments(
                    tuple(item.citation_text for item in ordered)
                ),
                support_ids=tuple(item.support_id for item in ordered),
            )
        )
    return tuple(cells)


def _table_coordinate(item: EvidenceItem) -> tuple[int, int] | None:
    if not item.table_context or not item.source_spans:
        return None
    anchor = item.source_spans[0].source_anchor
    if anchor is None:
        return None
    if anchor.row_index is not None and anchor.cell_index is not None:
        return anchor.row_index, anchor.cell_index
    row: int | None = None
    column: int | None = None
    for part in anchor.structural_path:
        if match := _TABLE_ROW.fullmatch(part):
            row = int(match[1])
        elif match := _TABLE_COLUMN.fullmatch(part):
            column = int(match[1])
    return None if row is None or column is None else (row, column)


def _source_order(item: EvidenceItem) -> tuple[int, int, str]:
    span = item.source_spans[0]
    anchor = span.source_anchor
    return (
        anchor.ordinal if anchor is not None else 2**31 - 1,
        span.source_start_char or 0,
        item.support_id,
    )


def _join_fragments(values: tuple[str, ...]) -> str:
    result = ""
    for value in values:
        if not result:
            result = value
            continue
        separator = (
            " "
            if result[-1:].isascii()
            and result[-1:].isalnum()
            and value[:1].isascii()
            and value[:1].isalnum()
            else ""
        )
        result += separator + value
    return result


def _heading_label(item: EvidenceItem) -> str:
    return " > ".join(
        part.strip() for part in item.heading_path if part.strip()
    )


def _support_reason(item: EvidenceItem) -> str:
    support = dict(item.metadata).get("answer_support")
    return (
        str(support.get("support_reason", ""))
        if isinstance(support, dict)
        else ""
    )


def _label(value: str) -> str:
    return _SPACE.sub(" ", value).strip(" ：:，,。\n\r\t") or "相关内容"


def _normalized_label(value: str) -> str:
    return re.sub(r"[\W_]", "", value.casefold())


def _materialize(lines: tuple[_RenderedLine, ...]) -> RenderedAnswer:
    if any(
        not line.text.strip()
        or not line.support_ids
        or _DANGLING_LEAD_IN.fullmatch(line.text.strip())
        for line in lines
    ):
        raise ValidationFailed(
            "结构化回答包含空项或悬空导语。",
            stage="answer.render",
            code="STRUCTURED_RENDER_INCOMPLETE",
        )
    ids = tuple(
        dict.fromkeys(
            support_id for line in lines for support_id in line.support_ids
        )
    )
    text = "\n".join(
        f"{line.text} "
        + " ".join(f"[{support_id}]" for support_id in line.support_ids)
        for line in lines
    )
    return RenderedAnswer(text=text, published_support_ids=ids)


__all__ = [
    "DeterministicAnswerRenderer",
    "RenderedAnswer",
    "validate_rendered_answer",
]
