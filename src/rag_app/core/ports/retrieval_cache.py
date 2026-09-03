"""绑定实际 route 与 rerank mode 的最终查询缓存端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.models import SearchAnswerResult


class RetrievalCachePort(Protocol):
    """只使用调用方已经完成实际路由后生成的安全 key。"""

    def get(self, cache_key: str) -> SearchAnswerResult | None:
        """读取完整最终结果。

        Args:
            cache_key: 实际 route/rerank 绑定的缓存键。

        Returns:
            命中的最终结果，否则为 None。

        """
        ...

    def put(
        self,
        cache_key: str,
        result: SearchAnswerResult,
        *,
        ttl_seconds: int = 300,
    ) -> None:
        """幂等保存不含未授权正文的结果。

        Args:
            cache_key: 实际 route/rerank 绑定的缓存键。
            result: 已通过发布门的最终结果。
            ttl_seconds: 正数缓存生命周期。

        Returns:
            无返回值。

        """
        ...

    def close(self) -> None:
        """幂等释放资源。

        Args:
            无参数；关闭当前缓存。

        Returns:
            无返回值。

        """
        ...


__all__ = ["RetrievalCachePort"]
