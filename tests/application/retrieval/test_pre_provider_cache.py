from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    BaseResultCacheKey,
    DocumentRef,
    KnowledgeBaseScope,
    SearchRequest,
)
from tests.adapters.parsers.docx_fixtures import build_docx

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def test_cache_hit_precedes_query_embedding_and_reranker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = deterministic_id("prj", "pre-provider-cache")
    knowledge_base_id = deterministic_id(
        "kb", project_id, "pre-provider-cache"
    )
    document = DocumentRef(
        project_id=project_id,
        knowledge_base_id=knowledge_base_id,
        document_id=deterministic_id("doc", "pre-provider-cache"),
        display_name="cache.docx",
    )
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.persistence.control.put_project(project_id, "Cache Project")
        runtime.persistence.control.put_knowledge_base(
            knowledge_base_id,
            project_id,
            "Cache KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                IngestionDocument(
                    document=document,
                    content=build_docx(
                        "<w:p><w:r><w:t>订单 ABC-123</w:t></w:r></w:p>"
                    ),
                    media_type=_MEDIA_TYPE,
                ),
            ),
            idempotency_key="pre-provider-cache",
            budgets=runtime.persistence.default_budgets(),
        )
        request = SearchRequest(
            scope=KnowledgeBaseScope(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            ),
            text="ABC-123",
        )
        first = runtime.retrieval.search_and_answer(request)

        def forbidden(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("provider path must not run on cache hit")

        monkeypatch.setattr(
            runtime.persistence.components.query_embedding_router,
            "embed_query",
            forbidden,
        )
        monkeypatch.setattr(
            runtime.persistence.components.reranker,
            "rerank",
            forbidden,
        )
        second = runtime.retrieval.search_and_answer(request)

    assert not first.cache_hit
    assert second.cache_hit
    assert second.trace_id != first.trace_id
    assert second.cache_key == first.cache_key
    assert second.selected_embedding_slot == first.selected_embedding_slot
    assert second.rerank_execution_mode == first.rerank_execution_mode


def test_base_cache_key_binds_filters_without_storing_query() -> None:
    common = {
        "project_id": f"prj_{'1' * 32}",
        "knowledge_base_id": f"kb_{'2' * 32}",
        "active_revision_id": f"irev_{'3' * 32}",
        "index_fingerprint": f"sha256:{'4' * 64}",
        "serving_fingerprint": f"sha256:{'5' * 64}",
        "query_sha256": "6" * 64,
        "conversation_identity": "conversation-v1",
        "rewrite_policy_identity": f"sha256:{'7' * 64}",
        "cache_schema": 2,
    }
    first = BaseResultCacheKey(
        **common,
        metadata_filter_hash=canonical_sha256({"role": "table"}),
        access_filter_hash=canonical_sha256({"documents": ["a"]}),
    )
    second = BaseResultCacheKey(
        **common,
        metadata_filter_hash=canonical_sha256({"role": "text"}),
        access_filter_hash=canonical_sha256({"documents": ["b"]}),
    )

    assert first.persistent_key != second.persistent_key
    assert "private query" not in first.model_dump_json()
