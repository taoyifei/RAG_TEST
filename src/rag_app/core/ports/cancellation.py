"""不依赖线程实现的协作取消端口。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class CancellationPort(Protocol):
    """允许 Application 检查取消、Adapter 登记上游关闭器。"""

    def is_cancelled(self) -> bool:
        """返回调用方是否已经请求取消。

        Args:
            无参数；读取当前协作取消状态。

        Returns:
            已请求取消时返回 ``True``。

        """
        ...

    def register(self, closer: Callable[[], None]) -> int:
        """登记取消时应调用的幂等上游关闭器。

        Args:
            closer: 取消时调用的幂等关闭函数。

        Returns:
            可用于解除登记的进程内整数标识。

        """
        ...

    def unregister(self, registration: int) -> None:
        """解除已经自然完成的上游关闭器。

        Args:
            registration: ``register`` 返回的登记标识。

        Returns:
            无返回值；标识不存在时保持幂等。

        """
        ...


__all__ = ["CancellationPort"]
