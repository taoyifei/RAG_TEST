"""进程内、实际 route 绑定的 P07 最终结果缓存。"""

from __future__ import annotations

import threading
import time

from rag_app.core.models import SearchAnswerResult


class InMemoryRetrievalCache:
    """不持久化 query/answer，生命周期限定于显式 runtime。"""

    def __init__(self) -> None:
        self._values: dict[str, tuple[SearchAnswerResult, float]] = {}
        self._lock = threading.Lock()
        self._closed = False

    def get(self, cache_key: str) -> SearchAnswerResult | None:
        """读取完整 SHA-256 key 对应结果。

        Args:
            cache_key: 已绑定实际 route 和 rerank mode 的缓存键。

        Returns:
            命中的最终结果，否则为 None。

        """
        self._ensure_open()
        with self._lock:
            cached = self._values.get(cache_key)
            if cached is None:
                return None
            result, expires_at = cached
            if expires_at <= time.monotonic():
                self._values.pop(cache_key, None)
                return None
            return result

    def put(
        self,
        cache_key: str,
        result: SearchAnswerResult,
        *,
        ttl_seconds: int = 300,
    ) -> None:
        """保存已通过 confidence/citation 门的最终结果。

        Args:
            cache_key: 已绑定实际执行结果的缓存键。
            result: 已通过发布门的统一查询结果。
            ttl_seconds: 正数缓存生命周期。

        Returns:
            无返回值。

        """
        self._ensure_open()
        if ttl_seconds <= 0:
            raise ValueError("Retrieval cache TTL 必须为正数。")
        if result.cache_key != cache_key:
            raise ValueError("Retrieval cache key 与结果身份不一致。")
        with self._lock:
            self._values[cache_key] = (
                result,
                time.monotonic() + ttl_seconds,
            )

    def close(self) -> None:
        """清空进程内正文并幂等关闭。

        Args:
            无参数；关闭当前缓存。

        Returns:
            无返回值。

        """
        with self._lock:
            self._values.clear()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Retrieval cache 已关闭。")


__all__ = ["InMemoryRetrievalCache"]
