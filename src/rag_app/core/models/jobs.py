"""P06 可恢复 ingestion job 与文档索引预算模型。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, StrictInt

from rag_app.core.models.common import FrozenModel


class IngestionJobState(StrEnum):
    """跨进程持久化的 ingestion job 状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    INTERRUPTED = "interrupted"


class DocumentEmbeddingBudget(FrozenModel):
    """在发送正文前检查的单 Job/Provider/Slot 硬预算。"""

    max_requests: StrictInt = Field(gt=0)
    max_tokens: StrictInt = Field(gt=0)
    max_chunks: StrictInt = Field(gt=0)
    used_requests: StrictInt = Field(default=0, ge=0)
    used_tokens: StrictInt = Field(default=0, ge=0)
    used_chunks: StrictInt = Field(default=0, ge=0)

    def reserve(
        self,
        *,
        requests: int,
        tokens: int,
        chunks: int,
    ) -> DocumentEmbeddingBudget:
        """原子语义地预留下一批调用预算。

        Args:
            requests: 即将发出的请求数。
            tokens: 本地估算 token 数。
            chunks: 即将发送的 chunk 数。

        Returns:
            包含新用量的冻结预算副本。

        Raises:
            ValueError: 任一预算不足或增量非法。

        """
        if min(requests, tokens, chunks) < 0:
            raise ValueError("预算预留增量不能为负。")
        updated = self.model_copy(
            update={
                "used_requests": self.used_requests + requests,
                "used_tokens": self.used_tokens + tokens,
                "used_chunks": self.used_chunks + chunks,
            }
        )
        if (
            updated.used_requests > updated.max_requests
            or updated.used_tokens > updated.max_tokens
            or updated.used_chunks > updated.max_chunks
        ):
            raise ValueError("文档 Embedding Job 预算不足。")
        return updated


__all__ = ["DocumentEmbeddingBudget", "IngestionJobState"]
