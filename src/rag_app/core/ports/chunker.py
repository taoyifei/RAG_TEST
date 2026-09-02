"""同步 Chunker 端口。"""

from __future__ import annotations

from typing import Protocol

from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.models import ChunkingContext, ChunkingResult, DocumentIR


class ChunkerPort(Protocol):
    """同步、无网络且相同 IR/上下文必须幂等的分块端口。"""

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回不含 secret 的组件身份。

        Args:
            无参数；读取当前 chunker。

        Returns:
            可审计组件描述符。

        """
        ...

    def chunk(
        self,
        document_ir: DocumentIR,
        context: ChunkingContext,
    ) -> ChunkingResult:
        """把 Document IR 划分成有来源跨度的 chunks。

        Args:
            document_ir: 格式中立文档。
            context: 冻结 chunker 身份和参数。

        Returns:
            有序 chunks 与分块报告。

        """
        ...
