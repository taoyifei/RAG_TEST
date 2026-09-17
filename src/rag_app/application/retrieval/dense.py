"""P07 一次请求只选择一个 named-vector 空间的 Dense 通道。"""

from __future__ import annotations

from dataclasses import dataclass

from rag_app.core.errors import IndexCompatibilityError
from rag_app.core.models import (
    ActiveRevisionEmbeddingState,
    ActiveRevisionQuerySnapshot,
    ChannelHit,
    QueryEmbeddingRequest,
    RoutedEmbeddingResult,
)
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import QueryEmbeddingPort, VectorStorePort
from rag_app.core.ports.query_embedding import BatchQueryEmbeddingPort


@dataclass(frozen=True, slots=True)
class DenseChannelResult:
    """单一实际 route 与对应候选。"""

    hits: tuple[ChannelHit, ...]
    routed: RoutedEmbeddingResult


class DenseChannel:
    """路由 Query Role embedding 后只查询对应 named vector。"""

    def __init__(
        self,
        router: QueryEmbeddingPort,
        vector_store: VectorStorePort,
    ) -> None:
        self._router = router
        self._vector_store = vector_store

    def search(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        query: str,
        egress: EgressPolicy,
        *,
        limit: int,
    ) -> DenseChannelResult:
        """返回 primary 或 standby 中恰好一个 Dense 通道。

        Args:
            snapshot: 请求级 immutable Active Revision。
            query: 用于 QUERY role embedding 的单条文本。
            egress: 请求 scope 的默认拒绝策略。
            limit: 最大 Dense 候选数。

        Returns:
            实际 route 与同一 named-vector 空间的候选。

        """
        routed = self._router.embed_query(
            QueryEmbeddingRequest(query),
            ActiveRevisionEmbeddingState(
                topology=snapshot.topology,
                coverages=snapshot.coverages,
            ),
            egress,
        )
        return self._search_routed(snapshot, routed, limit=limit)

    def search_many(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        queries: tuple[str, ...],
        egress: EgressPolicy,
        *,
        limit: int,
    ) -> tuple[DenseChannelResult, ...]:
        """同一请求内批量嵌入并顺序查询同一 named vector。

        Args:
            snapshot: 请求级 immutable Active Revision。
            queries: 每个 Atom 的有序检索文本。
            egress: 请求作用域的出网策略。
            limit: 每个 Atom 的最大 Dense 候选数。

        Returns:
            与查询顺序一致且只使用一个 slot 的 Dense 结果。

        """
        if not queries:
            raise ValueError("Dense 批量查询不能为空。")
        revision = ActiveRevisionEmbeddingState(
            topology=snapshot.topology,
            coverages=snapshot.coverages,
        )
        if isinstance(self._router, BatchQueryEmbeddingPort):
            routed_items = self._router.embed_queries(queries, revision, egress)
        else:
            routed_items = tuple(
                self._router.embed_query(
                    QueryEmbeddingRequest(query), revision, egress
                )
                for query in queries
            )
        if len(routed_items) != len(queries):
            raise IndexCompatibilityError(
                "Query router 批量结果数量不匹配。",
                stage="retrieval.dense",
            )
        first = routed_items[0]
        if any(
            (item.selected_slot_id, item.vector_name)
            != (first.selected_slot_id, first.vector_name)
            for item in routed_items[1:]
        ):
            raise IndexCompatibilityError(
                "同一请求的 Atom 禁止跨 slot 检索。",
                stage="retrieval.dense",
            )
        return tuple(
            self._search_routed(snapshot, routed, limit=limit)
            for routed in routed_items
        )

    def _search_routed(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        routed: RoutedEmbeddingResult,
        *,
        limit: int,
    ) -> DenseChannelResult:
        slot = snapshot.vector_spec.slot(routed.selected_slot_id)
        if slot.vector_name != routed.vector_name:
            raise IndexCompatibilityError(
                "Query router 返回跨 slot vector name。",
                stage="retrieval.dense",
            )
        results = self._vector_store.search_named(
            snapshot.vector_spec,
            slot_id=routed.selected_slot_id,
            vector_name=routed.vector_name,
            query_vector=routed.vector,
            limit=limit,
            excluded_document_ids=snapshot.excluded_document_ids,
        )
        channel = f"dense:{routed.selected_slot_id}"
        return DenseChannelResult(
            routed=routed,
            hits=tuple(
                ChannelHit(
                    revision_id=snapshot.revision.index_revision_id,
                    chunk_id=item.chunk_id,
                    document_id=item.document_id,
                    document_version_id=item.document_version_id,
                    role=item.role,
                    section_id=item.section_id,
                    content_sha256=item.content_sha256,
                    channel=channel,
                    rank=item.rank,
                    raw_score=item.score,
                )
                for item in results
            ),
        )


__all__ = ["DenseChannel", "DenseChannelResult"]
