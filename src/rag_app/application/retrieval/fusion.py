"""不混加 raw score 的可解释 Reciprocal Rank Fusion。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import ChannelHit, FusedCandidate, RrfContribution
from rag_app.core.models.search import RetrievalOrigin


def reciprocal_rank_fusion(  # noqa: PLR0913
    channels: Mapping[str, Sequence[ChannelHit]],
    *,
    expected_revision_id: str,
    k: int = 60,
    weights: Mapping[str, float] | None = None,
    limit: int = 48,
    required_candidate_ids: frozenset[str] = frozenset(),
) -> tuple[FusedCandidate, ...]:
    """只使用 1-based rank 融合有界候选。

    Args:
        channels: 通道名到候选序列。
        expected_revision_id: 请求 snapshot revision。
        k: P07 provisional RRF 常数。
        weights: 可选正权重；缺失通道默认一。
        limit: 最大融合候选数。
        required_candidate_ids: 必须进入后续结构闭合的有界候选 ID。

    Returns:
        带逐通道贡献且 tie 稳定的候选。

    Raises:
        IndexCorrupt: revision 或同 chunk 身份发生漂移。

    """
    if k <= 0 or limit <= 0:
        raise ValueError("RRF k 和 limit 必须为正数。")
    resolved_weights = dict(weights or {})
    aggregate: dict[str, dict[str, ChannelHit]] = {}
    origins: dict[str, list[RetrievalOrigin]] = {}
    identities = validated_channel_identities(
        channels, expected_revision_id=expected_revision_id
    )
    for variant_id, hits in channels.items():
        best_per_chunk: dict[str, ChannelHit] = {}
        for hit in hits:
            previous = best_per_chunk.get(hit.chunk_id)
            if previous is None or hit.rank < previous.rank:
                best_per_chunk[hit.chunk_id] = hit
            origins.setdefault(hit.chunk_id, []).extend(
                hit.retrieval_origins
                or (
                    RetrievalOrigin(
                        logical_channel=_channel_family(hit.channel),
                        variant_id=variant_id,
                        source_channel=hit.channel,
                        native_rank=hit.rank,
                    ),
                )
            )
        for hit in best_per_chunk.values():
            family = _channel_family(hit.channel)
            existing_hit = aggregate.setdefault(hit.chunk_id, {}).get(family)
            if existing_hit is None or hit.rank < existing_hit.rank:
                aggregate[hit.chunk_id][family] = hit
    fused: list[FusedCandidate] = []
    for chunk_id, family_hits in aggregate.items():
        hits = tuple(family_hits.values())
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
                retrieval_origins=tuple(dict.fromkeys(origins[chunk_id])),
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
    if required_candidate_ids:
        required = [
            item for item in fused if item.chunk_id in required_candidate_ids
        ]
        other = [
            item
            for item in fused
            if item.chunk_id not in required_candidate_ids
        ]
        fused = [*required, *other]
    return tuple(fused[:limit])


def candidate_source_identity(
    item: ChannelHit | FusedCandidate,
) -> tuple[str, str, str, str, str]:
    """普通融合与预融合复用同一来源身份，S/rank 不参与身份认证。

    Args:
        item: 已授权的原始或融合候选。

    Returns:
        不含排名、分数和可变认证的来源身份。

    """
    return (
        item.document_id,
        item.document_version_id,
        item.role,
        item.section_id,
        item.content_sha256,
    )


def validated_channel_identities(
    channels: Mapping[str, Sequence[ChannelHit]],
    *,
    expected_revision_id: str,
) -> dict[str, tuple[str, str, str, str, str]]:
    """对所有授权候选核对版本和身份，包括未被计票的重复 variant。

    Args:
        channels: 已通过访问边界的通道候选。
        expected_revision_id: 本请求固定的活动索引版本。

    Returns:
        由 chunk ID 索引的已核验来源身份。

    Raises:
        IndexCorrupt: 任一候选版本漂移或同 ID 的来源身份不一致。

    """
    identities: dict[str, tuple[str, str, str, str, str]] = {}
    for hits in channels.values():
        for hit in hits:
            if hit.revision_id != expected_revision_id:
                raise IndexCorrupt(
                    "RRF 候选 revision 漂移。", stage="retrieval.fuse"
                )
            identity = candidate_source_identity(hit)
            existing = identities.setdefault(hit.chunk_id, identity)
            if existing != identity:
                raise IndexCorrupt(
                    "跨通道相同 chunk 身份不一致。",
                    stage="retrieval.fuse",
                    details={"chunk_id": hit.chunk_id},
                )
    return identities


def _channel_family(channel: str) -> str:
    """同一逻辑通道的原问与改写只贡献一次 RRF 票。"""
    if channel.startswith("lexical:"):
        return "lexical"
    if channel.startswith("structural:"):
        return "structural"
    if channel.startswith("dense:"):
        return "dense"
    return channel


__all__ = ["reciprocal_rank_fusion"]
