"""原始/改写查询的 dense+BM25 召回与 RRF 合并。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rag_app.clients.model_services import ExternalCallAudit, TeiEmbeddingClient
from rag_app.index import QdrantIndex
from rag_app.retrieval.bm25 import QdrantBm25Encoder
from rag_app.retrieval.filters import MetadataPolicy
from rag_app.retrieval.fusion import FusedHit, reciprocal_rank_fusion
from rag_app.retrieval.rewrite import QueryVariants
from rag_app.retrieval.routing import SoftRouteDecision, SoftRouter

__all__ = [
    "HybridRetrievalConfig",
    "HybridRetrievalResult",
    "HybridRetrievalServices",
    "HybridRetriever",
]


@dataclass(frozen=True, slots=True)
class HybridRetrievalConfig:
    """必须由冻结评测集确定的候选参数。"""

    dense_limit: int
    bm25_limit: int
    rrf_rank_constant: int
    candidate_limit: int
    query_instruction: str

    def __post_init__(self) -> None:
        """拒绝零值、负值或空 embedding 指令。"""
        if min(
            self.dense_limit,
            self.bm25_limit,
            self.rrf_rank_constant,
            self.candidate_limit,
        ) <= 0:
            raise ValueError("检索 topK、RRF 常数与候选上限必须为正数。")
        if not self.query_instruction.strip():
            raise ValueError("query_instruction 不能为空。")


@dataclass(frozen=True, slots=True)
class HybridRetrievalResult:
    """一次多查询混合召回的候选与调用计数。"""

    candidates: tuple[FusedHit, ...]
    query_count: int
    embedding_calls: int
    route_id: str | None = None
    route_confidence: float = 0.0
    route_fallback: bool = True
    calls: tuple[ExternalCallAudit, ...] = ()


@dataclass(frozen=True, slots=True)
class HybridRetrievalServices:
    """混合召回使用的索引、模型、过滤和软路由依赖。"""

    index: QdrantIndex
    embedding: TeiEmbeddingClient
    bm25: QdrantBm25Encoder
    metadata_policy: MetadataPolicy
    router: SoftRouter | None = None


class HybridRetriever:
    """把全部查询变体送入两个召回通道后按名次融合。"""

    def __init__(
        self,
        services: HybridRetrievalServices,
        config: HybridRetrievalConfig,
    ) -> None:
        """保存检索依赖与未冻结参数。

        Args:
            services: 索引、模型、元数据过滤和软路由依赖。
            config: 各通道 topK、RRF 与候选上限。

        """
        self._index = services.index
        self._embedding = services.embedding
        self._bm25 = services.bm25
        self._metadata_policy = services.metadata_policy
        self._config = config
        self._router = services.router

    def retrieve(
        self,
        variants: QueryVariants,
        *,
        as_of: datetime,
    ) -> HybridRetrievalResult:
        """召回原查询及可选改写查询并做 RRF。

        Args:
            variants: 原查询在首位的一个或两个查询。
            as_of: 有效期判断时点。

        Returns:
            去重候选及外部 embedding 请求数。

        Raises:
            ValueError: 查询变体为空。

        """
        if not variants.queries:
            raise ValueError("查询变体不能为空。")
        embedding_result = self._embedding.embed(
            variants.queries,
            instruction=self._config.query_instruction,
        )
        route = (
            self._router.route(variants.resolved_query)
            if self._router is not None
            else SoftRouteDecision(
                route_id=None,
                source_ids=(),
                confidence=0.0,
                routed=False,
            )
        )
        metadata_filter = self._metadata_policy.to_qdrant_filter(
            as_of=as_of,
            source_ids=route.source_ids,
        )
        channels = {}
        for index, (query, vector) in enumerate(
            zip(
                variants.queries,
                embedding_result.vectors,
                strict=True,
            )
        ):
            channels[f"q{index}:dense"] = self._index.query_dense(
                list(vector),
                limit=self._config.dense_limit,
                additional_filter=metadata_filter,
            )
            channels[f"q{index}:bm25"] = self._index.query_sparse(
                self._bm25.embed_query(query),
                limit=self._config.bm25_limit,
                additional_filter=metadata_filter,
            )
        candidates = reciprocal_rank_fusion(
            channels,
            rank_constant=self._config.rrf_rank_constant,
            limit=self._config.candidate_limit,
        )
        return HybridRetrievalResult(
            candidates=candidates,
            query_count=len(variants.queries),
            embedding_calls=len(embedding_result.calls),
            route_id=route.route_id,
            route_confidence=route.confidence,
            route_fallback=not route.routed,
            calls=embedding_result.calls,
        )
