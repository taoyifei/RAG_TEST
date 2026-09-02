"""显式 named-vector 的同步 Vector Store 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models import (
    IndexRevisionRef,
    SearchHit,
    VectorSearchRequest,
    VectorWriteRequest,
)


class VectorStorePort(Protocol):
    """按 revision/slot 隔离写查并可幂等关闭的 Store。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Store 身份。

        Args:
            无参数；读取当前 Store。

        Returns:
            可审计组件描述符。

        """
        ...

    def write(self, request: VectorWriteRequest) -> None:
        """幂等写入一个显式 slot 的向量。

        Args:
            request: revision、slot、vector name、chunks 与向量。

        Returns:
            无返回值。

        """
        ...

    def search(self, request: VectorSearchRequest) -> tuple[SearchHit, ...]:
        """只搜索请求指定的 slot 和 vector name。

        Args:
            request: 显式向量空间和查询向量。

        Returns:
            Store 无关的有序命中。

        """
        ...

    def validate_revision(self, revision: IndexRevisionRef) -> None:
        """失败关闭地验证 revision 身份。

        Args:
            revision: 待验证 revision。

        Returns:
            无返回值。

        """
        ...

    def close(self) -> None:
        """幂等释放 Store 资源。

        Args:
            无参数；关闭当前 Store。

        Returns:
            无返回值。

        """
        ...
