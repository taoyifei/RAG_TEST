from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from rag_app.adapters.legacy.providers import DeterministicEmbeddingProvider
from rag_app.application.embedding_indexing import DocumentEmbeddingService
from rag_app.core.errors import ProviderUnavailable
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    CacheScope,
    Chunk,
    ChunkEmbeddingState,
    ChunkRole,
    DocumentEmbeddingBudget,
    DocumentVersionRef,
    EmbeddingCacheIdentity,
    EmbeddingCacheRecord,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    SourceSpan,
    SourceSpanKind,
)
from rag_app.core.models.common import freeze_json_object

_PROJECT_ID = deterministic_id("prj", "embedding")
_KNOWLEDGE_BASE_ID = deterministic_id("kb", "embedding")


class _MemoryCache:
    def __init__(self) -> None:
        self.records: dict[str, EmbeddingCacheRecord] = {}

    def get_many(
        self, identities: Sequence[EmbeddingCacheIdentity]
    ) -> tuple[EmbeddingCacheRecord | None, ...]:
        return tuple(
            self.records.get(item.persistent_key) for item in identities
        )

    def put_many(self, records: Sequence[EmbeddingCacheRecord]) -> None:
        self.records.update(
            (item.identity.persistent_key, item) for item in records
        )

    def close(self) -> None:
        return None


@dataclass
class _Progress:
    states: list[tuple[str, str, ChunkEmbeddingState]]
    usage: list[tuple[str, int]]

    def set_embedding_state(
        self,
        revision_id: str,
        chunk_id: str,
        slot_id: str,
        state: ChunkEmbeddingState,
        **values: object,
    ) -> None:
        del revision_id, values
        self.states.append((chunk_id, slot_id, state))

    def record_provider_usage(
        self,
        job_id: str,
        slot_id: str,
        provider_id: str,
        **values: object,
    ) -> None:
        del slot_id, provider_id
        requests = values["requests"]
        if not isinstance(requests, int):
            raise TypeError("requests 必须为 int。")
        self.usage.append((job_id, requests))


class _FlakyDeterministicProvider(DeterministicEmbeddingProvider):
    def __init__(self, slot: EmbeddingSlotIdentity) -> None:
        super().__init__(
            slot_id=slot.slot_id,
            dimension=slot.dimension,
            model=slot.model,
            request_policy_identity=canonical_sha256({"role": "document"}),
            document_request_policy_identity=canonical_sha256(
                slot.document_request_policy
            ),
            query_request_policy_identity=canonical_sha256(
                slot.query_request_policy
            ),
        )
        self.calls = 0

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.calls += 1
        if self.calls == 2:
            raise ProviderUnavailable("injected", stage="test.embedding")
        return super().embed(request)


def _slot() -> EmbeddingSlotIdentity:
    return EmbeddingSlotIdentity(
        slot_id="primary",
        role=EmbeddingSlotRole.PRIMARY,
        provider_id="deterministic",
        model="deterministic-sha256-v1",
        vector_name="dense_primary",
        dimension=8,
        normalization="l2",
        document_request_policy=freeze_json_object({"role": "document"}),
        query_request_policy=freeze_json_object({"role": "query"}),
        adapter_revision="deterministic-v1",
    )


def _chunk(suffix: str) -> Chunk:
    document_id = deterministic_id("doc", suffix)
    version = DocumentVersionRef(
        document_id=document_id,
        document_version_id=deterministic_id("dver", document_id, suffix),
        content_sha256="a" * 64,
    )
    return Chunk(
        chunk_id=deterministic_id("chunk", suffix),
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
        index_revision_id=deterministic_id("irev", "embedding"),
        version=version,
        chunker_fingerprint=canonical_sha256("test-chunker"),
        role=ChunkRole.TEXT,
        section_id=f"section-{suffix}",
        neighbor_group_id=f"group-{suffix}",
        source_spans=(
            SourceSpan(
                span_type=SourceSpanKind.SEPARATOR,
                chunk_start_char=0,
                chunk_end_char=len(f"citation {suffix}"),
                is_citable=False,
            ),
        ),
        citation_text=f"citation {suffix}",
        embedding_text=f"embedding {suffix}",
        lexical_text=f"lexical {suffix}",
        token_count=2,
        token_count_is_estimate=True,
        tokenizer_id="test",
        content_sha256=hashlib.sha256(
            f"citation {suffix}".encode()
        ).hexdigest(),
    )


def test_partial_cache_only_embeds_missing_and_preserves_order() -> None:
    slot = _slot()
    cache = _MemoryCache()
    progress = _Progress([], [])
    provider = DeterministicEmbeddingProvider(
        slot_id=slot.slot_id,
        dimension=slot.dimension,
        model=slot.model,
        request_policy_identity=canonical_sha256({"role": "document"}),
        document_request_policy_identity=canonical_sha256(
            slot.document_request_policy
        ),
        query_request_policy_identity=canonical_sha256(
            slot.query_request_policy
        ),
    )
    service = DocumentEmbeddingService(
        cache, progress, {slot.slot_id: provider}
    )
    chunks = (_chunk("a"), _chunk("b"))
    first = service.embed_missing(
        job_id=deterministic_id("job", "first"),
        revision_id=chunks[0].index_revision_id,
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
        chunks=chunks[:1],
        slots=(slot,),
        budgets={
            slot.slot_id: DocumentEmbeddingBudget(
                max_requests=2, max_tokens=100, max_chunks=2
            )
        },
    )
    second = service.embed_missing(
        job_id=deterministic_id("job", "second"),
        revision_id=chunks[0].index_revision_id,
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
        chunks=chunks,
        slots=(slot,),
        budgets={
            slot.slot_id: DocumentEmbeddingBudget(
                max_requests=2, max_tokens=100, max_chunks=2
            )
        },
        cache_scope=CacheScope.PROJECT,
    )

    assert second.vectors[slot.slot_id][0] == first.vectors[slot.slot_id][0]
    assert second.budgets[slot.slot_id].used_chunks == 1
    assert any(
        state is ChunkEmbeddingState.CACHED for _, _, state in progress.states
    )


def test_budget_is_checked_before_provider_call() -> None:
    slot = _slot()
    chunks = (_chunk("budget"),)
    provider = DeterministicEmbeddingProvider(
        slot_id=slot.slot_id,
        dimension=slot.dimension,
        model=slot.model,
        request_policy_identity=canonical_sha256({"role": "document"}),
        document_request_policy_identity=canonical_sha256(
            slot.document_request_policy
        ),
        query_request_policy_identity=canonical_sha256(
            slot.query_request_policy
        ),
    )
    provider.embed = Mock(wraps=provider.embed)  # type: ignore[method-assign]
    service = DocumentEmbeddingService(
        _MemoryCache(), _Progress([], []), {slot.slot_id: provider}
    )
    with pytest.raises(ValueError, match="预算不足"):
        service.embed_missing(
            job_id=deterministic_id("job", "budget"),
            revision_id=chunks[0].index_revision_id,
            project_id=_PROJECT_ID,
            knowledge_base_id=_KNOWLEDGE_BASE_ID,
            chunks=chunks,
            slots=(slot,),
            budgets={
                slot.slot_id: DocumentEmbeddingBudget(
                    max_requests=1, max_tokens=1, max_chunks=1
                )
            },
        )
    provider.embed.assert_not_called()  # type: ignore[attr-defined]


def test_retry_keeps_completed_batch_and_only_fills_missing() -> None:
    slot = _slot()
    chunks = (_chunk("retry-a"), _chunk("retry-b"))
    cache = _MemoryCache()
    progress = _Progress([], [])
    provider = _FlakyDeterministicProvider(slot)
    service = DocumentEmbeddingService(
        cache,
        progress,
        {slot.slot_id: provider},
        batch_size=1,
    )
    values = {
        "job_id": deterministic_id("job", "retry"),
        "revision_id": chunks[0].index_revision_id,
        "project_id": _PROJECT_ID,
        "knowledge_base_id": _KNOWLEDGE_BASE_ID,
        "chunks": chunks,
        "slots": (slot,),
        "budgets": {
            slot.slot_id: DocumentEmbeddingBudget(
                max_requests=4,
                max_tokens=100,
                max_chunks=4,
            )
        },
    }
    with pytest.raises(ProviderUnavailable):
        service.embed_missing(**values)
    assert len(cache.records) == 1

    result = service.embed_missing(**values, attempt=2)

    assert len(result.vectors[slot.slot_id]) == 2
    assert provider.calls == 3
    assert any(
        state is ChunkEmbeddingState.CACHED for _, _, state in progress.states
    )
