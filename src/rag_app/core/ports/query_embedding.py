"""实际调用 Provider 的同步 query embedding 端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class BatchQueryEmbeddingPort(Protocol):
    """可选批量能力；一次路由保证所有结果属于同一向量空间。"""

    def embed_queries(
        self,
        texts: tuple[str, ...],
        revision: ActiveRevisionEmbeddingState,
        egress: EgressPolicy,
    ) -> tuple[RoutedEmbeddingResult, ...]:
        """按输入顺序返回批量查询向量。

        Args:
            texts: 非空查询文本集合。
            revision: 同一 Active Revision 的 topology 和 coverage。
            egress: 请求作用域的查询 Embedding 出网策略。

        Returns:
            同一 slot 的有序结果；Provider 调用审计只由首项携带。

        """
        ...


__all__ = ["BatchQueryEmbeddingPort", "QueryEmbeddingPort"]
