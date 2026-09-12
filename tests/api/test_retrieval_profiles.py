"""知识库 Retrieval Profile API 与应用门禁回归。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


def _profile_payload(
    jina_connection: str, aliyun_connection: str
) -> dict[str, object]:
    return {
        "primary_connection_id": jina_connection,
        "primary_embedding_model": "jina-embeddings-v5-text-small",
        "primary_dimension": 1024,
        "primary_document_policy": {"task": "retrieval.passage"},
        "primary_query_policy": {"task": "retrieval.query"},
        "standby_connection_id": aliyun_connection,
        "standby_embedding_model": "qwen3.7-text-embedding",
        "standby_dimension": 1024,
        "standby_document_policy": {"text_type": "document"},
        "standby_query_policy": {
            "text_type": "query",
        },
        "reranker_connection_id": jina_connection,
        "reranker_model": "jina-reranker-v3.5",
        "failover_enabled": True,
        "standby_budget": {"requests": 2, "tokens": 4096},
        "retrieval_policy": {"rrf_k": 60},
        "evidence_policy": {"minimum_units": 1},
    }


def test_profile_requires_validation_then_previews_and_activates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection, aliyun_connection = create_provider_connections(
            harness
        )
        created = harness.client.post(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/retrieval-profiles",
            headers=harness.write_headers,
            json=_profile_payload(jina_connection, aliyun_connection),
        )
        created.raise_for_status()
        profile_id = str(created.json()["profile_revision_id"])
        preview = harness.client.get(
            f"/api/v1/retrieval-profiles/{profile_id}:preview"
        )
        blocked = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )

        assert preview.json()["impact"] == "NEW_INDEX_REVISION_REQUIRED"
        assert blocked.status_code == 409
        validate_five_operations(harness, jina_connection, aliyun_connection)
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        activated.raise_for_status()
        assert activated.json()["status"] == "active"
        assert harness.runtime.sdk.health().active_profile_count == 1
        issued = harness.client.post(
            "/api/v1/access-tokens",
            headers=harness.write_headers,
            json={
                "name": "过期索引查询回归",
                "scopes": ["query:read"],
                "project_id": project_id,
                "knowledge_base_id": knowledge_base_id,
            },
        )
        issued.raise_for_status()
        with TestClient(harness.client.app) as external:
            blocked_query = external.post(
                f"/api/v1/projects/{project_id}/knowledge-bases/"
                f"{knowledge_base_id}:search",
                headers={"Authorization": f"Bearer {issued.json()['token']}"},
                json={"query": "公开合成职责"},
            )
        assert blocked_query.status_code == 409
        assert blocked_query.json()["error"]["code"] == "REINDEX_REQUIRED"
    finally:
        harness.close()
