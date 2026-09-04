"""Product HTTP 的有界内存限流器。"""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock


class RequestRateLimiter:
    """按客户端与操作维护固定窗口调用计数。"""

    def __init__(
        self,
        *,
        window_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建进程内限流器。

        Args:
            window_seconds: 固定窗口秒数。
            clock: 可测试的单调时钟。

        Returns:
            无返回值。

        """
        if window_seconds <= 0:
            raise ValueError("限流窗口必须为正数。")
        self._window_seconds = window_seconds
        self._clock = clock
        self._calls: dict[tuple[str, str], list[float]] = {}
        self._lock = Lock()

    def allow(self, client: str, operation: str, *, limit: int) -> bool:
        """原子判断并记录一次调用。

        Args:
            client: 不持久化的客户端地址。
            operation: 固定操作类别。
            limit: 窗口内最大调用数。

        Returns:
            本次调用是否允许。

        """
        if limit <= 0:
            return False
        now = self._clock()
        key = (client, operation)
        with self._lock:
            calls = [
                value
                for value in self._calls.get(key, [])
                if now - value < self._window_seconds
            ]
            if len(calls) >= limit:
                self._calls[key] = calls
                return False
            calls.append(now)
            self._calls[key] = calls
            return True


__all__ = ["RequestRateLimiter"]
