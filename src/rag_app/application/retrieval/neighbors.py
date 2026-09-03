"""Rerank 后的 same-group、table 与 section 有界扩展。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
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
    ) -> ExpansionOutcome:
        """验证双向链接并拒绝跨 document/section/group。

        Args:
            snapshot: 请求级 immutable Active Revision。
            candidates: 已重排或明确 bypass 的 canonical 候选。
            mode: none、same_group、table 或 section。
            policy: 邻居数量和 section 上限。

        Returns:
            扩展候选及可审计的安全降级原因。

        """
        if mode == "none" or not candidates:
            return ExpansionOutcome(candidates)
        try:
            if mode == "section":
                return ExpansionOutcome(
                    self._expand_sections(snapshot, candidates, policy)
                )
            return ExpansionOutcome(
                self._expand_links(snapshot, candidates, mode, policy)
            )
        except IndexCorrupt:
            return ExpansionOutcome(candidates, ("NEIGHBOR_INDEX_CORRUPT",))

    def _expand_links(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        mode: str,
        policy: RetrievalPolicy,
    ) -> tuple[RankedChunk, ...]:
        ids: list[str] = []
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
        by_id = {item.chunk.chunk_id: item for item in hydrated}
        output: list[RankedChunk] = []
        seen: set[str] = set()
        for candidate in candidates:
            chunk = candidate.hydrated.chunk
            sequence = []
            if chunk.previous_chunk_id in by_id:
                sequence.append(by_id[chunk.previous_chunk_id])
            sequence.append(candidate.hydrated)
            if chunk.next_chunk_id in by_id:
                sequence.append(by_id[chunk.next_chunk_id])
            for hydrated_item in sequence[: 1 + 2 * policy.neighbor_count]:
                neighbor = hydrated_item.chunk
                if neighbor.chunk_id in seen:
                    continue
                if neighbor.chunk_id != chunk.chunk_id:
                    _validate_neighbor(chunk, neighbor)
                seen.add(neighbor.chunk_id)
                output.append(
                    candidate
                    if neighbor.chunk_id == chunk.chunk_id
                    else RankedChunk(
                        hydrated=hydrated_item,
                        fusion_rank=candidate.fusion_rank,
                        expansion_reason=(
                            "TABLE_CONTINUITY"
                            if mode == "table"
                            else "SAME_GROUP_NEIGHBOR"
                        ),
                    )
                )
        return tuple(output)

    def _expand_sections(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[RankedChunk, ...],
        policy: RetrievalPolicy,
    ) -> tuple[RankedChunk, ...]:
        output = list(candidates)
        seen = {item.hydrated.chunk.chunk_id for item in candidates}
        for candidate in candidates:
            chunk = candidate.hydrated.chunk
            ids = self._source.section_chunk_ids(
                snapshot,
                document_version_id=chunk.version.document_version_id,
                section_id=chunk.section_id,
                limit=policy.section_chunk_limit,
            )
            for item in self._source.hydrate_chunks(snapshot, ids):
                if item.chunk.chunk_id in seen:
                    continue
                seen.add(item.chunk.chunk_id)
                output.append(
                    RankedChunk(
                        hydrated=item,
                        fusion_rank=candidate.fusion_rank,
                        expansion_reason="SECTION_SIBLING",
                    )
                )
        return tuple(output)


def _validate_neighbor(origin_chunk: Chunk, neighbor_chunk: Chunk) -> None:
    if (
        neighbor_chunk.version != origin_chunk.version
        or neighbor_chunk.section_id != origin_chunk.section_id
        or neighbor_chunk.neighbor_group_id != origin_chunk.neighbor_group_id
    ):
        raise IndexCorrupt(
            "Neighbor 跨越 canonical 结构边界。",
            stage="retrieval.neighbors",
        )
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


__all__ = ["ExpansionOutcome", "NeighborExpander"]
