"""重排后在硬条数上限内补充同版本相邻原文块。"""

from __future__ import annotations

from typing import Protocol

from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.rerank import RerankedHit

__all__ = ["NeighborExpander"]


class _ActivePayloadReader(Protocol):
    """相邻扩展所需的最小索引读取接口。"""

    def fetch_active_payloads(
        self,
        chunk_ids: tuple[str, ...],
    ) -> dict[str, dict[str, object]]:
        """批量读取活动 chunk payload。

        Args:
            chunk_ids: 待读取的稳定 chunk ID。

        Returns:
            以 chunk ID 为键的活动 payload。

        """


class NeighborExpander:
    """保留全部重排命中后，按命中顺序补前后相邻块。"""

    def __init__(
        self,
        index: _ActivePayloadReader,
        *,
        max_items: int,
    ) -> None:
        """保存索引与硬条数上限。

        Args:
            index: 只返回活动版本 payload 的索引。
            max_items: 扩展后的最大条数。

        Raises:
            ValueError: 上限不为正数。

        """
        if max_items <= 0:
            raise ValueError("相邻扩展上限必须为正数。")
        self._index = index
        self._max_items = max_items

    def expand(
        self,
        ranked_hits: tuple[RerankedHit, ...],
    ) -> tuple[RerankedHit, ...]:
        """按需补充相邻块，拒绝跨来源或跨版本串接。

        Args:
            ranked_hits: 已完成模型重排的最终命中。

        Returns:
            原命中在前、相邻块在后的有界列表。

        """
        selected = list(ranked_hits[: self._max_items])
        for selected_hit in selected:
            _payload_identity(selected_hit.hit.payload)
        if len(selected) >= self._max_items:
            return tuple(selected)
        requests = _neighbor_requests(tuple(selected))
        payloads = self._index.fetch_active_payloads(
            tuple(chunk_id for _, chunk_id in requests)
        )
        seen = {item.hit.chunk_id for item in selected}
        for seed, chunk_id in requests:
            if len(selected) >= self._max_items:
                break
            if chunk_id in seen:
                continue
            payload = payloads.get(chunk_id)
            if payload is None or not _same_neighbor_group(
                seed.hit.payload,
                payload,
            ):
                continue
            seen.add(chunk_id)
            selected.append(
                RerankedHit(
                    rank=len(selected) + 1,
                    rerank_score=seed.rerank_score,
                    hit=FusedHit(
                        chunk_id=chunk_id,
                        rrf_score=seed.hit.rrf_score,
                        channel_ranks=seed.hit.channel_ranks,
                        payload=payload,
                    ),
                )
            )
        return tuple(selected)


def _neighbor_requests(
    ranked_hits: tuple[RerankedHit, ...],
) -> tuple[tuple[RerankedHit, str], ...]:
    requests: list[tuple[RerankedHit, str]] = []
    for hit in ranked_hits:
        for field in ("previous_chunk_id", "next_chunk_id"):
            value = hit.hit.payload.get(field)
            if isinstance(value, str) and value:
                requests.append((hit, value))
    return tuple(requests)


def _same_neighbor_group(
    seed: dict[str, object],
    neighbor: dict[str, object],
) -> bool:
    return _payload_identity(seed) == _payload_identity(neighbor)


def _payload_identity(
    payload: dict[str, object],
) -> tuple[str, str, str]:
    values: list[str] = []
    for field in ("source_id", "doc_version", "neighbor_group_id"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"相邻 chunk payload 缺少 {field}。")
        values.append(value)
    return values[0], values[1], values[2]
