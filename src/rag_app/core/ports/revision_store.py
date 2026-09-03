"""不可变 IndexRevision 控制面同步端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rag_app.core.models import (
    Chunk,
    ChunkEmbeddingState,
    EmbeddingSlotIdentity,
    IndexRevisionRef,
    IndexRevisionState,
    RevisionValidationEvidence,
)


class RevisionStorePort(Protocol):
    """持久化快照、Chunk、进度、验证和原子激活。"""

    def create_revision(
        self,
        revision: IndexRevisionRef,
        *,
        physical_namespace: str,
        expected_document_count: int,
        slots: Sequence[EmbeddingSlotIdentity],
        resolved_contracts: dict[str, object],
    ) -> None:
        """创建不可变 revision 控制行。

        Args:
            revision: 新 revision 身份。
            physical_namespace: 独占 Vector namespace。
            expected_document_count: 快照文档数。
            slots: required embedding slots。
            resolved_contracts: 不含路径和 secret 的 resolved 合同。

        Returns:
            无返回值。

        """
        ...

    def set_revision_state(
        self,
        revision_id: str,
        expected: IndexRevisionState,
        target: IndexRevisionState,
    ) -> None:
        """使用比较并交换语义推进合法状态。

        Args:
            revision_id: 目标 revision。
            expected: 调用方要求的当前状态。
            target: 下一状态。

        Returns:
            无返回值。

        """
        ...

    def write_chunks(self, revision_id: str, chunks: Sequence[Chunk]) -> None:
        """一次事务写权威 Chunk、Exact 和 FTS 行。

        Args:
            revision_id: staging revision。
            chunks: 已由 canonical validator 验证的 chunks。

        Returns:
            无返回值。

        """
        ...

    def set_embedding_state(  # noqa: PLR0913
        self,
        revision_id: str,
        chunk_id: str,
        slot_id: str,
        state: ChunkEmbeddingState,
        *,
        cache_key: str | None,
        attempt: int,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        """保存可重启恢复的单 Chunk/Slot 进度。

        Args:
            revision_id: 目标 revision。
            chunk_id: 目标 chunk。
            slot_id: 目标 slot。
            state: 新持久化状态。
            cache_key: 命中或生成的 cache key。
            attempt: 当前尝试序号。
            error_code: 可选稳定错误码。
            retryable: 是否可由用户显式 retry。

        Returns:
            无返回值。

        """
        ...

    def activate(
        self,
        knowledge_base_id: str,
        evidence: RevisionValidationEvidence,
        *,
        reason: str,
        trace_id: str,
    ) -> None:
        """在单一 SQLite 写事务内切换 active 指针。

        Args:
            knowledge_base_id: 目标知识库。
            evidence: 已绑定实际 Store 的验证证据。
            reason: 安全激活原因。
            trace_id: 受控 trace ID。

        Returns:
            无返回值。

        """
        ...

    def close(self) -> None:
        """幂等关闭控制面。

        Args:
            无参数；关闭当前 Store。

        Returns:
            无返回值。

        """
        ...
