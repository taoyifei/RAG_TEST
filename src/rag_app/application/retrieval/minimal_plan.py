"""把模型选择的受信 Span ID 转成类型化 QueryAtom。"""

from __future__ import annotations

import re

from pydantic import ConfigDict, Field

from rag_app.application.retrieval.context_resolution import (
    QueryInputSpan,
    SpanKind,
    current_context_modifier_clauses,
)
from rag_app.core.models import QueryAnalysis
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomConstraintKind,
    QueryAtom,
    QueryConstraint,
)

_VERSION = re.compile(r"(?i)(?<![a-z0-9])v\d+(?:\.\d+)*(?![a-z0-9])")
_DATE = re.compile(r"\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_NEGATION = re.compile(
    r"不得|无需|不必|禁止|严禁|没有|未|不(?![呢吗呀啊]?$)"
)
_UNIT = re.compile(r"^(秒|分钟|小时|日|天|周|月|年|万元|元|%|％|千克|公斤|米)")
_DURATION_UNIT = re.compile(r"^(秒|分钟|小时|日|天|周|月|年)")


class MinimalPlanValidationError(ValueError):
    """只向 Trace 暴露稳定、无正文的失败类别。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MinimalAtomPayload(FrozenModel):
    """模型只选择服务端给定的片段身份和回答形状。"""

    model_config = ConfigDict(populate_by_name=True)

    fragment_span_ids: tuple[str, ...] = Field(
        alias="f", min_length=1, max_length=2
    )
    target_span_id: str = Field(alias="t")
    relation_span_id: str = Field(alias="r")
    answer_shape: AtomAnswerShape = Field(alias="s")


class MinimalPlanPayload(FrozenModel):
    """无自由文本事实字段的单次 Planner 输出。"""

    model_config = ConfigDict(populate_by_name=True)

    intent: str = Field(alias="i", pattern=r"^(SINGLE|COMPOUND|FOLLOW_UP)$")
    clarification_reason: None = Field(default=None, alias="c")
    atoms: tuple[MinimalAtomPayload, ...] = Field(
        alias="a", min_length=1, max_length=4
    )


def planner_json_schema(
    spans: tuple[QueryInputSpan, ...],
) -> dict[str, object]:
    """把本请求受信 Span 的类型约束加入模型输出 Schema。"""
    current_clauses = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.CLAUSE
    )
    modifiers = current_context_modifier_clauses(current_clauses)
    answer_clauses = tuple(
        span for span in current_clauses if span not in modifiers
    ) or current_clauses
    clause_ids = [span.span_id for span in answer_clauses]
    target_ids = [
        span.span_id for span in spans if span.kind is SpanKind.TARGET
    ]
    relation_ids = [
        span.span_id
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.RELATION
    ]
    if not clause_ids or not target_ids or not relation_ids:
        raise MinimalPlanValidationError("PLANNER_SPAN_INPUT_INVALID")
    schema = MinimalPlanPayload.model_json_schema()
    # 澄清已由可信 Root 决定；输出仍兼容旧 c 字段，但不再要求模型重复 null。
    schema["properties"].pop("c")
    schema["required"] = [
        field for field in schema["required"] if field != "c"
    ]
    atom_definition = schema["$defs"]["MinimalAtomPayload"]
    properties = atom_definition["properties"]
    properties["f"]["items"]["enum"] = clause_ids
    properties["t"]["enum"] = target_ids
    properties["r"]["enum"] = relation_ids
    return schema


def build_query_atoms(
    payload: MinimalPlanPayload,
    spans: tuple[QueryInputSpan, ...],
    analysis: QueryAnalysis,
) -> tuple[QueryAtom, ...]:
    """仅解引用服务端受信片段；不存在的 ID 拒绝整份计划。"""
    by_id = {span.span_id: span for span in spans}
    current_clauses = tuple(
        span
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.CLAUSE
    )
    modifier_clauses = current_context_modifier_clauses(current_clauses)
    required_clauses = tuple(
        span for span in current_clauses if span not in modifier_clauses
    ) or current_clauses
    referenced_clauses: set[str] = set()
    atoms: list[QueryAtom] = []
    for index, item in enumerate(payload.atoms, 1):
        ids = (
            *item.fragment_span_ids,
            item.target_span_id,
            item.relation_span_id,
        )
        if any(span_id not in by_id for span_id in ids):
            raise MinimalPlanValidationError("PLANNER_UNKNOWN_SPAN_REFERENCE")
        fragments = tuple(by_id[span_id] for span_id in item.fragment_span_ids)
        target = by_id[item.target_span_id]
        relation = by_id[item.relation_span_id]
        if any(
            span.kind is not SpanKind.CLAUSE for span in fragments
        ) or not any(span.turn == "CURRENT" for span in fragments):
            raise MinimalPlanValidationError("PLANNER_INVALID_SPAN_KIND")
        if (
            target.kind is not SpanKind.TARGET
            or relation.kind is not SpanKind.RELATION
            or relation.turn != "CURRENT"
        ):
            raise MinimalPlanValidationError("PLANNER_INVALID_SPAN_KIND")
        referenced_clauses.update(
            span.span_id for span in fragments if span.turn == "CURRENT"
        )
        fragment = " ".join(
            dict.fromkeys(
                (
                    *(span.text for span in modifier_clauses),
                    *(span.text for span in fragments),
                )
            )
        )
        source = analysis.semantics.source_qualifier
        atoms.append(
            QueryAtom(
                atom_id=f"A{index}",
                target=target.text,
                relation=relation.text,
                answer_shape=item.answer_shape,
                source_qualifier=source,
                constraints=_constraints_for_fragment(fragment, source),
                original_fragment=fragment[:320],
            )
        )
    if {span.span_id for span in required_clauses} - referenced_clauses:
        raise MinimalPlanValidationError("PLANNER_CLAUSE_UNCOVERED")
    literal_values = {
        span.text
        for span in spans
        if span.turn == "CURRENT" and span.kind is SpanKind.LITERAL
    }
    atom_text = " ".join(
        (analysis.normalized_query, *(atom.search_text for atom in atoms))
    )
    if any(value not in atom_text for value in literal_values):
        raise MinimalPlanValidationError("PLANNER_LITERAL_VIOLATION")
    return tuple(atoms)


def _constraints_for_fragment(
    fragment: str, source: str | None
) -> tuple[QueryConstraint, ...]:
    constraints: list[QueryConstraint] = []
    protected: list[tuple[int, int]] = []
    for pattern, kind in (
        (_VERSION, AtomConstraintKind.VERSION),
        (_DATE, AtomConstraintKind.DATE_TIME),
    ):
        for match in pattern.finditer(fragment):
            protected.append(match.span())
            constraints.append(QueryConstraint(kind=kind, value=match[0]))
    for match in _NUMBER.finditer(fragment):
        if any(
            start <= match.start() and match.end() <= end
            for start, end in protected
        ):
            continue
        suffix = fragment[match.end() :]
        unit_match = _UNIT.match(suffix)
        constraints.append(
            QueryConstraint(
                kind=AtomConstraintKind.DURATION
                if _DURATION_UNIT.match(suffix)
                else AtomConstraintKind.NUMBER,
                value=match[0],
                unit=unit_match[0] if unit_match else None,
            )
        )
    constraints.extend(
        QueryConstraint(
            kind=AtomConstraintKind.NEGATION,
            value=match[0],
            polarity="NEGATIVE",
        )
        for match in _NEGATION.finditer(fragment)
    )
    if source:
        constraints.append(
            QueryConstraint(kind=AtomConstraintKind.SOURCE, value=source)
        )
    return tuple(dict.fromkeys(constraints))[:12]


__all__ = [
    "MinimalAtomPayload",
    "MinimalPlanPayload",
    "MinimalPlanValidationError",
    "build_query_atoms",
    "planner_json_schema",
]
