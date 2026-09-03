"""P06 仅补 missing 的文档 Embedding 与持久化进度服务。"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Protocol

from rag_app.core.capabilities import ProviderMode
from rag_app.core.errors import PolicyDenied, RagError
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    CacheScope,
    Chunk,
    ChunkEmbeddingState,
    DocumentEmbeddingBudget,
    EmbeddingCacheIdentity,
    EmbeddingCacheRecord,
    EmbeddingRequest,
    EmbeddingRequestRole,
    EmbeddingSlotIdentity,
    content_sha256,
)
from rag_app.core.ports import EmbeddingCachePort, EmbeddingPort


class EmbeddingProgressPort(Protocol):
    """DocumentEmbeddingService 所需的最小持久化进度视图。"""

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
        """保存单 Chunk/Slot 状态。

        Args:
            revision_id: 目标 Revision ID。
            chunk_id: 目标 Chunk ID。
            slot_id: 目标向量槽 ID。
            state: 最新持久化状态。
            cache_key: 可选持久化 Cache key。
            attempt: 当前尝试序号。
            error_code: 可选安全错误码。
            retryable: 是否允许显式重试。

        Returns:
            无返回值。

        """
        ...

    def record_provider_usage(  # noqa: PLR0913
        self,
        job_id: str,
        slot_id: str,
        provider_id: str,
        *,
        requests: int,
        estimated_tokens: int,
        chunks: int,
        elapsed_ms: int,
        status_category: str,
    ) -> None:
        """保存不含正文的累计预算用量。

        Args:
            job_id: 用量所属 Job。
            slot_id: 用量所属向量槽。
            provider_id: 实际 Provider 身份。
            requests: 累计请求数。
            estimated_tokens: 累计估算 Token 数。
            chunks: 累计 Chunk 数。
            elapsed_ms: 累计耗时毫秒数。
            status_category: 安全状态类别。

        Returns:
            无返回值。

        """
        ...


class DocumentEmbeddingResult:
    """保持 chunk 顺序的每 slot 向量与更新后预算。"""

    def __init__(
        self,
        vectors: Mapping[str, tuple[tuple[float, ...], ...]],
        budgets: Mapping[str, DocumentEmbeddingBudget],
    ) -> None:
        """保存不可变结果副本。

        Args:
            vectors: slot ID 到按 chunk 顺序排列的向量。
            budgets: slot ID 到最新预算使用量。

        Returns:
            无返回值。

        """
        self.vectors = dict(vectors)
        self.budgets = dict(budgets)


class DocumentEmbeddingService:
    """先查持久化 cache，再按 slot 串行补齐 missing 批次。"""

    def __init__(
        self,
        cache: EmbeddingCachePort,
        progress: EmbeddingProgressPort,
        providers: Mapping[str, EmbeddingPort],
        *,
        batch_size: int = 32,
    ) -> None:
        """注入无隐藏默认值的 cache、进度和 Provider。

        Args:
            cache: 持久化 Embedding cache。
            progress: 可重启的 Chunk/Slot 状态 Store。
            providers: slot ID 到实际 Provider。
            batch_size: 每次受控 Provider 批次大小。

        Returns:
            无返回值。

        """
        if batch_size <= 0:
            raise ValueError("Embedding batch size 必须为正数。")
        self._cache = cache
        self._progress = progress
        self._providers = dict(providers)
        self._batch_size = batch_size

    def embed_missing(  # noqa: PLR0913
        self,
        *,
        job_id: str,
        revision_id: str,
        project_id: str,
        knowledge_base_id: str,
        chunks: Sequence[Chunk],
        slots: Sequence[EmbeddingSlotIdentity],
        budgets: Mapping[str, DocumentEmbeddingBudget],
        egress_allowed_slots: frozenset[str] = frozenset(),
        attempt: int = 1,
        cache_scope: CacheScope = CacheScope.PROJECT,
    ) -> DocumentEmbeddingResult:
        """按 slot 顺序补齐 cache miss 并持久化完成批次。

        Args:
            job_id: 持久化预算归属 Job。
            revision_id: staging revision ID。
            project_id: 默认 cache scope 身份。
            knowledge_base_id: 可选 KB scope 身份。
            chunks: canonical chunk 顺序。
            slots: resolved required slots；通常 primary 后 standby。
            budgets: 每个 slot 的硬预算。
            egress_allowed_slots: 已显式授权公网的 slot IDs。
            attempt: 当前 retry 尝试序号。
            cache_scope: project、knowledge_base 或显式 global。

        Returns:
            每个 slot 的有序向量和已消费预算。

        """
        scope_id = _scope_id(cache_scope, project_id, knowledge_base_id)
        remaining_budgets = dict(budgets)
        all_vectors: dict[str, tuple[tuple[float, ...], ...]] = {}
        for slot in slots:
            provider = self._providers[slot.slot_id]
            if (
                provider.descriptor.mode is ProviderMode.REMOTE
                and slot.slot_id not in egress_allowed_slots
            ):
                raise PolicyDenied(
                    "远程文档 Embedding 未获得显式出网授权。",
                    stage="embedding.document",
                )
            identities = tuple(
                _cache_identity(cache_scope, scope_id, slot, chunk)
                for chunk in chunks
            )
            cached = list(self._cache.get_many(identities))
            missing = [
                index for index, value in enumerate(cached) if value is None
            ]
            for index, record in enumerate(cached):
                if record is not None:
                    self._progress.set_embedding_state(
                        revision_id,
                        chunks[index].chunk_id,
                        slot.slot_id,
                        ChunkEmbeddingState.CACHED,
                        cache_key=identities[index].persistent_key,
                        attempt=attempt,
                    )
            budget = remaining_budgets[slot.slot_id]
            for offset in range(0, len(missing), self._batch_size):
                positions = missing[offset : offset + self._batch_size]
                texts = tuple(
                    chunks[index].embedding_text for index in positions
                )
                estimated_tokens = sum(
                    max(1, len(text.encode("utf-8")) // 4) for text in texts
                )
                budget = budget.reserve(
                    requests=1,
                    tokens=estimated_tokens,
                    chunks=len(texts),
                )
                started = time.monotonic()
                try:
                    result = provider.embed(
                        EmbeddingRequest(
                            slot_id=slot.slot_id,
                            role=EmbeddingRequestRole.DOCUMENT,
                            texts=texts,
                        )
                    )
                except Exception as error:
                    retryable = isinstance(error, RagError) and error.retryable
                    code = (
                        error.code
                        if isinstance(error, RagError)
                        else type(error).__name__
                    )
                    for index in positions:
                        self._progress.set_embedding_state(
                            revision_id,
                            chunks[index].chunk_id,
                            slot.slot_id,
                            ChunkEmbeddingState.FAILED,
                            cache_key=identities[index].persistent_key,
                            attempt=attempt,
                            error_code=code,
                            retryable=retryable,
                        )
                    self._record_usage(
                        job_id,
                        slot,
                        budget,
                        started,
                        status_category=(
                            "failed_retryable"
                            if retryable
                            else "failed_terminal"
                        ),
                    )
                    raise
                if len(result.vectors) != len(positions):
                    raise ValueError(
                        "Provider 返回向量数量与 missing 批次不一致。"
                    )
                records = tuple(
                    EmbeddingCacheRecord(
                        identity=identities[index],
                        vector=tuple(result.vectors[position]),
                    )
                    for position, index in enumerate(positions)
                )
                self._cache.put_many(records)
                for index, record in zip(positions, records, strict=True):
                    cached[index] = record
                    self._progress.set_embedding_state(
                        revision_id,
                        chunks[index].chunk_id,
                        slot.slot_id,
                        ChunkEmbeddingState.EMBEDDED,
                        cache_key=record.identity.persistent_key,
                        attempt=attempt,
                    )
                self._record_usage(
                    job_id,
                    slot,
                    budget,
                    started,
                    status_category="completed",
                )
            if any(record is None for record in cached):
                raise RuntimeError("Embedding cache missing 合并后仍不完整。")
            all_vectors[slot.slot_id] = tuple(
                record.vector for record in cached if record is not None
            )
            remaining_budgets[slot.slot_id] = budget
        return DocumentEmbeddingResult(all_vectors, remaining_budgets)

    def _record_usage(
        self,
        job_id: str,
        slot: EmbeddingSlotIdentity,
        budget: DocumentEmbeddingBudget,
        started: float,
        *,
        status_category: str,
    ) -> None:
        self._progress.record_provider_usage(
            job_id,
            slot.slot_id,
            slot.provider_id,
            requests=budget.used_requests,
            estimated_tokens=budget.used_tokens,
            chunks=budget.used_chunks,
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            status_category=status_category,
        )


def _cache_identity(
    scope: CacheScope,
    scope_id: str,
    slot: EmbeddingSlotIdentity,
    chunk: Chunk,
) -> EmbeddingCacheIdentity:
    return EmbeddingCacheIdentity(
        scope_kind=scope,
        scope_id=scope_id,
        slot=slot,
        role=EmbeddingRequestRole.DOCUMENT,
        role_policy_identity=canonical_sha256(slot.document_request_policy),
        text_sha256=content_sha256(chunk.embedding_text),
    )


def _scope_id(
    scope: CacheScope,
    project_id: str,
    knowledge_base_id: str,
) -> str:
    if scope is CacheScope.PROJECT:
        return project_id
    if scope is CacheScope.KNOWLEDGE_BASE:
        return knowledge_base_id
    return "global"


__all__ = [
    "DocumentEmbeddingResult",
    "DocumentEmbeddingService",
    "EmbeddingProgressPort",
]
