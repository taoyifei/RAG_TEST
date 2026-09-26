"""公共问答流的有界 FIFO 准入队列。"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


class QueueFullError(Exception):
    """等待队列已满，尚未创建原生问答。"""


class QueueWaitTimeoutError(Exception):
    """请求在等待队列中超过上限。"""


class QueueClientDisconnectedError(Exception):
    """浏览器在等待期间断开。"""


@dataclass(slots=True)
class QaSlot:
    """持有一个运行名额，流结束或异常时只释放一次。"""

    queue: BoundedQaQueue
    released: bool = False

    def release(self) -> None:
        """把名额交给最早等待的请求。"""
        if not self.released:
            self.released = True
            self.queue._release()


class BoundedQaQueue:
    """单网关进程中最多四条问答流运行、八条等待。"""

    def __init__(
        self,
        *,
        max_active: int = 4,
        max_waiting: int = 8,
        wait_timeout_seconds: float = 300,
    ) -> None:
        if max_active < 1 or max_waiting < 0 or wait_timeout_seconds <= 0:
            raise ValueError("问答队列容量与等待时间必须有效。")
        self.max_active = max_active
        self.max_waiting = max_waiting
        self.wait_timeout_seconds = wait_timeout_seconds
        self._active = 0
        self._waiting: deque[asyncio.Future[None]] = deque()

    def snapshot(self) -> dict[str, int]:
        """返回不包含用户或问题内容的当前运行容量。"""
        return {
            "active": self._active,
            "waiting": len(self._waiting),
            "max_active": self.max_active,
            "max_waiting": self.max_waiting,
        }

    async def acquire(
        self,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> QaSlot:
        """按先到先服务取得名额；满额、超时、断线均不占用名额。"""
        if self._active < self.max_active and not self._waiting:
            self._active += 1
            return QaSlot(self)
        if len(self._waiting) >= self.max_waiting:
            raise QueueFullError

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.wait_timeout_seconds
        waiter: asyncio.Future[None] = loop.create_future()
        self._waiting.append(waiter)
        delivered = False
        try:
            while not waiter.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise QueueWaitTimeoutError
                try:
                    await asyncio.wait_for(
                        asyncio.shield(waiter), timeout=min(0.5, remaining)
                    )
                except TimeoutError:
                    if is_disconnected is not None and await is_disconnected():
                        raise QueueClientDisconnectedError from None
            if is_disconnected is not None and await is_disconnected():
                raise QueueClientDisconnectedError
            delivered = True
            return QaSlot(self)
        finally:
            if not delivered:
                if waiter.done() and not waiter.cancelled():
                    self._release()
                else:
                    waiter.cancel()
                    self._waiting.remove(waiter)

    def _release(self) -> None:
        while self._waiting:
            waiter = self._waiting.popleft()
            if not waiter.done():
                # 名额直接移交，active 计数不变。
                waiter.set_result(None)
                return
        self._active -= 1


__all__ = [
    "BoundedQaQueue",
    "QaSlot",
    "QueueClientDisconnectedError",
    "QueueFullError",
    "QueueWaitTimeoutError",
]
