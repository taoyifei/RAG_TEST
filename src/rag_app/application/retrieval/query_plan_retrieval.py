"""Root 与 Atom 两级有界融合，候选数量不随原子数线性放大。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from rag_app.application.retrieval.fusion import reciprocal_rank_fusion
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    ChannelHit,
    FusedCandidate,
    QueryAnalysis,
    RetrievalPolicy,
    RrfContribution,
)
from rag_app.core.models.query_plan import AtomCandidateLink

_ROOT_LEXICAL_SEED_LIMIT = 3


@dataclass(frozen=True, slots=True)
class QueryUnit:
    """原问或一个事实原子的独立召回单元。"""

    unit_id: str
    atom_id: str | None
    text: str
    analysis: QueryAnalysis
    weight: float


@dataclass(frozen=True, slots=True)
class QueryUnitRetrieval:
    """单元内四路 Hybrid 初召回。"""

    unit: QueryUnit
    channels: Mapping[str, tuple[ChannelHit, ...]]


@dataclass(frozen=True, slots=True)
class QueryPlanRetrievalOutcome:
    """统一重排前的唯一候选、保留种子与可追溯来源。"""

    candidates: tuple[FusedCandidate, ...]
    seed_chunk_ids: tuple[str, ...]
    links: tuple[AtomCandidateLink, ...]


def _root_seeds(
    candidates: tuple[FusedCandidate, ...],
    channels: Mapping[str, tuple[ChannelHit, ...]],
    limit: int,
) -> tuple[FusedCandidate, ...]:
    """保留原问高位词面命中，避免被其它通道挤出重排窗口。"""
    by_id = {candidate.chunk_id: candidate for candidate in candidates}
    lexical = channels.get("lexical", ())
    selected = tuple(
        by_id[hit.chunk_id]
        for hit in sorted(lexical, key=lambda hit: hit.rank)[
            : min(_ROOT_LEXICAL_SEED_LIMIT, limit)
        ]
        if hit.chunk_id in by_id
    )
    selected_ids = {candidate.chunk_id for candidate in selected}
    return (
        *selected,
        *(
            candidate
            for candidate in candidates
            if candidate.chunk_id not in selected_ids
        ),
    )[:limit]


def _atom_seeds(
    candidates: tuple[FusedCandidate, ...], limit: int
) -> tuple[FusedCandidate, ...]:
    """先保留不同文档版本的高位候选，再按原排名填满配额。"""
    diverse: list[FusedCandidate] = []
    seen_versions: set[str] = set()
    for candidate in candidates:
        if candidate.document_version_id in seen_versions:
            continue
        diverse.append(candidate)
        seen_versions.add(candidate.document_version_id)
        if len(diverse) == limit:
            break
    selected = {candidate.chunk_id for candidate in diverse}
    return (
        *diverse,
        *(item for item in candidates if item.chunk_id not in selected),
    )[:limit]


def fuse_query_units(
    retrieved: tuple[QueryUnitRetrieval, ...],
    *,
    revision_id: str,
    policy: RetrievalPolicy,
) -> QueryPlanRetrievalOutcome:
    """先在单元内 RRF，再按 Root/Atoms 总权重融合且保留种子。"""
    if not retrieved or retrieved[0].unit.unit_id != "ROOT":
        raise ValueError("QueryPlan 检索必须以 ROOT 开始。")
    unit_ids = tuple(item.unit.unit_id for item in retrieved)
    if len(set(unit_ids)) != len(unit_ids):
        raise ValueError("Query Unit ID 必须唯一。")
    links: list[AtomCandidateLink] = []
    seed_ids: list[str] = []
    aggregate: dict[str, tuple[FusedCandidate, list[RrfContribution]]] = {}
    for item in retrieved:
        fused = reciprocal_rank_fusion(
            item.channels,
            expected_revision_id=revision_id,
            k=policy.rrf_k,
            limit=policy.unit_fusion_candidate_limit,
        )
        seed_limit = (
            policy.unit_root_seed_limit
            if item.unit.unit_id == "ROOT"
            else policy.unit_atom_seed_limit
        )
        seeds = (
            _root_seeds(fused, item.channels, seed_limit)
            if item.unit.unit_id == "ROOT"
            else _atom_seeds(fused, seed_limit)
        )
        seed_ids.extend(candidate.chunk_id for candidate in seeds)
        for rank, candidate in enumerate(fused, 1):
            previous = aggregate.get(candidate.chunk_id)
            if previous is not None and _identity(previous[0]) != _identity(
                candidate
            ):
                raise IndexCorrupt(
                    "Query Unit 之间相同 chunk 身份不一致。",
                    stage="retrieval.query_unit_fuse",
                )
            contribution = RrfContribution(
                channel=item.unit.unit_id,
                rank=rank,
                weight=item.unit.weight,
                contribution=item.unit.weight / (policy.rrf_k + rank),
            )
            if previous is None:
                aggregate[candidate.chunk_id] = (candidate, [contribution])
            else:
                base = previous[0]
                aggregate[candidate.chunk_id] = (
                    base.model_copy(
                        update={
                            "best_channel_rank": min(
                                base.best_channel_rank,
                                candidate.best_channel_rank,
                            ),
                            "must_keep": base.must_keep or candidate.must_keep,
                        }
                    ),
                    previous[1],
                )
                previous[1].append(contribution)
            links.append(
                AtomCandidateLink(
                    unit_id=item.unit.unit_id,
                    atom_id=item.unit.atom_id,
                    chunk_id=candidate.chunk_id,
                    channels=tuple(
                        contribution.channel
                        for contribution in candidate.contributions
                    ),
                    best_rank=candidate.best_channel_rank,
                    unit_fusion_rank=rank,
                    best_channel_rank=candidate.best_channel_rank,
                    score=candidate.score,
                )
            )
    scored = tuple(
        candidate.model_copy(
            update={
                "score": sum(item.contribution for item in contributions),
                "contributions": tuple(contributions),
                "must_keep": candidate.must_keep,
            }
        )
        for candidate, contributions in aggregate.values()
    )
    ordered = sorted(
        scored,
        key=lambda candidate: (
            -candidate.score,
            candidate.best_channel_rank,
            candidate.chunk_id,
        ),
    )
    by_id = {candidate.chunk_id: candidate for candidate in ordered}
    required = tuple(
        by_id[chunk_id]
        for chunk_id in dict.fromkeys(seed_ids)
        if chunk_id in by_id
    )
    required_ids = {candidate.chunk_id for candidate in required}
    candidates = (
        *required,
        *(
            candidate
            for candidate in ordered
            if candidate.chunk_id not in required_ids
        ),
    )[: policy.unit_fusion_candidate_limit]
    return QueryPlanRetrievalOutcome(
        candidates=tuple(candidates),
        seed_chunk_ids=tuple(
            candidate.chunk_id for candidate in required[: len(candidates)]
        ),
        links=tuple(links),
    )


def _identity(candidate: FusedCandidate) -> tuple[str, str, str, str, str]:
    return (
        candidate.document_id,
        candidate.document_version_id,
        candidate.role,
        candidate.section_id,
        candidate.content_sha256,
    )


__all__ = [
    "QueryPlanRetrievalOutcome",
    "QueryUnit",
    "QueryUnitRetrieval",
    "fuse_query_units",
]
