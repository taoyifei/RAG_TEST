"""P07 Exact Identifier 与 quoted phrase 通道。"""

from __future__ import annotations

from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChannelHit,
    ExactSearchRequest,
    QueryAnalysis,
)
from rag_app.core.ports import ExactStorePort


class ExactChannel:
    """只调用正规 Exact Store Port 的有界通道。"""

    def __init__(self, store: ExactStorePort) -> None:
        self._store = store

    def search(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        analysis: QueryAnalysis,
        *,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        """查询已分析 identifier 与显式引号短语。

        Args:
            snapshot: 请求级 immutable Active Revision。
            analysis: 已提取 identifier 和 quoted phrase 的结果。
            limit: 最大候选数。

        Returns:
            identifier 优先的身份元数据候选。

        """
        return self._store.search_exact_candidates(
            ExactSearchRequest(
                revision=snapshot.revision,
                identifiers=analysis.identifiers,
                quoted_phrases=analysis.quoted_phrases,
                limit=limit,
            )
        )


__all__ = ["ExactChannel"]
