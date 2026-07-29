"""无第三方依赖的 Trace 导出边界。"""

from __future__ import annotations

from typing import Protocol

from rag_app.tracing.models import TraceDetail

__all__ = ["NullTraceExporter", "TraceExporter"]


class TraceExporter(Protocol):
    """可映射到 OTLP/Phoenix 的最小导出接口。"""

    def export_trace(self, trace: TraceDetail) -> None:
        """导出不含 artifact 正文的 Trace 详情。

        Args:
            trace: 已持久化的根、span、决策和 artifact 引用。

        Returns:
            无返回值。

        """


class NullTraceExporter:
    """默认不执行外部导出的实现。"""

    def export_trace(self, trace: TraceDetail) -> None:
        """丢弃导出请求且不影响本地持久化。

        Args:
            trace: 已持久化的 Trace 详情。

        Returns:
            无返回值。

        """
        del trace
