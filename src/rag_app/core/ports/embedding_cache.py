"""同步持久化 Embedding cache 端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rag_app.core.models import EmbeddingCacheIdentity, EmbeddingCacheRecord


class EmbeddingCachePort(Protocol):
    """按 scope 和完整 slot/policy 身份查询向量。"""

    def get_many(
        self,
        identities: Sequence[EmbeddingCacheIdentity],
    ) -> tuple[EmbeddingCacheRecord | None, ...]:
        """按输入顺序批量读取 cache。

        Args:
            identities: 不含正文的 cache 身份。

        Returns:
            与输入等长的命中或 None。

        """
        ...

    def put_many(self, records: Sequence[EmbeddingCacheRecord]) -> None:
        """幂等写入已校验向量。

        Args:
            records: 待持久化记录。

        Returns:
            无返回值。

        """
        ...

    def close(self) -> None:
        """幂等关闭 cache。

        Args:
            无参数；关闭当前 cache。

        Returns:
            无返回值。

        """
        ...
