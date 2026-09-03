"""P07 版本化 FTS5 lexical 通道。"""

from __future__ import annotations

from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChannelHit,
    LexicalSearchRequest,
    QueryVariant,
)
from rag_app.core.ports import LexicalStorePort


class LexicalChannel:
    """安全处理空 token、特殊字符和至多两个 query 变体。"""

    def __init__(self, store: LexicalStorePort) -> None:
        self._store = store

    def search(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        variant: QueryVariant,
        *,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        """返回不携带正文的 FTS5 候选。

        Args:
            snapshot: 请求级 immutable Active Revision。
            variant: 原始或唯一 normalized 变体。
            limit: 最大候选数。

        Returns:
            绑定变体通道名的 FTS5 身份候选。

        """
        hits = self._store.search_candidates(
            LexicalSearchRequest(
                revision=snapshot.revision,
                query=variant.text,
                limit=limit,
            )
        )
        channel = (
            "lexical:fts5"
            if variant.kind == "original"
            else f"lexical:fts5:{variant.kind}"
        )
        return tuple(
            hit.model_copy(update={"channel": channel}) for hit in hits
        )


__all__ = ["LexicalChannel"]
