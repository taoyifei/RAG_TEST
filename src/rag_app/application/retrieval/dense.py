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
