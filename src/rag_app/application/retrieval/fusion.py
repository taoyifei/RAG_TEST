"""不混加 raw score 的可解释 Reciprocal Rank Fusion。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import ChannelHit, FusedCandidate, RrfContribution


def reciprocal_rank_fusion(
    channels: Mapping[str, Sequence[ChannelHit]],
    *,
    expected_revision_id: str,
    k: int = 60,
    weights: Mapping[str, float] | None = None,
    limit: int = 48,
) -> tuple[FusedCandidate, ...]:
    """只使用 1-based rank 融合有界候选。

    Args:
        channels: 通道名到候选序列。
        expected_revision_id: 请求 snapshot revision。
        k: P07 provisional RRF 常数。
        weights: 可选正权重；缺失通道默认一。
        limit: 最大融合候选数。

    Returns:
        带逐通道贡献且 tie 稳定的候选。

    Raises:
        IndexCorrupt: revision 或同 chunk 身份发生漂移。

    """
    if k <= 0 or limit <= 0:
        raise ValueError("RRF k 和 limit 必须为正数。")
    resolved_weights = dict(weights or {})
    aggregate: dict[str, list[ChannelHit]] = {}
    identities: dict[str, tuple[str, str, str, str, str]] = {}
    for hits in channels.values():
        best_per_chunk: dict[str, ChannelHit] = {}
        for hit in hits:
            if hit.revision_id != expected_revision_id:
                raise IndexCorrupt(
                    "RRF 候选 revision 漂移。", stage="retrieval.fuse"
                )
            previous = best_per_chunk.get(hit.chunk_id)
            if previous is None or hit.rank < previous.rank:
                best_per_chunk[hit.chunk_id] = hit
        for hit in best_per_chunk.values():
            identity = (
                hit.document_id,
                hit.document_version_id,
                hit.role,
                hit.section_id,
                hit.content_sha256,
            )
            existing = identities.setdefault(hit.chunk_id, identity)
            if existing != identity:
                raise IndexCorrupt(
                    "跨通道相同 chunk 身份不一致。",
                    stage="retrieval.fuse",
                    details={"chunk_id": hit.chunk_id},
                )
            aggregate.setdefault(hit.chunk_id, []).append(hit)
    fused: list[FusedCandidate] = []
    for chunk_id, hits in aggregate.items():
        contributions = tuple(
            RrfContribution(
                channel=hit.channel,
                rank=hit.rank,
                weight=float(resolved_weights.get(hit.channel, 1.0)),
                contribution=float(resolved_weights.get(hit.channel, 1.0))
                / (k + hit.rank),
            )
            for hit in sorted(hits, key=lambda item: (item.rank, item.channel))
        )
        fused.append(
            FusedCandidate(
                revision_id=expected_revision_id,
                chunk_id=chunk_id,
                document_id=identities[chunk_id][0],
                document_version_id=identities[chunk_id][1],
                role=identities[chunk_id][2],
                section_id=identities[chunk_id][3],
                content_sha256=identities[chunk_id][4],
                score=sum(item.contribution for item in contributions),
                best_channel_rank=min(item.rank for item in hits),
                must_keep=any(item.must_keep for item in hits),
                contributions=contributions,
            )
        )
    fused.sort(
        key=lambda item: (
            -item.score,
            item.best_channel_rank,
            -int(item.must_keep),
            item.chunk_id,
        )
    )
    return tuple(fused[:limit])


__all__ = ["reciprocal_rank_fusion"]
