"""同步 Trace Sink 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.events import TraceEvent


class TracePort(Protocol):
    """只接受脱敏结构事件且写入必须同步可见的 Trace 端口。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回 Sink 身份。

        Args:
            无参数；读取当前 Sink。

        Returns:
            可审计组件描述符。

        """
        ...

    def record(self, event: TraceEvent) -> None:
        """记录一个结构化事件。

        Args:
            event: 不含 secret 或完整正文的事件。

        Returns:
            无返回值。

        """
        ...

    def close(self) -> None:
        """幂等释放 Sink 资源。

        Args:
            无参数；关闭当前 Sink。

        Returns:
            无返回值。

        """
        ...
