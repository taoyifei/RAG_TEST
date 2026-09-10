"""Rerank 后的 same-group、table 与 section 有界扩展。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.application.retrieval.semantics import source_qualifier_matches
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    HydratedChunk,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.models.chunk import Chunk
from rag_app.core.ports import EvidenceSourcePort


@dataclass(frozen=True, slots=True)
class ExpansionOutcome:
    """扩展候选和安全降级原因。"""

    candidates: tuple[RankedChunk, ...]
    degraded_reason_codes: tuple[str, ...] = ()


class NeighborExpander:
    """只在 canonical revision 内做 bounded 结构扩展。"""

    def __init__(self, source: EvidenceSourcePort) -> None:
        self._source = source

    def expand(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        mode: str,
        policy: RetrievalPolicy,
        *,
        source_qualifier: str | None = None,
    ) -> ExpansionOutcome:
        """验证双向链接并拒绝跨 document/section/group。

        Args:
            snapshot: 请求级 immutable Active Revision。
            candidates: 已重排或明确 bypass 的 canonical 候选。
            mode: none、same_group、table 或 section。
            policy: 邻居数量和 section 上限。
            source_qualifier: 原问中的可选来源限定，仅用于安排结构扩展优先级。

        Returns:
            扩展候选及可审计的安全降级原因。

        """
        if not candidates:
            return ExpansionOutcome(candidates)
        try:
            originals = _original_candidates(candidates)
        except IndexCorrupt:
            # 原命中自身冲突时不能把不一致对象交给后续去重器任选其一。
            return ExpansionOutcome((), ("NEIGHBOR_INDEX_CORRUPT",))
        candidates = tuple(originals.values())
        try:
            if mode == "none":
                expanded = candidates
            elif mode == "section":
                expanded = self._expand_sections(snapshot, candidates, policy)
            elif (
                mode == "table"
                and policy.neighbor_count
                and _has_table_coordinates(candidates)
            ):
                expanded = self._expand_table_context(
                    snapshot,
                    candidates,
                    policy,
                    source_qualifier=source_qualifier,
                )
            else:
                expanded = self._expand_links(
                    snapshot, candidates, mode, policy
                )
            return ExpansionOutcome(expanded)
        except IndexCorrupt:
            return ExpansionOutcome(candidates, ("NEIGHBOR_INDEX_CORRUPT",))

    def _expand_table_context(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
        *,
        source_qualifier: str | None,
    ) -> tuple[RankedChunk, ...]:
        """取实际章节开头表头，并在候选上限内闭合被切开的逻辑行。"""
        originals = _original_candidates(candidates)
        context: dict[str, RankedChunk] = {}
        limit = max(len(candidates), policy.fusion_candidate_limit)
        # 每个种子先闭合同组来源链，防止无关章节铺满窗口后留下半个职责行。
        seeds = _prioritize_uniquely_qualified_source(
            candidates, source_qualifier
        )
        for seed in seeds:
            expanded: tuple[RankedChunk, ...] = (seed,)
            for _ in range(min(policy.max_evidence_items, 8)):
                additional = self._expand_links(
                    snapshot, expanded, "table", policy
                )
                if len(additional) == len(expanded):
                    break
                expanded = additional[: policy.max_evidence_items + 1]
            expanded = self._expand_sections(
                snapshot,
                expanded,
                policy.model_copy(
                    update={
                        "section_chunk_limit": min(
                            1, policy.section_chunk_limit
                        )
                    }
                ),
            )
            for candidate in expanded:
                _add_context(
                    originals,
                    context,
                    candidate.hydrated,
                    seed_id=seed.hydrated.chunk.chunk_id,
                    reason="TABLE_CONTINUITY",
                )
                if len(originals) + len(context) >= limit:
                    break
            if len(originals) + len(context) >= limit:
                break
        return (*candidates, *context.values())

    def _expand_links(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        mode: str,
        policy: RetrievalPolicy,
    ) -> tuple[RankedChunk, ...]:
        ids: list[str] = []
        originals = _original_candidates(candidates)
        if policy.neighbor_count == 0:
            return candidates
        for candidate in candidates:
            chunk = candidate.hydrated.chunk
            if mode == "table" and chunk.role.value != "table":
                continue
            ids.extend(
                item
                for item in (chunk.previous_chunk_id, chunk.next_chunk_id)
                if item is not None
            )
        hydrated = self._source.hydrate_chunks(
            snapshot, tuple(dict.fromkeys(ids))
        )
        by_id = _hydrated_candidates(hydrated, originals)
        by_id.update(
            {chunk_id: item.hydrated for chunk_id, item in originals.items()}
        )
        context: dict[str, RankedChunk] = {}
        for candidate in candidates:
            chunk = candidate.hydrated.chunk
            if mode == "table" and chunk.role.value != "table":
                continue
            for neighbor_id in (chunk.previous_chunk_id, chunk.next_chunk_id):
                if neighbor_id not in by_id:
                    continue
                hydrated_item = by_id[neighbor_id]
                _validate_neighbor(chunk, hydrated_item.chunk)
                _add_context(
                    originals,
                    context,
                    hydrated_item,
                    seed_id=chunk.chunk_id,
                    reason=(
                        "TABLE_CONTINUITY"
                        if mode == "table"
                        else "SAME_GROUP_NEIGHBOR"
                    ),
                )
        return (*candidates, *context.values())

    def _expand_sections(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
    ) -> tuple[RankedChunk, ...]:
        if policy.section_chunk_limit == 0:
            return candidates
        originals = _original_candidates(candidates)
        context: dict[str, RankedChunk] = {}
        for candidate in candidates:
            chunk = candidate.hydrated.chunk
            ids = self._source.section_chunk_ids(
                snapshot,
                document_version_id=chunk.version.document_version_id,
                section_id=chunk.section_id,
                limit=policy.section_chunk_limit,
            )
            hydrated = self._source.hydrate_chunks(
                snapshot, ids[: policy.section_chunk_limit]
            )
            for item in _hydrated_candidates(hydrated, originals).values():
                _validate_boundary(chunk, item.chunk, require_group=False)
                _add_context(
                    originals,
                    context,
                    item,
                    seed_id=chunk.chunk_id,
                    reason="SECTION_SIBLING",
                )
        return (*candidates, *context.values())


def _original_candidates(
    candidates: tuple[RankedChunk, ...],
) -> dict[str, RankedChunk]:
    """在任何扩展之前冻结原命中；冲突身份或评分不静默选取。"""
    originals: dict[str, RankedChunk] = {}
    for candidate in candidates:
        chunk_id = candidate.hydrated.chunk.chunk_id
        existing = originals.get(chunk_id)
        if existing is not None and existing != candidate:
            raise IndexCorrupt(
                "同 ID 的原始候选身份或排名不一致。",
                stage="retrieval.neighbors",
            )
        if existing is None:
            originals[chunk_id] = candidate
    return originals


def _has_table_coordinates(candidates: tuple[RankedChunk, ...]) -> bool:
    return any(
        any(part.startswith("tbl:") for part in span.structural_path)
        and any(part.startswith("tr:") for part in span.structural_path)
        for candidate in candidates
        if candidate.hydrated.chunk.role.value == "table"
        for span in candidate.hydrated.chunk.source_spans
    )


def _prioritize_uniquely_qualified_source(
    candidates: tuple[RankedChunk, ...], source_qualifier: str | None
) -> tuple[RankedChunk, ...]:
    """让唯一来源限定的种子先使用表格闭合预算。

    限定未命中或同时命中多个文档版本时保持原次序，避免有限扩展窗口
    隐藏真实来源歧义。这里只调整上下文扩展顺序，不改变直接候选排名。
    """
    if source_qualifier is None:
        return candidates
    matching = tuple(
        candidate
        for candidate in candidates
        if source_qualifier_matches(
            candidate.hydrated.display_name,
            candidate.hydrated.chunk.heading_path,
            source_qualifier,
        )
    )
    document_versions = {
        candidate.hydrated.chunk.version.document_version_id
        for candidate in matching
    }
    if len(document_versions) != 1:
        return candidates
    matching_ids = {candidate.hydrated.chunk.chunk_id for candidate in matching}
    return (
        *matching,
        *(
            candidate
            for candidate in candidates
            if candidate.hydrated.chunk.chunk_id not in matching_ids
        ),
    )


def _hydrated_candidates(
    hydrated: tuple[HydratedChunk, ...],
    originals: dict[str, RankedChunk],
) -> dict[str, HydratedChunk]:
    result: dict[str, HydratedChunk] = {}
    for item in hydrated:
        chunk_id = item.chunk.chunk_id
        existing = result.get(chunk_id)
        original = originals.get(chunk_id)
        if (existing is not None and existing.chunk != item.chunk) or (
            original is not None and original.hydrated.chunk != item.chunk
        ):
            raise IndexCorrupt(
                "Hydration 返回同 ID 的不一致 canonical Chunk。",
                stage="retrieval.neighbors",
            )
        result[chunk_id] = item
    return result


def _add_context(
    originals: dict[str, RankedChunk],
    context: dict[str, RankedChunk],
    item: HydratedChunk,
    *,
    seed_id: str,
    reason: str,
) -> None:
    """原命中优先；纯上下文只记录扩展来源，不继承召回身份。"""
    chunk_id = item.chunk.chunk_id
    if chunk_id in originals:
        return
    existing = context.get(chunk_id)
    if existing is not None:
        if existing.hydrated.chunk != item.chunk:
            raise IndexCorrupt(
                "不同种子扩展出的同 ID canonical Chunk 不一致。",
                stage="retrieval.neighbors",
            )
        context[chunk_id] = existing.model_copy(
            update={
                "expansion_seed_ids": tuple(
                    sorted({*existing.expansion_seed_ids, seed_id})
                )
            }
        )
        return
    # 兼容必填正整数：纯上下文序号位于全部融合名次之后，非种子分数。
    context[chunk_id] = RankedChunk(
        hydrated=item,
        fusion_rank=max(seed.fusion_rank for seed in originals.values())
        + len(context)
        + 1,
        expansion_reason=reason,
        expansion_seed_ids=(seed_id,),
    )


def _validate_neighbor(origin_chunk: Chunk, neighbor_chunk: Chunk) -> None:
    _validate_boundary(origin_chunk, neighbor_chunk)
    linked = (
        origin_chunk.previous_chunk_id == neighbor_chunk.chunk_id
        and neighbor_chunk.next_chunk_id == origin_chunk.chunk_id
    ) or (
        origin_chunk.next_chunk_id == neighbor_chunk.chunk_id
        and neighbor_chunk.previous_chunk_id == origin_chunk.chunk_id
    )
    if not linked:
        raise IndexCorrupt(
            "Neighbor 双向链接不一致。", stage="retrieval.neighbors"
        )


def _validate_boundary(
    origin_chunk: Chunk, neighbor_chunk: Chunk, *, require_group: bool = True
) -> None:
    if (
        neighbor_chunk.version != origin_chunk.version
        or neighbor_chunk.project_id != origin_chunk.project_id
        or neighbor_chunk.knowledge_base_id != origin_chunk.knowledge_base_id
        or neighbor_chunk.index_revision_id != origin_chunk.index_revision_id
        or neighbor_chunk.section_id != origin_chunk.section_id
        or (
            require_group
            and neighbor_chunk.neighbor_group_id
            != origin_chunk.neighbor_group_id
        )
    ):
        raise IndexCorrupt(
            "Neighbor 跨越 canonical 结构边界。",
            stage="retrieval.neighbors",
        )


__all__ = ["ExpansionOutcome", "NeighborExpander"]
