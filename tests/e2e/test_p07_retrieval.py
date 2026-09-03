from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.adapters.legacy.providers import ExtractiveGenerator
from rag_app.adapters.stores import SqliteFtsStore
from rag_app.application.embedding_router import QueryEmbeddingRouter
from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.errors import (
    DenseUnavailable,
    IndexCorrupt,
    IndexNotReady,
    ProviderUnavailable,
)
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ConfidenceStatus,
    DocumentRef,
    KnowledgeBaseScope,
    RetrievalPolicy,
    SearchRequest,
)
from tests.adapters.parsers.docx_fixtures import TABLE, build_docx

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _build_active_revision(data_dir: Path) -> tuple[str, str, str]:
    project_id = deterministic_id("prj", "p07-e2e")
    knowledge_base_id = deterministic_id("kb", project_id, "p07-e2e")
    document_id = deterministic_id(
        "doc", project_id, knowledge_base_id, "table"
    )
    with build_p07_runtime(_PROFILE, data_dir=data_dir) as runtime:
        runtime.persistence.control.put_project(project_id, "P07 Project")
        runtime.persistence.control.put_knowledge_base(
            knowledge_base_id,
            project_id,
            "P07 KB",
            profile_id="dev-p06-memory",
        )
        document = DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            display_name="table.docx",
        )
        runtime.persistence.control.upsert_document(document)
        result = runtime.persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                IngestionDocument(
                    document=document,
                    content=build_docx(
                        "<w:p><w:r><w:t>订单 ABC-123</w:t></w:r></w:p>"
                        + TABLE
                    ),
                    media_type=_MEDIA_TYPE,
                ),
            ),
            idempotency_key="p07-offline-e2e",
            budgets=runtime.persistence.default_budgets(),
        )
    return project_id, knowledge_base_id, result.revision_id


def test_p07_offline_reopen_search_answer_cache_and_refusal(
    tmp_path: Path,
) -> None:
    project_id, knowledge_base_id, revision_id = _build_active_revision(
        tmp_path
    )
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )

    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        request = SearchRequest(scope=scope, text="A B")
        first = runtime.retrieval.search_and_answer(request)
        second = runtime.retrieval.search_and_answer(request)
        exact = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="ABC-123")
        )
        malicious_fts = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text='" OR *')
        )
        refused = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="zzzz-unseen-token")
        )
        trace_sink = runtime.persistence.components.trace_sink
        trace_events = trace_sink.events(first.trace_id)

    assert first.status is ConfidenceStatus.ANSWERABLE
    assert first.active_index_revision_id == revision_id
    assert first.selected_embedding_slot == "primary"
    assert first.selected_vector_name == "dense_primary"
    assert first.generation_mode == "extractive"
    assert first.answer
    assert first.evidence
    assert all(item.support_id.startswith("S") for item in first.evidence)
    assert second.cache_key == first.cache_key
    assert second.trace_id != first.trace_id
    assert exact.status is ConfidenceStatus.ANSWERABLE
    assert exact.query_kind.value == "exact_identifier"
    assert any("exact" in item.retrieval_origins for item in exact.evidence)
    assert malicious_fts.reason_code
    assert refused.status is ConfidenceStatus.INSUFFICIENT_EVIDENCE
    assert refused.answer is None
    assert refused.generation_mode == "none"
    serialized_trace = "".join(
        event.model_dump_json() for event in trace_events
    )
    assert "A B" not in serialized_trace
    assert "table.docx" not in serialized_trace
    assert "query_sha256" in serialized_trace
    assert "rank_contributions" in serialized_trace
    assert "circuit_before" in serialized_trace
    assert "embedding_text" not in serialized_trace
    assert "provider_body" not in serialized_trace


def test_active_snapshot_remains_readable_after_concurrent_activation(
    tmp_path: Path,
) -> None:
    project_id, knowledge_base_id, revision_id = _build_active_revision(
        tmp_path
    )
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        before_activation = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="A B")
        )
        snapshot = runtime.persistence.control.active_query_snapshot(
            scope,
            serving_fingerprint=runtime.persistence.components.serving_fingerprint,
            retrieval_policy=RetrievalPolicy(),
        )
        old_chunk_ids = tuple(
            item.chunk_id
            for item in runtime.persistence.control.chunk_rows(revision_id)
        )
        document = runtime.persistence.control.active_documents(
            knowledge_base_id
        )[0][0]
        replacement = runtime.persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                IngestionDocument(
                    document=document,
                    content=build_docx(
                        "<w:p><w:r><w:t>replacement</w:t></w:r></w:p>"
                    ),
                    media_type=_MEDIA_TYPE,
                ),
            ),
            idempotency_key="p07-concurrent-activation",
            budgets=runtime.persistence.default_budgets(),
        )
        hydrated = runtime.persistence.control.hydrate_chunks(
            snapshot, old_chunk_ids
        )
        after_activation = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="A B")
        )

    assert replacement.revision_id != revision_id
    assert snapshot.revision.index_revision_id == revision_id
    assert hydrated
    assert all(
        item.chunk.index_revision_id == revision_id for item in hydrated
    )
    assert before_activation.active_index_revision_id == revision_id
    assert after_activation.active_index_revision_id == replacement.revision_id
    assert before_activation.cache_key != after_activation.cache_key


def test_missing_active_revision_and_missing_chunk_fail_closed(
    tmp_path: Path,
) -> None:
    project_id = deterministic_id("prj", "p07-empty")
    knowledge_base_id = deterministic_id("kb", project_id, "p07-empty")
    empty_scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )
    with build_p07_runtime(_PROFILE, data_dir=tmp_path / "empty") as runtime:
        runtime.persistence.control.put_project(project_id, "Empty")
        runtime.persistence.control.put_knowledge_base(
            knowledge_base_id,
            project_id,
            "Empty KB",
            profile_id="dev-p06-memory",
        )
        with pytest.raises(IndexNotReady):
            runtime.retrieval.search_and_answer(
                SearchRequest(scope=empty_scope, text="query")
            )

    active_dir = tmp_path / "active"
    active_project, active_kb, _ = _build_active_revision(active_dir)
    scope = KnowledgeBaseScope(
        project_id=active_project, knowledge_base_id=active_kb
    )
    with build_p07_runtime(_PROFILE, data_dir=active_dir) as runtime:
        snapshot = runtime.persistence.control.active_query_snapshot(
            scope,
            serving_fingerprint=runtime.persistence.components.serving_fingerprint,
            retrieval_policy=RetrievalPolicy(),
        )
        with pytest.raises(IndexCorrupt):
            runtime.persistence.control.hydrate_chunks(
                snapshot, (f"chunk_{'f' * 32}",)
            )


def test_lexical_and_dense_failures_degrade_without_false_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, knowledge_base_id, _ = _build_active_revision(tmp_path)
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )

    def fail_lexical(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected lexical failure")

    monkeypatch.setattr(SqliteFtsStore, "search_candidates", fail_lexical)
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        lexical_failed = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="A B")
        )
    assert lexical_failed.status is ConfidenceStatus.INSUFFICIENT_EVIDENCE
    assert any(
        reason.startswith("LEXICAL_STORE_FAILURE")
        for reason in lexical_failed.degraded_reason_codes
    )

    monkeypatch.undo()

    def fail_dense(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise DenseUnavailable("injected dense failure", stage="test")

    monkeypatch.setattr(QueryEmbeddingRouter, "embed_query", fail_dense)
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        dense_failed = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="A B")
        )
    assert dense_failed.status is ConfidenceStatus.ANSWERABLE
    assert "DENSE_UNAVAILABLE" in dense_failed.degraded_reason_codes
    assert dense_failed.selected_embedding_slot is None


def test_reranker_and_generator_failures_have_distinct_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, knowledge_base_id, _ = _build_active_revision(tmp_path)
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )

    def fail_provider(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProviderUnavailable("injected failure", stage="test")

    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        reranker_type = type(runtime.persistence.components.reranker)
        monkeypatch.setattr(reranker_type, "rerank", fail_provider)
        rerank_failed = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="A B")
        )
    assert rerank_failed.status is ConfidenceStatus.ANSWERABLE
    assert rerank_failed.rerank_execution_mode == (
        "rerank_bypassed_provider_unavailable"
    )

    monkeypatch.undo()
    monkeypatch.setattr(ExtractiveGenerator, "generate", fail_provider)
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        generation_failed = runtime.retrieval.search_and_answer(
            SearchRequest(scope=scope, text="A B")
        )
    assert generation_failed.status is ConfidenceStatus.PROVIDER_UNAVAILABLE
    assert generation_failed.answer is None
    assert "GENERATOR_FAILURE:ProviderUnavailable" in (
        generation_failed.degraded_reason_codes
    )


def test_active_pointer_state_drift_is_index_corrupt(tmp_path: Path) -> None:
    project_id, knowledge_base_id, revision_id = _build_active_revision(
        tmp_path
    )
    scope = KnowledgeBaseScope(
        project_id=project_id, knowledge_base_id=knowledge_base_id
    )
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        connections = runtime.persistence.control._connections
        with connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE index_revisions SET state='retired' "
                "WHERE index_revision_id=?",
                (revision_id,),
            )
        with pytest.raises(IndexCorrupt):
            runtime.retrieval.search_and_answer(
                SearchRequest(scope=scope, text="A B")
            )
