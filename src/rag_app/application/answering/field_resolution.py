"""从可信表结构生成字段候选，并冻结一次 schema-aware 解析。"""

from __future__ import annotations

from collections import defaultdict

from rag_app.application.retrieval.generation_evidence import (
    GenerationEvidencePack,
)
from rag_app.core.models.answer_plan import (
    FieldCandidate,
    FieldResolution,
    FieldResolutionStatus,
    ResolvedQueryView,
)
from rag_app.core.models.query_plan import QueryPlan
from rag_app.core.models.retrieval import (
    EvidenceItem,
    PhysicalTableFact,
)
from rag_app.core.query_text import (
    normalize_document_label,
    table_axis_label_in_query,
)
from rag_app.core.source_scope import source_identity_allowed


def _ordered_text(
    support_ids: tuple[str, ...],
    registry: dict[str, EvidenceItem],
) -> str:
    """按来源登记顺序连接有限文本。"""
    return " / ".join(
        dict.fromkeys(
            registry[support_id].citation_text.strip()
            for support_id in support_ids
            if registry[support_id].citation_text.strip()
        )
    )


def _label_matches(value: str, label: str) -> bool:
    """只用规范标签包含关系确认目标行，不推断字段同义。"""
    normalized_value = normalize_document_label(value)
    normalized_label = normalize_document_label(label)
    return bool(normalized_value and normalized_label) and (
        normalized_value in normalized_label
        or normalized_label in normalized_value
    )


def build_field_candidates(
    query_plan: QueryPlan,
    pack: GenerationEvidencePack,
) -> tuple[FieldCandidate, ...]:
    """从获准物理事实生成全部同目标竞争字段，不看关系支持状态。"""
    registry = {item.support_id: item for item in pack.evidence}
    raw: dict[tuple[str, str], tuple[str, PhysicalTableFact]] = {}
    # FieldCandidate 直接来自获准 canonical schema。同目标的竞争列不能先
    # 经过 AtomFactBinding，因为该绑定仍含关系相关筛选；即便不读取它的
    # relation_status，也会在字段解析前悄悄丢掉“输出”等真实候选。
    for atom in query_plan.atoms:
        for fact in pack.physical_table_facts:
            if not source_identity_allowed(
                atom.source_scope,
                document_id=fact.document_id,
                document_version_id=fact.document_version_id,
            ):
                continue
            if not set(fact.all_support_ids) <= registry.keys():
                continue
            target_label = _ordered_text(fact.row_label_support_ids, registry)
            if not _label_matches(atom.target, target_label):
                continue
            if any(
                not registry[support_id].publishable
                or not registry[support_id].source_spans
                or any(
                    not span.is_citable
                    for span in registry[support_id].source_spans
                )
                for support_id in fact.all_support_ids
            ):
                continue
            raw[(atom.atom_id, fact.fact_id)] = (atom.atom_id, fact)
    ordered = sorted(
        raw.values(),
        key=lambda pair: (
            pair[0],
            pair[1].document_version_id,
            pair[1].table_key,
            pair[1].row_index,
            pair[1].value_column_index,
            pair[1].fact_id,
        ),
    )
    result: list[FieldCandidate] = []
    for index, (atom_id, fact) in enumerate(ordered, 1):
        target_label = _ordered_text(fact.row_label_support_ids, registry)
        field_label = _ordered_text(fact.header_support_ids, registry)
        value = _ordered_text(fact.value_support_ids, registry)
        if not target_label or not field_label or not value:
            continue
        result.append(
            FieldCandidate(
                candidate_id=f"F{index}",
                atom_id=atom_id,
                fact_id=fact.fact_id,
                document_id=fact.document_id,
                document_version_id=fact.document_version_id,
                table_key=fact.table_key,
                row_index=fact.row_index,
                value_column_index=fact.value_column_index,
                target_label=target_label,
                field_label=field_label,
                value_preview=value[:320],
                dependency_support_ids=fact.all_support_ids,
            )
        )
    return tuple(result)


def _default_resolution(
    atom_id: str,
    candidates: tuple[FieldCandidate, ...],
    query_view: ResolvedQueryView,
) -> FieldResolution:
    """先冻结可形式验证的精确轴；其余不猜等义。"""
    exact = tuple(
        item
        for item in candidates
        if table_axis_label_in_query(
            query_view.business_query, item.target_label
        )
        and table_axis_label_in_query(
            query_view.business_query, item.field_label
        )
    )
    if len(exact) == 1:
        return FieldResolution(
            atom_id=atom_id,
            status=FieldResolutionStatus.EXACT,
            candidate_ids=(exact[0].candidate_id,),
            reason_code="EXACT_SCHEMA_AXES",
        )
    if len(exact) > 1:
        return FieldResolution(
            atom_id=atom_id,
            status=FieldResolutionStatus.AMBIGUOUS,
            candidate_ids=tuple(item.candidate_id for item in exact),
            reason_code="MULTIPLE_EXACT_SCHEMA_FIELDS",
        )
    if len(candidates) == 1:
        return FieldResolution(
            atom_id=atom_id,
            status=FieldResolutionStatus.RELATED_FIELD,
            candidate_ids=(candidates[0].candidate_id,),
            reason_code="SINGLE_RELATED_SCHEMA_FIELD",
        )
    if candidates:
        return FieldResolution(
            atom_id=atom_id,
            status=FieldResolutionStatus.AMBIGUOUS,
            candidate_ids=tuple(item.candidate_id for item in candidates),
            reason_code="SCHEMA_FIELDS_REQUIRE_INTERPRETATION",
        )
    return FieldResolution(
        atom_id=atom_id,
        status=FieldResolutionStatus.NOT_FOUND,
        reason_code="NO_SCHEMA_FIELD_FOR_TARGET",
    )


def default_field_resolutions(
    query_plan: QueryPlan,
    candidates: tuple[FieldCandidate, ...],
    query_view: ResolvedQueryView,
) -> tuple[FieldResolution, ...]:
    """为每个 Atom 生成精确、相关、歧义或未找到的保守初态。"""
    by_atom: dict[str, list[FieldCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_atom[candidate.atom_id].append(candidate)
    return tuple(
        _default_resolution(
            atom.atom_id,
            tuple(by_atom.get(atom.atom_id, ())),
            query_view,
        )
        for atom in query_plan.atoms
    )


def merge_field_resolutions(
    defaults: tuple[FieldResolution, ...],
    proposed: tuple[FieldResolution, ...],
    candidates: tuple[FieldCandidate, ...],
    query_view: ResolvedQueryView,
) -> tuple[FieldResolution, ...]:
    """核对模型仅选择真实 ID；精确 schema 结果不能被模型覆盖。"""
    candidate_by_id = {item.candidate_id: item for item in candidates}
    proposals = {item.atom_id: item for item in proposed}
    result: list[FieldResolution] = []
    for default in defaults:
        if default.status is FieldResolutionStatus.EXACT:
            result.append(default)
            continue
        proposal = proposals.get(default.atom_id)
        if proposal is None:
            result.append(default)
            continue
        owned = tuple(
            candidate_by_id[candidate_id]
            for candidate_id in proposal.candidate_ids
            if candidate_id in candidate_by_id
            and candidate_by_id[candidate_id].atom_id == default.atom_id
        )
        if len(owned) != len(proposal.candidate_ids):
            result.append(default)
            continue
        if proposal.status is FieldResolutionStatus.EXACT:
            result.append(default)
            continue
        if (
            proposal.query_span_start is not None
            and proposal.query_span_end is not None
            and proposal.query_span_end > len(query_view.original_query)
        ):
            result.append(default)
            continue
        result.append(proposal)
    return tuple(result)


__all__ = [
    "build_field_candidates",
    "default_field_resolutions",
    "merge_field_resolutions",
]
