"""重排后在硬条数上限内补充同版本相邻原文块。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.rerank import RerankedHit
from rag_app.tracing.reasons import DecisionCode

__all__ = [
    "NeighborDecision",
    "NeighborExpander",
    "NeighborExpansionResult",
]


class _NeighborDirection:
    """稳定的相邻方向字符串。"""

    PREVIOUS = "previous"
    NEXT = "next"


class _NeighborField:
    """相邻方向对应的 payload 字段。"""

    PREVIOUS = "previous_chunk_id"
    NEXT = "next_chunk_id"


@dataclass(frozen=True, slots=True)
class NeighborDecision:
    """一个相邻候选的确定性接受或淘汰记录。"""

    seed_chunk_id: str
    direction: str
    candidate_chunk_id: str
    selected: bool
    reason_code: DecisionCode


@dataclass(frozen=True, slots=True)
class NeighborExpansionResult:
    """相邻扩展结果及全部请求决策。"""

    hits: tuple[RerankedHit, ...]
    decisions: tuple[NeighborDecision, ...]


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
        return self.expand_with_trace(ranked_hits).hits

    def expand_with_trace(
        self,
        ranked_hits: tuple[RerankedHit, ...],
    ) -> NeighborExpansionResult:
        """扩展相邻块并返回全部确定性决策。

        Args:
            ranked_hits: 已完成模型重排的最终命中。

        Returns:
            与 `expand` 相同的候选，以及不参与算法的旁路决策。

        """
        selected = list(ranked_hits[: self._max_items])
        decisions: list[NeighborDecision] = []
        for selected_hit in selected:
            _payload_identity(selected_hit.hit.payload)
        if len(selected) >= self._max_items:
            return NeighborExpansionResult(
                hits=tuple(selected),
                decisions=(),
            )
        requests = _neighbor_requests(tuple(selected))
        payloads = self._index.fetch_active_payloads(
            tuple(chunk_id for _, _, chunk_id in requests)
        )
        seen = {item.hit.chunk_id for item in selected}
        for request_index, (seed, direction, chunk_id) in enumerate(requests):
            if len(selected) >= self._max_items:
                decisions.extend(
                    NeighborDecision(
                        seed_chunk_id=remaining_seed.hit.chunk_id,
                        direction=remaining_direction,
                        candidate_chunk_id=remaining_chunk_id,
                        selected=False,
                        reason_code=DecisionCode.CAPACITY_LIMIT,
                    )
                    for (
                        remaining_seed,
                        remaining_direction,
                        remaining_chunk_id,
                    ) in requests[request_index:]
                )
                break
            if chunk_id in seen:
                decisions.append(
                    NeighborDecision(
                        seed_chunk_id=seed.hit.chunk_id,
                        direction=direction,
                        candidate_chunk_id=chunk_id,
                        selected=False,
                        reason_code=DecisionCode.DUPLICATE,
                    )
                )
                continue
            payload = payloads.get(chunk_id)
            if payload is None:
                decisions.append(
                    NeighborDecision(
                        seed_chunk_id=seed.hit.chunk_id,
                        direction=direction,
                        candidate_chunk_id=chunk_id,
                        selected=False,
                        reason_code=DecisionCode.MISSING_PAYLOAD,
                    )
                )
                continue
            mismatch = _neighbor_mismatch_reason(
                seed.hit.payload,
                payload,
            )
            if mismatch is not None:
                decisions.append(
                    NeighborDecision(
                        seed_chunk_id=seed.hit.chunk_id,
                        direction=direction,
                        candidate_chunk_id=chunk_id,
                        selected=False,
                        reason_code=mismatch,
                    )
                )
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
            decisions.append(
                NeighborDecision(
                    seed_chunk_id=seed.hit.chunk_id,
                    direction=direction,
                    candidate_chunk_id=chunk_id,
                    selected=True,
                    reason_code=DecisionCode.ACCEPTED,
                )
            )
        return NeighborExpansionResult(
            hits=tuple(selected),
            decisions=tuple(decisions),
        )


def _neighbor_requests(
    ranked_hits: tuple[RerankedHit, ...],
) -> tuple[tuple[RerankedHit, str, str], ...]:
    requests: list[tuple[RerankedHit, str, str]] = []
    for hit in ranked_hits:
        for field, direction in (
            (_NeighborField.PREVIOUS, _NeighborDirection.PREVIOUS),
            (_NeighborField.NEXT, _NeighborDirection.NEXT),
        ):
            value = hit.hit.payload.get(field)
            if isinstance(value, str) and value:
                requests.append((hit, direction, value))
    return tuple(requests)


def _neighbor_mismatch_reason(
    seed: dict[str, object],
    neighbor: dict[str, object],
) -> DecisionCode | None:
    seed_source, seed_version, seed_group = _payload_identity(seed)
    (
        neighbor_source,
        neighbor_version,
        neighbor_group,
    ) = _payload_identity(neighbor)
    if seed_source != neighbor_source:
        return DecisionCode.SOURCE_MISMATCH
    if seed_version != neighbor_version:
        return DecisionCode.VERSION_MISMATCH
    if seed_group != neighbor_group:
        return DecisionCode.NEIGHBOR_GROUP_MISMATCH
    return None


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
