"""在一个本地 Phase 内按原子锚点闭合少量同源结构邻居。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_app.application.retrieval.atom_group_alignment import (
    AlignmentQualification,
    align_atom_to_groups,
)
from rag_app.application.retrieval.evidence_groups import (
    GroupCandidate,
    _coordinate_labels,
    build_evidence_groups,
    rank_evidence_groups,
)
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    EvidenceGroupKind,
    HydratedChunk,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.models.query_plan import (
    AtomCandidateLink,
    AtomStatus,
    AtomSupportMatrix,
    QueryPlan,
)
from rag_app.core.ports import EvidenceSourcePort

_MAX_ADDED_CHUNKS = 12
_MAX_ADDED_GROUPS = 4
_ROW_LABEL = re.compile(r"r(?P<row>\d+):c0$")
_CORRECTION_TRIGGERS = frozenset(
    {
        "EVIDENCE_PRESENT_BUT_NOT_OWNED",
        "ROOT_SOURCE_HIT_ATOM_MISS",
        "STRUCTURE_GROUP_INCOMPLETE",
    }
)


@dataclass(frozen=True, slots=True)
class AtomCorrectionTrace:
    """单 Atom 的闭库纠错结果；只含结构 ID 和稳定原因码。"""

    atom_id: str
    reason_code: str
    anchor_group_id: str | None
    anchor_document_id: str | None
    anchor_section_id: str | None
    added_chunk_count: int


@dataclass(frozen=True, slots=True)
class PerAtomCorrectionOutcome:
    """全 QueryPlan 只执行一次的有界本地纠错结果。"""

    candidates: tuple[RankedChunk, ...]
    groups: tuple[GroupCandidate, ...]
    added_chunk_count: int
    added_group_count: int
    atom_traces: tuple[AtomCorrectionTrace, ...]


def correct_per_atom(  # noqa: PLR0913
    *,
    source: EvidenceSourcePort,
    snapshot: ActiveRevisionQuerySnapshot,
    candidates: tuple[RankedChunk, ...],
    groups: tuple[GroupCandidate, ...],
    links: tuple[AtomCandidateLink, ...],
    matrix: AtomSupportMatrix,
    query_plan: QueryPlan,
    policy: RetrievalPolicy,
) -> PerAtomCorrectionOutcome:
    """每个缺项只选一个合格组，不读整节或跨版本扩展。"""
    ranked = {item.hydrated.chunk.chunk_id: item for item in candidates}
    added_ids: set[str] = set()
    anchors: list[GroupCandidate] = []
    traces: list[AtomCorrectionTrace] = []
    next_rank = max((item.fusion_rank for item in candidates), default=0)
    atom_by_id = {atom.atom_id: atom for atom in query_plan.atoms}
    for support in matrix.atoms:
        if support.status not in {AtomStatus.PARTIAL, AtomStatus.MISSING}:
            continue
        if not set(support.missing_aspects) & _CORRECTION_TRIGGERS:
            traces.append(
                AtomCorrectionTrace(
                    support.atom_id,
                    "CORRECTION_NOT_TRIGGERED",
                    None,
                    None,
                    None,
                    0,
                )
            )
            continue
        atom = atom_by_id[support.atom_id]
        alignments = align_atom_to_groups(atom, groups, links, policy)
        eligible = [
            (alignment, candidate)
            for alignment, candidate in zip(alignments, groups, strict=True)
            if alignment.qualification is not AlignmentQualification.REJECTED
        ]
        eligible.sort(
            key=lambda pair: (
                pair[0].qualification is not AlignmentQualification.STRONG,
                not pair[0].provenance_hit,
                -pair[0].target_anchor_score,
            )
        )
        if not eligible:
            traces.append(
                AtomCorrectionTrace(
                    support.atom_id, "NO_QUALIFIED_ANCHOR", None, None, None, 0
                )
            )
            continue
        anchor = eligible[0][1]
        anchors.append(anchor)
        remaining = _MAX_ADDED_CHUNKS - len(added_ids)
        if remaining <= 0:
            traces.append(
                _trace(support.atom_id, anchor, "CHUNK_BUDGET_EXHAUSTED", 0)
            )
            continue
        neighbor_ids = _neighbor_ids(anchor, ranked)[:remaining]
        if not neighbor_ids:
            traces.append(
                _trace(support.atom_id, anchor, "NO_GROUP_NEIGHBOR", 0)
            )
            continue
        hydrated = source.hydrate_chunks(snapshot, neighbor_ids)
        if len(hydrated) != len(neighbor_ids):
            raise IndexCorrupt(
                "闭库纠错邻居回读数量不一致。", stage="retrieval.corrective"
            )
        if any(
            item.chunk.chunk_id != requested
            for requested, item in zip(neighbor_ids, hydrated, strict=True)
        ):
            raise IndexCorrupt(
                "闭库纠错回读 Chunk 身份不一致。", stage="retrieval.corrective"
            )
        valid = tuple(
            item for item in hydrated if _allowed_neighbor(item, anchor)
        )
        for item in valid:
            next_rank += 1
            chunk = item.chunk
            ranked[chunk.chunk_id] = RankedChunk(
                hydrated=item,
                fusion_rank=next_rank,
                expansion_reason="closed_correction",
                expansion_seed_ids=(anchor.group.member_chunk_ids[0],),
            )
            added_ids.add(chunk.chunk_id)
        traces.append(
            _trace(
                support.atom_id,
                anchor,
                "GROUP_NEIGHBORS_ADDED" if valid else "NEIGHBORS_OUTSIDE_GROUP",
                len(valid),
            )
        )
    corrected = tuple(ranked.values())
    if not added_ids:
        return PerAtomCorrectionOutcome(corrected, groups, 0, 0, tuple(traces))
    rebuilt = rank_evidence_groups(
        build_evidence_groups(
            corrected,
            max_groups=policy.rerank_candidate_limit,
            max_member_chunks=policy.group_member_chunk_limit,
            rerank_text_char_limit=policy.rerank_text_char_limit,
        )
    )
    additions = tuple(
        group
        for group in rebuilt
        if set(group.group.member_chunk_ids) & added_ids
        and any(
            group.group.document_version_id
            == anchor.group.document_version_id
            and group.group.section_id == anchor.group.section_id
            and set(group.group.member_chunk_ids)
            & set(anchor.group.member_chunk_ids)
            for anchor in anchors
        )
    )[:_MAX_ADDED_GROUPS]
    replaced = {
        anchor.group_id
        for anchor in anchors
        if any(
            set(anchor.group.member_chunk_ids)
            & set(addition.group.member_chunk_ids)
            for addition in additions
        )
    }
    final_groups = (
        *(group for group in groups if group.group_id not in replaced),
        *additions,
    )
    return PerAtomCorrectionOutcome(
        corrected,
        final_groups,
        len(added_ids),
        len(additions),
        tuple(traces),
    )


def _neighbor_ids(
    anchor: GroupCandidate, ranked: dict[str, RankedChunk]
) -> tuple[str, ...]:
    members = anchor.members
    candidates = (
        *(member.hydrated.chunk.previous_chunk_id for member in members),
        *(member.hydrated.chunk.next_chunk_id for member in members),
    )
    return tuple(
        chunk_id
        for chunk_id in dict.fromkeys(candidates)
        if chunk_id and chunk_id not in ranked
    )


def _allowed_neighbor(
    hydrated: HydratedChunk, anchor: GroupCandidate
) -> bool:
    chunk = hydrated.chunk
    first = anchor.members[0].hydrated.chunk
    if (
        chunk.version.document_id != anchor.group.document_id
        or chunk.version.document_version_id
        != anchor.group.document_version_id
        or chunk.section_id != anchor.group.section_id
        or chunk.neighbor_group_id != first.neighbor_group_id
    ):
        return False
    if anchor.group.kind in {
        EvidenceGroupKind.LIST_GROUP,
        EvidenceGroupKind.PROCEDURE_GROUP,
    } and chunk.role.value not in {"list", "text"}:
        return False
    if anchor.group.kind is EvidenceGroupKind.TABLE_ROW_GROUP:
        rows = tuple(
            int(match["row"])
            for source_map in anchor.group.member_source_maps
            for coordinate in source_map.structural_coordinates
            if (match := _ROW_LABEL.fullmatch(coordinate))
        )
        if not rows:
            return False
        data_row = max(rows)
        ranked_chunk = RankedChunk(hydrated=hydrated, fusion_rank=1)
        if not any(
            label.startswith(f"r{data_row}:c")
            for label in _coordinate_labels(ranked_chunk)
        ):
            return False
    return True


def _trace(
    atom_id: str, anchor: GroupCandidate, reason: str, added: int
) -> AtomCorrectionTrace:
    return AtomCorrectionTrace(
        atom_id=atom_id,
        reason_code=reason,
        anchor_group_id=anchor.group_id,
        anchor_document_id=anchor.group.document_id,
        anchor_section_id=anchor.group.section_id,
        added_chunk_count=added,
    )


__all__ = [
    "AtomCorrectionTrace",
    "PerAtomCorrectionOutcome",
    "correct_per_atom",
]
