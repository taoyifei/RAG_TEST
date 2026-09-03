"""Fusion 后从 SQLite canonical Chunk Store 批量 hydrate。"""

from __future__ import annotations

from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    FusedCandidate,
    RankedChunk,
)
from rag_app.core.ports import EvidenceSourcePort


class CandidateHydrator:
    """拒绝把 Vector/FTS payload 当成回答正文。"""

    def __init__(self, source: EvidenceSourcePort) -> None:
        self._source = source

    def hydrate(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        candidates: tuple[FusedCandidate, ...],
    ) -> tuple[RankedChunk, ...]:
        """批量回读并复核通道身份与 canonical Chunk。

        Args:
            snapshot: 请求级 immutable Active Revision。
            candidates: 已通过 RRF 的身份候选。

        Returns:
            保留 fusion rank 的 canonical hydrated chunks。

        Raises:
            IndexCorrupt: 通道 metadata 与 canonical Chunk 不一致。

        """
        rows = self._source.hydrate_chunks(
            snapshot, tuple(item.chunk_id for item in candidates)
        )
        by_id = {item.chunk.chunk_id: item for item in rows}
        result = []
        for rank, candidate in enumerate(candidates, start=1):
            hydrated = by_id[candidate.chunk_id]
            chunk = hydrated.chunk
            identity = (
                chunk.version.document_id,
                chunk.version.document_version_id,
                chunk.role.value,
                chunk.section_id,
                chunk.content_sha256,
            )
            expected = (
                candidate.document_id,
                candidate.document_version_id,
                candidate.role,
                candidate.section_id,
                candidate.content_sha256,
            )
            if identity != expected:
                raise IndexCorrupt(
                    "候选 metadata 与 canonical Chunk 不一致。",
                    stage="retrieval.hydrate",
                    details={"chunk_id": candidate.chunk_id},
                )
            result.append(
                RankedChunk(
                    hydrated=hydrated,
                    fusion_rank=rank,
                    must_keep=candidate.must_keep,
                    contributions=candidate.contributions,
                )
            )
        return tuple(result)


__all__ = ["CandidateHydrator"]
