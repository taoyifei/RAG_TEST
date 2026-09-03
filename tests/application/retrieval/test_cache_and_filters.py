from __future__ import annotations

import pytest

from rag_app.application.embedding_router import search_cache_key
from rag_app.application.retrieval.filters import apply_candidate_filters
from rag_app.core.errors import PolicyDenied
from rag_app.core.models import (
    ChannelHit,
    KnowledgeBaseScope,
    SearchRequest,
)

_SCOPE = KnowledgeBaseScope(
    project_id=f"prj_{'1' * 32}",
    knowledge_base_id=f"kb_{'2' * 32}",
)


def _hit(document_suffix: str = "3") -> ChannelHit:
    return ChannelHit(
        revision_id=f"irev_{'4' * 32}",
        chunk_id=f"chunk_{'5' * 32}",
        document_id=f"doc_{document_suffix * 32}",
        document_version_id=f"dver_{'6' * 32}",
        role="table",
        section_id="section-a",
        content_sha256="7" * 64,
        channel="lexical:fts5",
        rank=1,
        raw_score=-1.0,
    )


def test_access_and_metadata_filters_apply_before_hydration() -> None:
    allowed = _hit("3")
    denied = _hit("8").model_copy(
        update={"chunk_id": f"chunk_{'9' * 32}"}
    )
    request = SearchRequest(
        scope=_SCOPE,
        text="query",
        metadata_filters={"role": "table", "section_id": "section-a"},
        access_filters={"allowed_document_ids": [allowed.document_id]},
    )

    assert apply_candidate_filters((allowed, denied), request) == (allowed,)


def test_unknown_filter_fails_closed() -> None:
    request = SearchRequest(
        scope=_SCOPE,
        text="query",
        metadata_filters={"sql": "ignored"},
    )
    with pytest.raises(PolicyDenied):
        apply_candidate_filters((_hit(),), request)


def test_final_cache_key_separates_route_rerank_filters_and_context() -> None:
    common = {
        "project_id": _SCOPE.project_id,
        "knowledge_base_id": _SCOPE.knowledge_base_id,
        "active_index_revision_id": f"irev_{'4' * 32}",
        "serving_fingerprint": f"sha256:{'5' * 64}",
        "query": "private query",
    }
    primary = search_cache_key(
        **common,
        selected_embedding_slot="primary",
        rerank_mode="provider",
        metadata_filters={"role": "table"},
        access_filters={"allowed_document_ids": [f"doc_{'3' * 32}"]},
        conversation_identity="conversation-a",
        rewrite_identity="rewrite-a",
    )
    assert primary != search_cache_key(
        **common,
        selected_embedding_slot="standby",
        rerank_mode="provider",
        metadata_filters={"role": "table"},
        access_filters={"allowed_document_ids": [f"doc_{'3' * 32}"]},
        conversation_identity="conversation-a",
        rewrite_identity="rewrite-a",
    )
    assert primary != search_cache_key(
        **common,
        selected_embedding_slot="primary",
        rerank_mode="bypass",
        metadata_filters={"role": "text"},
        access_filters={"allowed_document_ids": [f"doc_{'8' * 32}"]},
        conversation_identity="conversation-b",
        rewrite_identity="rewrite-b",
    )
    assert primary != search_cache_key(
        **{
            **common,
            "active_index_revision_id": f"irev_{'a' * 32}",
        },
        selected_embedding_slot="primary",
        rerank_mode="provider",
        metadata_filters={"role": "table"},
        access_filters={"allowed_document_ids": [f"doc_{'3' * 32}"]},
        conversation_identity="conversation-a",
        rewrite_identity="rewrite-a",
    )
