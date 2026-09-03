"""同步 Lexical Store 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models import (
    ChannelHit,
    Chunk,
    LexicalSearchRequest,
    SearchHit,
)


class LexicalStorePort(Protocol):
    """按 revision 隔离且返回排名语义的词法 Store。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Store 身份。

        Args:
            无参数；读取当前 Store。

        Returns:
            可审计组件描述符。

        """
        ...

    def write(self, chunks: tuple[Chunk, ...]) -> None:
        """幂等写入 chunks。

        Args:
            chunks: 带 revision 身份的 Core chunks。

        Returns:
            无返回值。

        """
        ...

    def search(self, request: LexicalSearchRequest) -> tuple[SearchHit, ...]:
        """执行词法查询并返回统一排名语义。

        Args:
            request: revision、查询和上限。

        Returns:
            Store 无关的有序命中。

        """
        ...

    def search_candidates(
        self, request: LexicalSearchRequest
    ) -> tuple[ChannelHit, ...]:
        """返回不携带正文的 FTS5 候选。

        Args:
            request: revision、query 和数量上限。

        Returns:
            受 scope/revision 约束的 1-based rank 候选。

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
