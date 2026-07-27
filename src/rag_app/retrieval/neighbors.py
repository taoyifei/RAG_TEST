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
        """批量读取活动 chunk payload。"""


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
            if payload is None or not _same_version(seed.hit.payload, payload):
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


def _same_version(
    seed: dict[str, object],
    neighbor: dict[str, object],
) -> bool:
    source_id = seed.get("source_id")
    doc_version = seed.get("doc_version")
    return (
        isinstance(source_id, str)
        and isinstance(doc_version, str)
        and neighbor.get("source_id") == source_id
        and neighbor.get("doc_version") == doc_version
    )
