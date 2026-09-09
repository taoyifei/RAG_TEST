"""不依赖线程实现的协作取消端口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class CancellationPort(Protocol):
    """允许 Application 检查取消、Adapter 登记上游关闭器。"""

    def is_cancelled(self) -> bool:
        """返回调用方是否已经请求取消。"""
        ...

    def register(self, closer: Callable[[], None]) -> int:
        """登记取消时应调用的幂等上游关闭器。"""
        ...

    def unregister(self, registration: int) -> None:
        """解除已经自然完成的上游关闭器。"""
        ...


__all__ = ["CancellationPort"]
