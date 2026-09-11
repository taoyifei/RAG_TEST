"""基于 canonical 文档结构的独立、有界检索通道。"""

from __future__ import annotations

from typing import cast

from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChannelHit,
    QueryAnalysis,
    StructuralSearchRequest,
)
from rag_app.core.ports import LexicalStorePort


class StructuralChannel:
    """将共享 typed semantics 投影到 Store 的结构检索合同。"""

    def __init__(self, store: LexicalStorePort) -> None:
        self._store = store

    def search(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        analysis: QueryAnalysis,
        *,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        """返回具有独立 rank/reason 的 canonical Chunk 身份。

        Args:
            snapshot: 请求级不可变活动 Revision。
            analysis: Planner、Evidence 和生成共同消费的分析。
            limit: structural 通道最大候选数。

        Returns:
            Store 支持结构检索时返回候选；兼容旧 Store 时为空。

        """
        search = getattr(self._store, "search_structural_candidates", None)
        if not callable(search):
            return ()
        semantics = analysis.semantics
        result = search(
            StructuralSearchRequest(
                revision=snapshot.revision,
                query=analysis.resolved_query or analysis.normalized_query,
                target=semantics.target,
                relation=semantics.relation,
                answer_type=semantics.answer_type,
                source_qualifier=semantics.source_qualifier,
                context_qualifier=semantics.context_qualifier,
                limit=limit,
            )
        )
        return cast(tuple[ChannelHit, ...], result)


__all__ = ["StructuralChannel"]
