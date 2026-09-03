"""实际调用 Provider 的同步 query embedding 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models import (
    ActiveRevisionEmbeddingState,
    QueryEmbeddingRequest,
    RoutedEmbeddingResult,
)
from rag_app.core.policies import EgressPolicy


class QueryEmbeddingPort(Protocol):
    """统一 Single 与 Hot-Standby 的实际查询向量路由。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回实际调用 Router 身份。

        Args:
            无参数；读取当前 Router。

        Returns:
            可审计组件描述符。

        """
        ...

    def embed_query(
        self,
        request: QueryEmbeddingRequest,
        revision: ActiveRevisionEmbeddingState,
        egress: EgressPolicy,
    ) -> RoutedEmbeddingResult:
        """校验快照并返回恰好一个 slot 的查询向量。

        Args:
            request: 单条 query embedding 请求。
            revision: Active Revision 的 topology 和 coverage。
            egress: 默认拒绝的 query embedding 出网策略。

        Returns:
            实际调用并绑定一个 named vector 的结果。

        """
        ...


__all__ = ["QueryEmbeddingPort"]
