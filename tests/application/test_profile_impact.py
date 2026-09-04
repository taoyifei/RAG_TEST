"""Retrieval Profile 双指纹和三态影响判定回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.product.models import ImpactKind, RetrievalProfileDraft
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


def _draft(
    knowledge_base_id: str,
    jina_connection: str,
    aliyun_connection: str,
    *,
    instruction: str = "默认查询指令",
    rrf_k: int = 60,
) -> RetrievalProfileDraft:
    return RetrievalProfileDraft.model_validate(
        {
            "knowledge_base_id": knowledge_base_id,
            "primary_connection_id": jina_connection,
            "primary_embedding_model": "jina-embeddings-v5-text-small",
            "primary_dimension": 1024,
            "primary_document_policy": {"task": "retrieval.passage"},
            "primary_query_policy": {
                "task": "retrieval.query",
                "instruction": instruction,
            },
            "standby_connection_id": aliyun_connection,
            "standby_embedding_model": "qwen3.7-text-embedding",
            "standby_dimension": 1024,
            "standby_document_policy": {"text_type": "document"},
            "standby_query_policy": {"text_type": "query"},
            "reranker_connection_id": jina_connection,
            "reranker_model": "jina-reranker-v3.5",
            "failover_enabled": True,
            "standby_budget": {"requests": 2},
            "retrieval_policy": {"rrf_k": rrf_k},
            "evidence_policy": {"minimum_units": 1},
        }
    )


def test_profile_fingerprint_impact_and_credential_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        _, knowledge_base_id = create_project_and_knowledge_base(harness)
        jina_credential, _, jina_connection, aliyun_connection = (
            create_provider_connections(harness)
        )
        validate_five_operations(harness, jina_connection, aliyun_connection)
        first = harness.runtime.control.create_profile(
            _draft(knowledge_base_id, jina_connection, aliyun_connection)
        )
        harness.runtime.control.activate_profile(
            first.profile_revision_id,
            confirmed_impact=ImpactKind.NEW_INDEX_REVISION_REQUIRED,
        )
        unchanged = harness.runtime.control.create_profile(
            _draft(knowledge_base_id, jina_connection, aliyun_connection)
        )
        serving = harness.runtime.control.create_profile(
            _draft(
                knowledge_base_id,
                jina_connection,
                aliyun_connection,
                rrf_k=80,
            )
        )
        semantic = harness.runtime.control.create_profile(
            _draft(
                knowledge_base_id,
                jina_connection,
                aliyun_connection,
                instruction="新版查询指令",
            )
        )
        before_rotation = first.index_semantic_fingerprint
        harness.runtime.credentials.rotate(
            jina_credential, "rotated-synthetic-jina-value"
        )

        assert (
            harness.runtime.control.preview_impact(
                unchanged.profile_revision_id
            ).impact
            == "NO_REINDEX"
        )
        assert (
            harness.runtime.control.preview_impact(
                serving.profile_revision_id
            ).impact
            == "SERVING_RELOAD"
        )
        assert (
            harness.runtime.control.preview_impact(
                semantic.profile_revision_id
            ).impact
            == "NEW_INDEX_REVISION_REQUIRED"
        )
        assert first.index_semantic_fingerprint == before_rotation
    finally:
        harness.close()


def test_profile_draft_rejects_unsupported_retrieval_policy() -> None:
    with pytest.raises(ValueError, match="cache"):
        RetrievalProfileDraft.model_validate(
            {
                "knowledge_base_id": "kb_test",
                "primary_connection_id": "conn_test",
                "primary_embedding_model": "embedding-test",
                "primary_dimension": 1024,
                "primary_document_policy": {},
                "primary_query_policy": {},
                "retrieval_policy": {"cache": "revision-bound"},
            }
        )
