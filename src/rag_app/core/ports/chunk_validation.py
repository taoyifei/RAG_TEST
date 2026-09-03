"""持久化 Chunk 重新验证端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rag_app.core.models import Chunk, ChunkingReport, DocumentIR


class ChunkValidationPort(Protocol):
    """从持久化 IR 与 Chunk 复算结构校验结果。"""

    def validate_persisted(
        self,
        chunks: Sequence[Chunk],
        document_ir: DocumentIR,
    ) -> ChunkingReport:
        """校验持久化 Chunk 并返回确定性报告。

        Args:
            chunks: 从持久化 Store 重新读取的 Chunk。
            document_ir: 对应的持久化 Document IR。

        Returns:
            耗时归零、可与持久化报告比较的结构报告。

        Raises:
            ValueError: 任一来源、token 或 neighbor 不变量失败。

        """
        ...


__all__ = ["ChunkValidationPort"]
