"""原始/改写查询的 dense+BM25 召回与 RRF 合并。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime

from rag_app.clients.model_services import ExternalCallAudit, TeiEmbeddingClient
from rag_app.index import QdrantIndex
from rag_app.retrieval.bm25 import QdrantBm25Encoder
from rag_app.retrieval.filters import MetadataPolicy
from rag_app.retrieval.fusion import FusedHit, reciprocal_rank_fusion
from rag_app.retrieval.rewrite import QueryVariants
from rag_app.retrieval.routing import SoftRouteDecision, SoftRouter
from rag_app.tracing.models import JsonValue

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
        if (
            min(
                self.dense_limit,
                self.bm25_limit,
                self.rrf_rank_constant,
                self.candidate_limit,
            )
            <= 0
        ):
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
    trace: dict[str, JsonValue] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )


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
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """保存检索依赖与未冻结参数。

        Args:
            services: 索引、模型、元数据过滤和软路由依赖。
            config: 各通道 topK、RRF 与候选上限。
            clock: 记录独立外部阶段耗时的单调时钟。

        """
        self._index = services.index
        self._embedding = services.embedding
        self._bm25 = services.bm25
        self._metadata_policy = services.metadata_policy
        self._config = config
        self._router = services.router
        self._clock = clock

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
        embedding_started = self._clock()
        embedding_result = self._embedding.embed(
            variants.queries,
            instruction=self._config.query_instruction,
        )
        embedding_duration_ms = _duration_ms(
            embedding_started,
            self._clock(),
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
        channel_traces: list[JsonValue] = []
        for index, (query, vector) in enumerate(
            zip(
                variants.queries,
                embedding_result.vectors,
                strict=True,
            )
        ):
            dense_name = f"q{index}:dense"
            dense_started = self._clock()
            dense_points = self._index.query_dense(
                list(vector),
                limit=self._config.dense_limit,
                additional_filter=metadata_filter,
            )
            channels[dense_name] = dense_points
            channel_traces.append(
                _channel_trace(
                    name=dense_name,
                    query_variant_index=index,
                    channel_type="dense",
                    limit=self._config.dense_limit,
                    duration_ms=_duration_ms(
                        dense_started,
                        self._clock(),
                    ),
                    points=dense_points,
                )
            )
            sparse_name = f"q{index}:bm25"
            sparse_started = self._clock()
            sparse_points = self._index.query_sparse(
                self._bm25.embed_query(query),
                limit=self._config.bm25_limit,
                additional_filter=metadata_filter,
            )
            channels[sparse_name] = sparse_points
            channel_traces.append(
                _channel_trace(
                    name=sparse_name,
                    query_variant_index=index,
                    channel_type="bm25",
                    limit=self._config.bm25_limit,
                    duration_ms=_duration_ms(
                        sparse_started,
                        self._clock(),
                    ),
                    points=sparse_points,
                )
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
            trace={
                "embedding_duration_ms": embedding_duration_ms,
                "embedding_query_count": len(variants.queries),
                "metadata_filter_sha256": _filter_sha256(metadata_filter),
                "route": {
                    "route_id": route.route_id,
                    "source_ids": list(route.source_ids),
                    "confidence": route.confidence,
                    "routed": route.routed,
                    "reason_code": route.reason_code.value,
                    "threshold": route.threshold,
                    "rule_scores": [
                        asdict(score) for score in route.rule_scores
                    ],
                },
                "channels": channel_traces,
                "fused": [
                    {
                        "chunk_id": hit.chunk_id,
                        "rrf_score": hit.rrf_score,
                        "fused_rank": rank,
                        "channel_ranks": [
                            {
                                "channel": name,
                                "rank": channel_rank,
                                "contribution": (
                                    1.0
                                    / (
                                        self._config.rrf_rank_constant
                                        + channel_rank
                                    )
                                ),
                            }
                            for name, channel_rank in hit.channel_ranks
                        ],
                        **_payload_metadata(hit.payload),
                    }
                    for rank, hit in enumerate(candidates, start=1)
                ],
                "rrf_rank_constant": self._config.rrf_rank_constant,
                "candidate_limit": self._config.candidate_limit,
            },
        )


def _duration_ms(started: float, finished: float) -> int:
    return max(0, round((finished - started) * 1000))


def _channel_trace(  # noqa: PLR0913
    *,
    name: str,
    query_variant_index: int,
    channel_type: str,
    limit: int,
    duration_ms: int,
    points: object,
) -> dict[str, JsonValue]:
    """规范化单个召回通道的候选与耗时诊断。

    Args:
        name: 通道在本次检索中的稳定名称。
        query_variant_index: 通道对应的查询变体序号。
        channel_type: dense 或 sparse 通道类型。
        limit: 请求通道返回的最大候选数。
        duration_ms: 通道调用的非负毫秒耗时。
        points: Qdrant 返回的候选序列。

    Returns:
        仅含候选身份、排名、分数和索引元数据的 Trace 属性。

    """
    raw_points = points if isinstance(points, (list, tuple)) else ()
    candidates: list[JsonValue] = []
    for rank, point in enumerate(raw_points, start=1):
        raw_payload = getattr(point, "payload", None)
        payload = (
            {str(key): value for key, value in raw_payload.items()}
            if isinstance(raw_payload, dict)
            else {}
        )
        raw_score = getattr(point, "score", None)
        candidates.append(
            {
                "chunk_id": str(payload.get("chunk_id", "")),
                "rank": rank,
                "raw_score": (
                    float(raw_score)
                    if isinstance(raw_score, (int, float))
                    and not isinstance(raw_score, bool)
                    else None
                ),
                **_payload_metadata(payload),
            }
        )
    return {
        "name": name,
        "query_variant_index": query_variant_index,
        "channel_type": channel_type,
        "limit": limit,
        "returned_count": len(raw_points),
        "duration_ms": duration_ms,
        "candidates": candidates,
    }


def _payload_metadata(
    payload: dict[str, object],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for field_name in (
        "source_id",
        "doc_version",
        "section_id",
        "section_path",
        "chunk_role",
        "neighbor_group_id",
    ):
        value = payload.get(field_name)
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[field_name] = value
    return result


def _filter_sha256(metadata_filter: object) -> str:
    dump = getattr(metadata_filter, "model_dump", None)
    payload = dump(mode="json") if callable(dump) else str(metadata_filter)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
