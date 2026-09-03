"""按 immutable revision 隔离的 Exact 检索端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.models import ChannelHit, ExactSearchRequest


class ExactStorePort(Protocol):
    """查询正规 identifier 表和受控 quoted phrase。"""

    def search_exact_candidates(
        self, request: ExactSearchRequest
    ) -> tuple[ChannelHit, ...]:
        """返回不携带正文且 rank 从一开始的 Exact 候选。

        Args:
            request: revision、identifier、phrase 和数量上限。

        Returns:
            受 scope/revision 约束的 Exact 候选。

        """
        ...


__all__ = ["ExactStorePort"]
