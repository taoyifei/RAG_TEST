"""真实检索资料授权的 API、清单和失效回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    ProviderBudgetLedger,
)
from rag_app.core.models import EmbeddingRequest, EmbeddingRequestRole
from rag_app.core.tokenization import estimate_provider_input_tokens
from tests.api.test_query_history import _upload
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)


def _validate_jina_profile(harness: ProductHarness, connection_id: str) -> None:
    """验证单槽 Jina Profile 的文档、查询和重排合同。"""
    for operation, model, dimension, policy in (
        (
            "embedding.document",
            "jina-embeddings-v5-text-small",
            1024,
            {"task": "retrieval.passage", "normalized": True},
        ),
        (
            "embedding.query",
            "jina-embeddings-v5-text-small",
            1024,
            {"task": "retrieval.query", "normalized": True},
        ),
        ("reranking", "jina-reranker-v3.5", None, {}),
    ):
        response = harness.client.post(
            f"/api/v1/provider-connections/{connection_id}:validate",
            headers=harness.write_headers,
            json={
                "operation": operation,
                "model": model,
                "expected_dimension": dimension,
                "request_policy": policy,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "succeeded"


def _create_jina_profile(
    harness: ProductHarness,
    knowledge_base_id: str,
    connection_id: str,
) -> str:
    """创建并验证单槽 Jina 检索方案。"""
    created = harness.client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/retrieval-profiles",
        headers=harness.write_headers,
        json={
            "primary_connection_id": connection_id,
            "primary_embedding_model": "jina-embeddings-v5-text-small",
            "primary_dimension": 1024,
            "primary_document_policy": {
                "task": "retrieval.passage",
                "normalized": True,
            },
            "primary_query_policy": {
                "task": "retrieval.query",
                "normalized": True,
            },
            "reranker_connection_id": connection_id,
            "reranker_model": "jina-reranker-v3.5",
        },
    )
    assert created.status_code == 201, created.text
    _validate_jina_profile(harness, connection_id)
    return str(created.json()["profile_revision_id"])


def _approve(
    harness: ProductHarness,
    profile_id: str,
    *,
    operation_request_limits: dict[str, int] | None = None,
) -> dict[str, object]:
    """为当前测试语料创建一份短期、独立的检索授权。"""
    limits = operation_request_limits or {
        "embedding.document": 10,
        "embedding.query": 10,
        "reranking": 10,
    }
    response = harness.client.post(
        f"/api/v1/retrieval-profiles/{profile_id}/authorization:approve",
        headers=harness.write_headers,
        json={
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "request_limit": sum(limits.values()),
            "estimated_token_limit": 100_000,
            "operation_request_limits": limits,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_new_empty_knowledge_base_does_not_require_retrieval_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次空知识库没有 Index Revision 时仍可应用已验证方案。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        _, knowledge_base_id = create_project_and_knowledge_base(harness)
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )

        response = harness.client.get(
            f"/api/v1/retrieval-profiles/{profile_id}/authorization"
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "authorization_state": "NOT_REQUIRED",
            "budget_state": "AVAILABLE",
            "connection_budget_state": "READY",
            "required_operations": [
                "embedding.document",
                "embedding.query",
                "reranking",
            ],
            "estimated_document_chunks": 0,
            "estimated_document_requests_per_slot": 0,
            "estimated_document_tokens_per_slot": 0,
            "embedding_slot_count": 1,
            "manifest": None,
            "reason_codes": [],
        }
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == "active"
    finally:
        harness.close()


def test_active_documents_without_index_revision_remain_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """活动文档丢失 Index Revision 时不得借空库例外绕过授权门。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        with harness.runtime.retrieval_authorizations._connections.transaction(
            write=True
        ) as connection:
            connection.execute(
                "UPDATE knowledge_bases SET active_revision_id=NULL "
                "WHERE knowledge_base_id=?",
                (knowledge_base_id,),
            )

        response = harness.client.get(
            f"/api/v1/retrieval-profiles/{profile_id}/authorization"
        )

        assert response.status_code == 200, response.text
        assert response.json()["authorization_state"] == "BLOCKED"
        assert response.json()["reason_codes"] == [
            "RETRIEVAL_CONFIGURATION_INVALID"
        ]
    finally:
        harness.close()


def test_retrieval_authorization_binds_current_corpus_profile_and_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有管理员显式批准后才生成绑定当前文档与连接的累计账本。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        document_id = _upload(harness, project_id, knowledge_base_id)
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )

        path = f"/api/v1/retrieval-profiles/{profile_id}/authorization"
        missing = harness.client.get(path)
        assert missing.status_code == 200, missing.text
        requirements = missing.json()
        assert requirements["authorization_state"] == "MISSING"
        assert requirements["connection_budget_state"] == "READY"
        assert requirements["estimated_document_chunks"] >= 1
        assert requirements["estimated_document_requests_per_slot"] >= 1

        approval = {
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "request_limit": 30,
            "estimated_token_limit": 100_000,
            "operation_request_limits": {
                "embedding.document": 10,
                "embedding.query": 10,
                "reranking": 10,
            },
        }
        no_csrf = harness.client.post(path + ":approve", json=approval)
        assert no_csrf.status_code == 403
        approved = harness.client.post(
            path + ":approve", headers=harness.write_headers, json=approval
        )
        assert approved.status_code == 200, approved.text
        payload = approved.json()
        assert payload["authorization_state"] == "APPROVED"
        assert payload["budget_state"] == "AVAILABLE"
        assert payload["manifest"]["profile_revision_id"] == profile_id

        document = harness.runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        version = harness.runtime.sdk.get_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            str(document.current_version_id),
        )
        assert version.content_sha256 not in approved.text
        campaign = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3",
            read_only=True,
        ).campaign(payload["manifest"]["budget_campaign_id"])
        assert campaign.approved_source_hashes == (version.content_sha256,)
        assert set(campaign.allowed_operations) == {
            "embedding.document",
            "embedding.query",
            "reranking",
        }
        assert campaign.provider_request_limits == {"jina": 5}
        assert campaign.provider_token_limits == {"jina": 4096}

        connection = harness.runtime.control.get_connection(jina_connection_id)
        changed = harness.client.patch(
            f"/api/v1/provider-connections/{jina_connection_id}",
            headers=harness.write_headers,
            json={
                "expected_version": connection.configuration_version,
                "display_name": "Jina 已变更连接",
            },
        )
        assert changed.status_code == 200, changed.text
        stale = harness.client.get(path)
        assert stale.json()["authorization_state"] == "STALE_PROFILE"
        assert stale.json()["reason_codes"] == [
            "RETRIEVAL_PROFILE_BINDING_CHANGED"
        ]
    finally:
        harness.close()


def test_retrieval_estimate_uses_unique_provider_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """授权估算按实际 16 条 Provider 批次去重，不能沿用外层 32 条批次。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        store = harness.runtime.retrieval_authorizations
        snapshot = store._snapshot(knowledge_base_id)
        unique_texts = tuple(f"公开批次文本 {index}" for index in range(17))
        embedding_texts = (*unique_texts, unique_texts[0])
        monkeypatch.setattr(
            store,
            "_snapshot",
            lambda _knowledge_base_id: {
                **snapshot,
                "embedding_texts": embedding_texts,
            },
        )

        status = store.status(profile_id)

        assert status.estimated_document_chunks == 18
        assert status.estimated_document_requests_per_slot == 2
        assert status.estimated_document_tokens_per_slot == sum(
            estimate_provider_input_tokens(text) for text in unique_texts
        )
    finally:
        harness.close()


def test_retrieval_scope_rejects_revision_drift_before_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """活动 Revision 漂移必须在预算预留与 Provider 调用前失败。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        payload = _approve(harness, profile_id)
        manifest = payload["manifest"]
        assert isinstance(manifest, dict)
        campaign_id = str(manifest["budget_campaign_id"])
        ledger = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3",
            read_only=True,
        )

        entered = False
        with (
            pytest.raises(
                BudgetBlockedError, match="RETRIEVAL_REVISION_CHANGED"
            ),
            harness.runtime.retrieval_authorizations.scope(
                profile_id,
                step_id="retrieval.query",
                required_operations=("embedding.query",),
                expected_index_revision_id=f"irev_{'f' * 32}",
            ),
        ):
            entered = True
        assert not entered
        assert ledger.attempts(campaign_id) == []
    finally:
        harness.close()


def test_retrieval_operation_limit_blocks_next_query_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单项查询预算耗尽后不得借总预算余量继续发送。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        payload = _approve(
            harness,
            profile_id,
            operation_request_limits={
                "embedding.document": 10,
                "embedding.query": 1,
                "reranking": 10,
            },
        )
        manifest = payload["manifest"]
        assert isinstance(manifest, dict)
        campaign_id = str(manifest["budget_campaign_id"])
        revision_id = str(manifest["source_index_revision_id"])
        adapter = harness.runtime.providers.embedding_adapter(
            jina_connection_id,
            slot_id="primary",
            model="jina-embeddings-v5-text-small",
            dimension=1024,
            document_policy_identity="document-policy-v1",
            query_policy_identity="query-policy-v1",
        )
        try:
            with harness.runtime.retrieval_authorizations.scope(
                profile_id,
                step_id="retrieval.query",
                required_operations=("embedding.query",),
                expected_index_revision_id=revision_id,
            ):
                adapter.embed(
                    EmbeddingRequest(
                        slot_id="primary",
                        role=EmbeddingRequestRole.QUERY,
                        texts=("公开合成查询",),
                    )
                )
            with (
                pytest.raises(
                    BudgetBlockedError, match="RETRIEVAL_BUDGET_EXHAUSTED"
                ),
                harness.runtime.retrieval_authorizations.scope(
                    profile_id,
                    step_id="retrieval.query",
                    required_operations=("embedding.query",),
                    expected_index_revision_id=revision_id,
                ),
            ):
                pytest.fail("预算耗尽后不得进入 Provider 调用体")
        finally:
            adapter.close()

        ledger = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3",
            read_only=True,
        )
        attempts = ledger.attempts(campaign_id)
        assert len(attempts) == 1
        assert attempts[0]["operation"] == "embedding.query"
        assert attempts[0]["forwarded"]
        assert (
            harness.runtime.retrieval_authorizations.status(
                profile_id
            ).budget_state
            == "EXHAUSTED"
        )
    finally:
        harness.close()
