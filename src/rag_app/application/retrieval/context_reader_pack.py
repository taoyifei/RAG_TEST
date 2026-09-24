"""把来源回读组直接接入现有生成与核验合同。"""

from __future__ import annotations

from rag_app.application.retrieval.context_reader import ContextReadGroup
from rag_app.application.retrieval.evidence import _evidence_item
from rag_app.application.retrieval.generation_evidence import (
    EvidenceAdmissionStatus,
    GenerationEvidenceEntry,
    GenerationEvidencePack,
    _atom_fact_bindings,
    _hard_reasons,
    _physical_table_facts,
    _source_matches,
)
from rag_app.core.models import (
    EvidenceGroup,
    EvidenceGroupKind,
    EvidenceItem,
    GroupSourceMap,
    RankedChunk,
    SearchRequest,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import QueryPlan

CONTEXT_READER_PACK_REVISION = "wb08r-context-reader-pack-v3"
_SHA256_HEX_LENGTH = 64


def _evidence_group_id(group: ContextReadGroup) -> str:
    """把回读器的完整摘要映射成既有 EvidenceGroup 身份格式。"""
    digest = group.group_id.removeprefix("sha256:")
    if len(digest) != _SHA256_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in digest
    ):
        raise ValueError("来源阅读组身份格式无效。")
    return f"egrp_{digest[:32]}"


def _trusted_group(group: ContextReadGroup) -> EvidenceGroup:
    """用已核验的 canonical span 构造终端可复核的组成员映射。"""
    materialized = tuple(piece for piece in group.pieces if piece.text.strip())
    members = tuple(
        {
            piece.candidate.hydrated.chunk.chunk_id: piece.candidate
            for piece in materialized
        }.values()
    )
    if not members:
        raise ValueError("空来源组不能成为可信结构组。")
    candidate_by_id = {
        member.hydrated.chunk.chunk_id: member for member in members
    }
    source_maps = tuple(
        GroupSourceMap(
            chunk_id=chunk_id,
            citation_text=candidate.hydrated.chunk.citation_text,
            source_spans=tuple(
                piece.span
                for piece in materialized
                if piece.candidate.hydrated.chunk.chunk_id == chunk_id
            ),
        )
        for chunk_id, candidate in candidate_by_id.items()
    )
    first = members[0].hydrated
    chunk = first.chunk
    kind = (
        EvidenceGroupKind.TABLE_ROW_GROUP
        if group.kind == "table_row"
        else EvidenceGroupKind.LIST_GROUP
        if group.kind == "list"
        else EvidenceGroupKind.PARAGRAPH_GROUP
    )
    return EvidenceGroup(
        group_id=_evidence_group_id(group),
        kind=kind,
        document_id=chunk.version.document_id,
        document_version_id=chunk.version.document_version_id,
        index_revision_id=chunk.index_revision_id,
        section_id=chunk.section_id,
        display_name=first.display_name,
        heading_path=chunk.heading_path,
        member_chunk_ids=tuple(candidate_by_id),
        member_source_maps=source_maps,
        member_ranks=tuple(
            member.rerank_rank or member.fusion_rank for member in members
        ),
        complete=group.source_complete,
        incomplete_reasons=group.reason_codes,
        token_cost=0,
    )


def _group_item(
    group: ContextReadGroup,
    candidate: RankedChunk,
    span_index: int,
) -> EvidenceItem:
    """物化真实 SourceSpan，并保留回读组和逻辑表格行身份。"""
    span = group.pieces[span_index].span
    item = _evidence_item(candidate, span, group.pieces[span_index].text, "S0")
    metadata = dict(item.metadata)
    metadata.update(
        {
            "context_reader_group_id": group.group_id,
            "context_reader_source_complete": group.source_complete,
            "context_reader_reason_codes": list(group.reason_codes),
            "evidence_group_id": _evidence_group_id(group),
            "evidence_group_type": (
                "TABLE_ROW_GROUP"
                if group.kind == "table_row"
                else "LIST_GROUP"
                if group.kind == "list"
                else "SOURCE_NODE_GROUP"
            ),
            "group_complete": group.source_complete,
        }
    )
    if (
        group.kind == "table_row"
        and group.table_node_id is not None
        and group.table_row_index is not None
        and span.is_repeated
    ):
        # 共享物理节点仍位于原始行；目标逻辑行只来自已核验的组关系。
        metadata["table_logical_node_id"] = group.table_node_id
        metadata["table_logical_row_index"] = group.table_row_index
    return item.model_copy(update={"metadata": freeze_json_object(metadata)})


def build_context_reader_evidence_pack(
    *,
    query_plan: QueryPlan,
    groups: tuple[ContextReadGroup, ...],
    request: SearchRequest,
    active_revision_id: str,
    excluded_document_ids: tuple[str, ...],
) -> GenerationEvidencePack:
    """按完整组准入阅读材料，禁止旧证据配额裁掉来源成员。

    Args:
        query_plan: 本次原问与 Atom 的冻结解析。
        groups: 组级预算已选出的不可拆分回读结果。
        request: 本次授权作用域及过滤器。
        active_revision_id: 单请求固定的活动索引版本。
        excluded_document_ids: 当前不可见的文档。

    Returns:
        可直接用于现有生成器与来源核验的证据包。

    """
    entries: list[GenerationEvidenceEntry] = []
    rejected: list[GenerationEvidenceEntry] = []
    candidates: dict[str, RankedChunk] = {}
    complete_ids: list[str] = []
    partial_ids: list[str] = []
    trusted: list[EvidenceGroup] = []
    reasons: list[str] = []
    for group in groups:
        prepared: list[tuple[EvidenceItem, RankedChunk]] = []
        group_hard = False
        for index, piece in enumerate(group.pieces):
            if not piece.text.strip():
                continue
            candidate = piece.candidate
            item = _group_item(group, candidate, index)
            hard = _hard_reasons(
                item,
                candidate=candidate,
                groups_by_id={},
                possible_atoms=query_plan.atoms,
                request=request,
                active_revision_id=active_revision_id,
                excluded_document_ids=frozenset(excluded_document_ids),
            )
            if hard:
                group_hard = True
                rejected.append(
                    GenerationEvidenceEntry(
                        support_id=f"R{len(rejected) + 1}",
                        evidence_item=item,
                        source_group_id=_evidence_group_id(group),
                        linked_atom_ids=(),
                        admission_status=EvidenceAdmissionStatus.REJECTED_HARD,
                        hard_reject_reasons=hard,
                        soft_signals=(),
                        rerank_rank=candidate.rerank_rank,
                        source_order=(
                            piece.span.source_anchor.ordinal
                            if piece.span.source_anchor is not None
                            else None
                        ),
                        table_node_id=group.table_node_id,
                        table_group_id=item.table_locator,
                        table_row_index=group.table_row_index,
                    )
                )
            prepared.append((item, candidate))
        if not prepared or group_hard:
            reasons.append("CONTEXT_GROUP_REJECTED_HARD")
            continue
        group_identity = _evidence_group_id(group)
        for raw_item, candidate in prepared:
            support_id = f"S{len(entries) + 1}"
            item = raw_item.model_copy(update={"evidence_id": support_id})
            linked = tuple(
                atom.atom_id
                for atom in query_plan.atoms
                if _source_matches(atom, item)
            )
            entries.append(
                GenerationEvidenceEntry(
                    support_id=support_id,
                    evidence_item=item,
                    source_group_id=group_identity,
                    linked_atom_ids=linked,
                    admission_status=(
                        EvidenceAdmissionStatus.ADMITTED
                        if group.source_complete
                        else EvidenceAdmissionStatus.ADMITTED_STRUCTURED_PARTIAL
                    ),
                    hard_reject_reasons=(),
                    soft_signals=(),
                    rerank_rank=candidate.rerank_rank,
                    source_order=(
                        item.source_spans[0].source_anchor.ordinal
                        if item.source_spans[0].source_anchor is not None
                        else None
                    ),
                    table_node_id=group.table_node_id,
                    table_group_id=item.table_locator,
                    table_row_index=group.table_row_index,
                )
            )
            candidates.setdefault(item.chunk_id, candidate)
        if group.source_complete:
            complete_ids.append(group_identity)
            trusted.append(_trusted_group(group))
        else:
            partial_ids.append(group_identity)
            reasons.extend(group.reason_codes)
    per_atom = tuple(
        (
            atom.atom_id,
            tuple(
                entry.support_id
                for entry in entries
                if atom.atom_id in entry.linked_atom_ids
            ),
        )
        for atom in query_plan.atoms
    )
    priority = tuple(
        (
            atom_id,
            tuple(
                stable_support_key(entry.evidence_item)
                for entry in entries
                if entry.support_id in support_ids
            ),
        )
        for atom_id, support_ids in per_atom
        if support_ids
    )
    facts = _physical_table_facts(
        tuple(entry.evidence_item for entry in entries), candidates
    )
    bindings = _atom_fact_bindings(
        query_plan,
        facts,
        tuple(entry.evidence_item for entry in entries),
        per_atom,
        priority,
    )
    return GenerationEvidencePack(
        original_query=query_plan.original_query,
        resolved_root_query=query_plan.resolved_root_query,
        entries=tuple(entries),
        rejected_entries=tuple(rejected),
        per_atom_candidate_support_ids=per_atom,
        complete_group_ids=tuple(complete_ids),
        partial_group_ids=tuple(partial_ids),
        missing_atom_ids=tuple(
            atom_id for atom_id, support_ids in per_atom if not support_ids
        ),
        pack_revision=CONTEXT_READER_PACK_REVISION,
        trusted_source_groups=tuple(trusted),
        priority_source_units=priority,
        reading_unit_reason_codes=tuple(dict.fromkeys(reasons)),
        physical_table_facts=facts,
        atom_fact_bindings=bindings,
    )


__all__ = [
    "CONTEXT_READER_PACK_REVISION",
    "build_context_reader_evidence_pack",
]
