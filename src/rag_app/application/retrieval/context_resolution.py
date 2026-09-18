"""从受信用户问题构造可审计的多轮检索根问句。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import StrEnum

from pydantic import Field

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import QueryAnalysis, SearchRequest
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.query_plan import (
    QueryAtom,
    QueryPlan,
    fallback_query_plan,
    make_query_plan,
)

CONTEXT_RESOLUTION_REVISION = "wb08r-context-resolution-v1"
_CLAUSES = re.compile(r"[^，,；;。！？?!]+")
_REFERENCES = re.compile(r"这个|那个|上述|前者|后者|其中|它|这些|那些")
_SHORT_RELATION = re.compile(
    r"多久|何时|什么时候|多少|谁|哪里|怎么|如何|哪些|什么"
)
_TARGET_BEFORE_QUESTION = re.compile(
    r"^(.+?)(?:什么时候|何时|多久|多少|怎么|如何|是什么|有哪些|负责什么|需要什么)"
)
_MULTIPLE = re.compile(
    r"[^，,；;。！？?!、]{1,80}(?:、[^，,；;。！？?!、]{1,80})+"
)
_TIME_MODIFIER = re.compile(
    r"^(提前|之后|之前|以后|以前|还|再|又|那|这|同时|同时在)$"
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_NEGATION = re.compile(r"不得|无需|不必|禁止|严禁|没有|未|不")
_SEQUENCE = re.compile(r"先|再|然后|随后")
_MIN_SEQUENCE_PARTS = 2
_MAX_ATOMS = 4


class SpanKind(StrEnum):
    """用户输入片段的通用语义类别。"""

    CLAUSE = "CLAUSE"
    TARGET = "TARGET"
    RELATION = "RELATION"
    SOURCE = "SOURCE"
    LITERAL = "LITERAL"


class QueryInputSpan(FrozenModel):
    """服务端从当前或先前用户问句切出的受信片段。"""

    span_id: str = Field(min_length=3, max_length=16)
    turn: str
    kind: SpanKind
    text: str = Field(min_length=1, max_length=160, repr=False)
    normalized_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ResolvedRootQuery(FrozenModel):
    """原问和确定性组合的 Root 查询同时存在。"""

    original_query: str = Field(min_length=1, repr=False)
    resolved_query: str = Field(min_length=1, max_length=512, repr=False)
    mode: str
    confidence: str
    referenced_span_ids: tuple[str, ...] = ()
    context_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolution_revision: str = CONTEXT_RESOLUTION_REVISION
    reason_codes: tuple[str, ...] = ()


def trusted_user_questions(context: tuple[str, ...]) -> tuple[str, ...]:
    """会话密文中只提取用户问句，绝不读入模型回答。"""
    questions: list[str] = []
    for turn in context[-2:]:
        first_line = turn.splitlines()[0].strip()
        if first_line.startswith("上一问："):
            first_line = first_line.removeprefix("上一问：").strip()
        elif "已验证事实：" in first_line:
            continue
        if first_line:
            questions.append(first_line)
    return tuple(questions)


def build_input_spans(  # noqa: PLR0912
    request: SearchRequest,
) -> tuple[QueryInputSpan, ...]:
    """从当前问题和最多两轮用户问句构造稳定 ID。"""
    questions = (
        request.text,
        *reversed(trusted_user_questions(request.conversation_context)),
    )
    spans: list[QueryInputSpan] = []
    for turn_index, question in enumerate(questions):
        prefix = "Q" if turn_index == 0 else f"P{turn_index}"
        turn = "CURRENT" if turn_index == 0 else f"PREVIOUS_{turn_index}"
        normalized = unicodedata.normalize("NFKC", question).strip()
        clauses = tuple(
            match[0].strip()
            for match in _CLAUSES.finditer(normalized)
            if match[0].strip()
        )
        _append(spans, prefix, turn, SpanKind.CLAUSE, clauses or (normalized,))
        whole_analysis = QueryAnalyzer().analyze(
            request.model_copy(
                update={"text": normalized, "conversation_context": ()}
            )
        )
        if whole_analysis.semantics.source_qualifier:
            _append(
                spans,
                prefix,
                turn,
                SpanKind.SOURCE,
                (whole_analysis.semantics.source_qualifier,),
            )
        for clause in clauses or (normalized,):
            analyzed = QueryAnalyzer().analyze(
                request.model_copy(
                    update={"text": clause, "conversation_context": ()}
                )
            )
            semantics = analyzed.semantics
            targets: list[str] = []
            if (
                semantics.target
                and semantics.target in clause
                and "分别" not in semantics.target
            ):
                targets.append(semantics.target)
            syntax_target = _TARGET_BEFORE_QUESTION.search(clause)
            if syntax_target:
                candidate = syntax_target[1].strip("，,；;。！？?!、 ")
                candidate = candidate.removeprefix("同时").removesuffix("要")
                if (
                    candidate
                    and not _TIME_MODIFIER.fullmatch(candidate)
                    and "、" not in candidate
                    and "分别" not in candidate
                ):
                    targets.append(candidate)
            if "不得" in clause:
                actor = clause.split("不得", 1)[0].strip()
                if actor:
                    targets.append(actor)
            if "先" in clause:
                actor = clause.split("先", 1)[0].strip()
                if actor:
                    targets.append(actor)
            # 并列问句中的对象以原文位置分开，Planner 仍只能引用这些片段。
            before_relation = clause.split("分别", 1)[0]
            if _MULTIPLE.search(before_relation):
                targets.extend(
                    part.strip()
                    for part in before_relation.split("、")
                    if part.strip()
                )
            _append(spans, prefix, turn, SpanKind.TARGET, targets)
            _append(spans, prefix, turn, SpanKind.RELATION, (clause,))
            if len(_SEQUENCE.findall(clause)) >= _MIN_SEQUENCE_PARTS:
                _append(
                    spans,
                    prefix,
                    turn,
                    SpanKind.RELATION,
                    tuple(
                        part.strip()
                        for part in _SEQUENCE.split(clause)
                        if part.strip() and _SHORT_RELATION.search(part)
                    ),
                )
            if (
                semantics.source_qualifier
                and semantics.source_qualifier in clause
            ):
                _append(
                    spans,
                    prefix,
                    turn,
                    SpanKind.SOURCE,
                    (semantics.source_qualifier,),
                )
            _append(
                spans,
                prefix,
                turn,
                SpanKind.LITERAL,
                (
                    *analyzed.identifiers,
                    *analyzed.quoted_phrases,
                    *analyzed.date_version_signals,
                    *analyzed.numbers,
                    *analyzed.units,
                    *analyzed.negation_signals,
                    *_NUMBER.findall(clause),
                    *_NEGATION.findall(clause),
                ),
            )
    return tuple(spans)


def resolve_root_query(
    request: SearchRequest, spans: tuple[QueryInputSpan, ...]
) -> ResolvedRootQuery:
    """仅在先行对象唯一时组合短追问；歧义交给服务端澄清。"""
    original = unicodedata.normalize("NFKC", request.text).strip()
    digest = canonical_sha256(
        trusted_user_questions(request.conversation_context)
    )
    current_target = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.TARGET
    )
    current_relation = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.RELATION
    )
    previous_targets = tuple(
        span
        for span in spans
        if span.turn == "PREVIOUS_1" and span.kind is SpanKind.TARGET
    )
    needs_context = bool(_REFERENCES.search(original)) or (
        bool(previous_targets)
        and not current_target
        and bool(_SHORT_RELATION.search(original))
    )
    if not needs_context:
        return ResolvedRootQuery(
            original_query=request.text,
            resolved_query=request.text.strip()[:512],
            mode="ORIGINAL",
            confidence="HIGH",
            context_digest=digest,
        )
    unique_targets = tuple(
        dict.fromkeys(span.text for span in previous_targets)
    )
    if len(unique_targets) != 1 or not current_relation:
        return ResolvedRootQuery(
            original_query=request.text,
            resolved_query=request.text.strip()[:512],
            mode="CLARIFY",
            confidence="LOW",
            context_digest=digest,
            reason_codes=("CONTEXT_TARGET_AMBIGUOUS",),
        )
    target_span = next(
        span for span in previous_targets if span.text == unique_targets[0]
    )
    current_source = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.SOURCE
    )
    previous_source = tuple(
        span
        for span in spans
        if span.turn == "PREVIOUS_1" and span.kind is SpanKind.SOURCE
    )
    if (
        current_source
        and previous_source
        and current_source[0].text != previous_source[0].text
    ):
        return ResolvedRootQuery(
            original_query=request.text,
            resolved_query=request.text.strip()[:512],
            mode="CLARIFY",
            confidence="LOW",
            context_digest=digest,
            reason_codes=("CONTEXT_SOURCE_CONFLICT",),
        )
    selected = (
        *current_source,
        target_span,
        *current_relation[:1],
        *(
            span
            for span in spans
            if span.turn == "CURRENT" and span.kind is SpanKind.LITERAL
        ),
    )
    parts = tuple(dict.fromkeys(span.text for span in selected))
    resolved = " ".join((*parts, original))[:512]
    return ResolvedRootQuery(
        original_query=request.text,
        resolved_query=resolved,
        mode="RULE_CONTEXT",
        confidence="HIGH",
        referenced_span_ids=tuple(span.span_id for span in selected),
        context_digest=digest,
        reason_codes=("CONTEXT_TARGET_RESOLVED",),
    )


def _append(
    spans: list[QueryInputSpan],
    prefix: str,
    turn: str,
    kind: SpanKind,
    values: tuple[str, ...] | list[str],
) -> None:
    seen = {
        span.text for span in spans if span.turn == turn and span.kind is kind
    }
    for value in values:
        text = unicodedata.normalize("NFKC", value).strip()[:160]
        if not text or text in seen:
            continue
        seen.add(text)
        ordinal = (
            sum(span.turn == turn and span.kind is kind for span in spans) + 1
        )
        span_id = f"{prefix}.{kind.value[0]}{ordinal}"
        spans.append(
            QueryInputSpan(
                span_id=span_id,
                turn=turn,
                kind=kind,
                text=text,
                normalized_sha256="sha256:"
                + hashlib.sha256(text.casefold().encode()).hexdigest(),
            )
        )


def degraded_query_plan(  # noqa: PLR0913
    request: SearchRequest,
    analysis: QueryAnalysis,
    spans: tuple[QueryInputSpan, ...],
    root: ResolvedRootQuery,
    *,
    effort: str,
    reason_code: str,
    planner_called: bool,
) -> QueryPlan:
    """Planner 失败时保留可机械切分的子问题和低置信覆盖。"""
    analyzed = analysis
    if root.mode == "CLARIFY":
        base = fallback_query_plan(
            analyzed,
            effort=effort,
            reason_code=reason_code,
            planner_called=planner_called,
            original_query=request.text,
            resolved_root_query=root.resolved_query,
            context_resolution_mode=root.mode,
            context_digest=root.context_digest,
            fallback_mode="DEGRADED_CLARIFY",
            coverage_confidence="LOW",
        )
        return base.model_copy(
            update={
                "needs_clarification": True,
                "clarification_question": "请明确您所指的对象和要查询的事项。",
            }
        )
    clauses = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.CLAUSE
    )
    targets = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.TARGET
    )
    atoms: list[QueryAtom] = []
    if len(clauses) > 1 or len(targets) > 1:
        for clause in clauses:
            clause_targets = tuple(
                span for span in targets if span.text in clause.text
            )
            if not clause_targets:
                clause_targets = (None,)
            for target in clause_targets:
                if len(atoms) == _MAX_ATOMS:
                    break
                scoped = QueryAnalyzer().analyze(
                    request.model_copy(
                        update={"text": clause.text, "conversation_context": ()}
                    )
                )
                fallback = fallback_query_plan(
                    scoped,
                    effort=effort,
                    reason_code=reason_code,
                    planner_called=planner_called,
                )
                atom = fallback.atoms[0].model_copy(
                    update={
                        "atom_id": f"A{len(atoms) + 1}",
                        "target": target.text
                        if target
                        else (scoped.semantics.target or clause.text[:160]),
                        "relation": scoped.semantics.relation
                        or next(
                            (
                                span.text
                                for span in spans
                                if span.turn == "CURRENT"
                                and span.kind is SpanKind.RELATION
                                and span.text in clause.text
                            ),
                            clause.text[:160],
                        ),
                        "original_fragment": clause.text,
                    }
                )
                atoms.append(atom)
        if len(atoms) > 1:
            return make_query_plan(
                standalone_query=root.resolved_query,
                original_query=request.text,
                context_resolution_mode=root.mode,
                context_digest=root.context_digest,
                referenced_span_ids=root.referenced_span_ids,
                intent="COMPOUND",
                effort=effort,
                atoms=tuple(atoms),
                reason_code=reason_code,
                planner_called=planner_called,
                fallback_mode="DEGRADED_RULE_ATOMS",
                coverage_confidence="MEDIUM",
            )
    return fallback_query_plan(
        analyzed,
        effort=effort,
        reason_code=reason_code,
        planner_called=planner_called,
        original_query=request.text,
        resolved_root_query=root.resolved_query,
        context_resolution_mode=root.mode,
        context_digest=root.context_digest,
        referenced_span_ids=root.referenced_span_ids,
        fallback_mode="DEGRADED_RESOLVED_ROOT",
        coverage_confidence="LOW",
    )
