"""只按通道名次合并候选的 Reciprocal Rank Fusion。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from qdrant_client.http import models

__all__ = ["FusedHit", "reciprocal_rank_fusion"]


@dataclass(frozen=True, slots=True)
class FusedHit:
    """一个去重候选及其可解释 RRF 通道名次。"""

    chunk_id: str
    rrf_score: float
    channel_ranks: tuple[tuple[str, int], ...]
    payload: dict[str, object]


@dataclass(slots=True)
class _Accumulator:
    payload: dict[str, object]
    score: float
    ranks: list[tuple[str, int]]


def reciprocal_rank_fusion(
    channels: Mapping[str, Sequence[models.ScoredPoint]],
    *,
    rank_constant: int,
    limit: int,
) -> tuple[FusedHit, ...]:
    """按名次融合 dense/BM25 与原始/改写查询结果。

    Args:
        channels: 通道名到有序 Qdrant 命中的映射。
        rank_constant: RRF 正数平滑常数。
        limit: 融合后候选上限。

    Returns:
        分数降序、稳定打破并列的去重候选。

    Raises:
        ValueError: 参数无效、chunk ID 缺失/重复或 payload 漂移。

    """
    if rank_constant <= 0 or limit <= 0:
        raise ValueError("RRF rank_constant 与 limit 必须为正数。")
    if not channels:
        return ()
    accumulated: dict[str, _Accumulator] = {}
    for channel_name, points in channels.items():
        if not channel_name:
            raise ValueError("RRF 通道名不能为空。")
        seen_in_channel: set[str] = set()
        for rank, point in enumerate(points, start=1):
            payload = _require_payload(point)
            chunk_id = payload.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ValueError("RRF 候选 payload 缺少 chunk_id。")
            if chunk_id in seen_in_channel:
                raise ValueError("同一 RRF 通道含重复 chunk ID。")
            seen_in_channel.add(chunk_id)
            contribution = 1.0 / (rank_constant + rank)
            current = accumulated.get(chunk_id)
            if current is None:
                accumulated[chunk_id] = _Accumulator(
                    payload=payload,
                    score=contribution,
                    ranks=[(channel_name, rank)],
                )
                continue
            if current.payload != payload:
                raise ValueError("同一 chunk 跨通道 payload 不一致。")
            current.score += contribution
            current.ranks.append((channel_name, rank))

    fused = [
        FusedHit(
            chunk_id=chunk_id,
            rrf_score=current.score,
            channel_ranks=tuple(sorted(current.ranks)),
            payload=current.payload,
        )
        for chunk_id, current in accumulated.items()
    ]
    fused.sort(
        key=lambda item: (
            -item.rrf_score,
            min(rank for _, rank in item.channel_ranks),
            item.chunk_id,
        )
    )
    return tuple(fused[:limit])


def _require_payload(point: models.ScoredPoint) -> dict[str, object]:
    if point.payload is None:
        raise ValueError("RRF 候选缺少 payload。")
    return {str(key): value for key, value in point.payload.items()}
