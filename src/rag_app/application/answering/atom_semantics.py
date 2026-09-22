"""将经过边界校验的 Atom 派生为唯一的当前问题语义。"""

from __future__ import annotations

import re

from rag_app.core.models import (
    QueryAnalysis,
    QuerySemantics,
    RequestedAnswerType,
)
from rag_app.core.models.query_plan import QueryAtom

_QUESTION = re.compile(r"[?？]|谁|何时|哪些|什么|怎么|如何|能否|是否|啥")


def current_atom_analysis(
    atom: QueryAtom, analysis: QueryAnalysis | None
) -> QueryAnalysis:
    """保留原文和会话身份，只让本 Atom 的限制参与关系核验。

    完整 Clause 是历史兼容输入，先复用共享解析器。解析未命中时保持
    UNKNOWN，不能把疑问词当作答案必须逐字包含的对象。
    """
    from rag_app.application.retrieval.semantics import (  # noqa: PLC0415
        parse_query_semantics,
    )

    question = atom.original_fragment or atom.search_text
    parsed = parse_query_semantics(atom.target)
    clause_target = (
        _QUESTION.search(atom.target) is not None
        or "POLAR_SUBJECT_ACTION_QUESTION_SYNTAX" in parsed.reason_codes
    )
    shape = RequestedAnswerType.__members__.get(
        atom.answer_shape.value, RequestedAnswerType.UNKNOWN
    )
    semantics = (
        parsed
        if clause_target
        else QuerySemantics(
            target=atom.target,
            relation=atom.relation,
            answer_type=shape,
            source="SPAN_REFERENCED",
        )
    )
    base = analysis or QueryAnalysis(
        original_query=question,
        normalized_query=question,
        conversation_fingerprint="sha256:" + "0" * 64,
    )
    # 根问题可能包含其他子问，不能把它们的数字或角色限制复制过来。
    # 当前 Clause、显式约束以及已解析的当前对象是限制的归属依据。
    scope = " ".join(
        (
            question,
            atom.target,
            atom.relation,
            *(c.value for c in atom.constraints),
        )
    )
    same_target = base.semantics.target in {atom.target, semantics.target}
    inherited_source = base.semantics.source_qualifier
    source = atom.source_qualifier or semantics.source_qualifier
    if source is None and same_target:
        source = inherited_source
    context = semantics.context_qualifier
    current_times = tuple(
        constraint.value
        for constraint in atom.constraints
        if constraint.kind.value == "DATE_TIME"
    )
    if current_times:
        context = " ".join(current_times)
    if context is None and same_target:
        context = base.semantics.context_qualifier
    constraints = tuple(
        item for item in base.semantics.constraints if item.raw_text in scope
    )
    semantics = semantics.model_copy(
        update={
            "source_qualifier": source,
            "context_qualifier": context,
            "constraints": constraints,
            "expected_count": semantics.expected_count
            or (base.semantics.expected_count if same_target else None),
            "ordinal": semantics.ordinal
            or (base.semantics.ordinal if same_target else None),
        }
    )
    updates: dict[str, object] = {
        "normalized_query": question,
        "resolved_query": question,
        "semantics": semantics,
        "reason_codes": (*base.reason_codes, "CURRENT_ATOM_SEMANTICS"),
    }
    for name in (
        "quoted_phrases",
        "identifiers",
        "numbers",
        "units",
        "date_version_signals",
        "negation_signals",
    ):
        updates[name] = tuple(
            value for value in getattr(base, name) if value in scope
        )
    return base.model_copy(update=updates)
