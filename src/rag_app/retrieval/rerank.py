"""严格 reranker 阶段与稳定候选排序。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.clients.model_services import (
    ExternalCallAudit,
    RerankerClient,
)
from rag_app.retrieval.fusion import FusedHit

__all__ = [
    "RerankConfig",
    "RerankStage",
    "RerankStageResult",
    "RerankedHit",
]


@dataclass(frozen=True, slots=True)
class RerankConfig:
    """待冻结的首轮、最终与邻块扩展上限。"""

    candidate_limit: int
    final_limit: int
    max_final_limit: int

    def __post_init__(self) -> None:
        """要求 `final <= max_final <= candidate`。"""
        if not (
            0
            < self.final_limit
            <= self.max_final_limit
            <= self.candidate_limit
        ):
            raise ValueError(
                "必须满足 0 < final_limit <= max_final_limit "
                "<= candidate_limit。"
            )


@dataclass(frozen=True, slots=True)
class RerankedHit:
    """一个带模型分与最终名次的融合候选。"""

    rank: int
    rerank_score: float
    hit: FusedHit


@dataclass(frozen=True, slots=True)
class RerankStageResult:
    """reranker 输出与非敏感外部调用审计。"""

    hits: tuple[RerankedHit, ...]
    call: ExternalCallAudit | None

    @property
    def call_count(self) -> int:
        """返回是否发生一次 reranker 调用。"""
        return 0 if self.call is None else 1


class RerankStage:
    """用完整 embedding 上下文重排有限 RRF 候选。"""

    def __init__(
        self,
        client: RerankerClient,
        config: RerankConfig,
    ) -> None:
        """保存 reranker 客户端与候选上限。

        Args:
            client: 严格内部 reranker 客户端。
            config: 首轮、最终和最大证据数。

        """
        self._client = client
        self.config = config

    def rerank(
        self,
        query: str,
        candidates: tuple[FusedHit, ...],
    ) -> RerankStageResult:
        """按模型分重排，RRF 仅稳定打破并列。

        Args:
            query: 始终使用当前原始用户问题。
            candidates: RRF 有序候选。

        Returns:
            最终 `final_limit` 条候选与调用审计。

        Raises:
            ValueError: 候选缺少 embedding_text。

        """
        selected = candidates[: self.config.candidate_limit]
        if not selected:
            return RerankStageResult(hits=(), call=None)
        documents = tuple(_embedding_text(hit) for hit in selected)
        scored = self._client.rerank(query, documents)
        ranked = [
            RerankedHit(
                rank=0,
                rerank_score=item.score,
                hit=selected[item.index],
            )
            for item in scored.items
        ]
        ranked.sort(
            key=lambda item: (
                -item.rerank_score,
                -item.hit.rrf_score,
                item.hit.chunk_id,
            )
        )
        limited = tuple(
            RerankedHit(
                rank=rank,
                rerank_score=item.rerank_score,
                hit=item.hit,
            )
            for rank, item in enumerate(
                ranked[: self.config.final_limit],
                start=1,
            )
        )
        return RerankStageResult(hits=limited, call=scored.call)


def _embedding_text(hit: FusedHit) -> str:
    value = hit.payload.get("embedding_text")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("候选 payload 缺少 embedding_text。")
    return value
