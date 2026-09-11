"""真实检索资料授权的 API、清单和失效回归。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    ProviderBudgetLedger,
)
from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.errors import RevisionStateError
from rag_app.core.models import EmbeddingRequest, EmbeddingRequestRole, Job
from rag_app.core.tokenization import estimate_provider_input_tokens
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.adapters.parsers.docx.fixtures import build_package
from tests.api.test_query_history import _upload
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _wait_for_job(
    harness: ProductHarness, job_id: str, *, timeout: float = 5.0
) -> dict[str, object]:
    """等待公开 Job 离开 queued/running，并返回 API 投影。"""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        response = harness.client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] not in {"queued", "running"}:
            return job
        sleep(0.01)
    raise AssertionError(f"Job 未在期限内进入持久终态：{job_id}")


def _ingestion_approval(status: dict[str, object]) -> dict[str, object]:
    """按服务端给出的硬上限形成短期测试批准。"""
    return {
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "request_limit": status["recommended_request_limit"],
        "estimated_token_limit": status["recommended_estimated_token_limit"],
        "operation_request_limits": status[
            "recommended_operation_request_limits"
        ],
    }


def _provider_usage_count(harness: ProductHarness, job_id: str) -> int:
    """返回指定 Job 已持久化的 Provider 使用行数。"""
    with harness.runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT count(*) AS value FROM job_provider_usage WHERE job_id=?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return int(row["value"])


def _assert_unapproved_retry_blocked(
    harness: ProductHarness, job_id: str, *, attempt: int
) -> None:
    """确认授权前的通用重试不会消耗有限尝试次数。"""
    response = harness.client.post(
        f"/api/v1/jobs/{job_id}:retry",
        headers=harness.write_headers,
    )
    assert response.status_code == 409, response.text
    job = harness.client.get(f"/api/v1/jobs/{job_id}").json()
    assert job["attempt"] == attempt
    assert job["required_action"] == "approve_retrieval"


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
    *,
    validate: bool = True,
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
    if validate:
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


def test_regular_authorization_does_not_cross_same_corpus_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """语料摘要相同也不能把普通授权复用于另一个 Index Revision。"""
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
        store = harness.runtime.retrieval_authorizations
        snapshot = store._snapshot(knowledge_base_id)
        changed_revision = f"irev_{'e' * 32}"
        monkeypatch.setattr(
            store,
            "_snapshot",
            lambda _knowledge_base_id: {
                **snapshot,
                "active_index_revision_id": changed_revision,
            },
        )

        status = store.status(profile_id)

        assert status.authorization_state == "STALE_CORPUS"
        with (
            pytest.raises(
                BudgetBlockedError,
                match="RETRIEVAL_AUTHORIZATION_STALE_CORPUS",
            ),
            store.scope(
                profile_id,
                step_id="retrieval.query",
                required_operations=("embedding.query",),
                expected_index_revision_id=changed_revision,
            ),
        ):
            pytest.fail("普通授权不得跨 Index Revision 进入 Provider 调用体")
        ledger = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3",
            read_only=True,
        )
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


def test_first_remote_ingestion_requires_job_bound_approval_before_provider(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空库首次远程入库先持久等待授权，批准同一 Job 后才可构建。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        # 仍使用隔离 MockTransport，但走与生产相同的授权门。
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )

        queued = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="首次远程入库.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成设备的维护周期为十四天。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="first-remote-ingestion",
        )
        blocked = _wait_for_job(harness, queued.job_id)

        assert blocked["state"] == "failed_retryable"
        assert blocked["attempt"] == 0
        assert blocked["required_action"] == "approve_retrieval"
        assert blocked["revision_available"] is False
        assert blocked["error_code"] == (
            "RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED"
        )
        assert _provider_usage_count(harness, queued.job_id) == 0
        _assert_unapproved_retry_blocked(harness, queued.job_id, attempt=0)

        path = f"/api/v1/jobs/{queued.job_id}/retrieval-authorization"
        missing = harness.client.get(path)
        assert missing.status_code == 200, missing.text
        status = missing.json()
        assert status["authorization_state"] == "MISSING"
        assert status["next_action"] == "approve"
        assert status["approval_allowed"] is True
        assert status["estimation_state"] == "UNAVAILABLE_PREBUILD"
        assert status["source_document_count"] == 1
        assert status["source_size_bytes"] > 0
        assert status["target_index_revision_id"] == queued.revision_id
        assert set(status["required_operations"]) == {
            "embedding.document",
            "embedding.query",
            "reranking",
        }

        approval = _ingestion_approval(status)
        first_approval = {
            **approval,
            "request_limit": 3,
            "operation_request_limits": {
                "embedding.document": 1,
                "embedding.query": 1,
                "reranking": 1,
            },
        }
        assert (
            harness.client.post(
                path + ":approve", json=first_approval
            ).status_code
            == 403
        )
        approved = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=first_approval,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["authorization_state"] == "APPROVED"
        assert approved.json()["next_action"] == "continue"
        first_manifest_id = approved.json()["manifest"]["manifest_id"]
        idempotent = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=first_approval,
        )
        assert idempotent.status_code == 200, idempotent.text
        assert idempotent.json()["manifest"]["manifest_id"] == (
            first_manifest_id
        )
        expanded = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=approval,
        )
        assert expanded.status_code == 200, expanded.text
        assert expanded.json()["manifest"]["manifest_id"] != (first_manifest_id)
        ingestion_campaign_id = expanded.json()["manifest"][
            "budget_campaign_id"
        ]

        retried = harness.client.post(
            f"/api/v1/jobs/{queued.job_id}:retry",
            headers=harness.write_headers,
        )
        assert retried.status_code == 200, retried.text
        completed = _wait_for_job(harness, queued.job_id)
        assert completed["state"] == "succeeded"
        assert completed["revision_available"] is True
        assert completed["required_action"] is None
        assert (
            harness.runtime.p09.control.active_revision_id(knowledge_base_id)
            == completed["revision_id"]
        )
        reusable = harness.client.get(
            f"/api/v1/retrieval-profiles/{profile_id}/authorization"
        )
        assert reusable.status_code == 200, reusable.text
        assert reusable.json()["authorization_state"] == "APPROVED"
        assert reusable.json()["manifest"]["budget_campaign_id"] == (
            ingestion_campaign_id
        )

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
                expected_index_revision_id=completed["revision_id"],
            ):
                adapter.embed(
                    EmbeddingRequest(
                        slot_id="primary",
                        role=EmbeddingRequestRole.QUERY,
                        texts=("公开合成设备维护周期",),
                    )
                )
        finally:
            adapter.close()
        ledger = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3",
            read_only=True,
        )
        attempts = ledger.attempts(ingestion_campaign_id)
        assert {attempt["operation"] for attempt in attempts} == {
            "embedding.document",
            "embedding.query",
        }
        assert all(attempt["forwarded"] for attempt in attempts)
        with harness.runtime.connections.transaction() as connection:
            row = connection.execute(
                "SELECT count(*) AS value "
                "FROM retrieval_authorization_manifests "
                "WHERE profile_revision_id=?",
                (profile_id,),
            ).fetchone()
        assert row is not None and int(row["value"]) == 0
    finally:
        harness.close()


def test_next_remote_ingestion_requires_new_combined_corpus_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首版授权不能放行下一份资料；新 Job 必须绑定合并后的完整语料。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )

        first = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="第一份远程资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成设备甲每十四天维护。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="remote-ingestion-first",
        )
        assert _wait_for_job(harness, first.job_id)["required_action"] == (
            "approve_retrieval"
        )
        first_path = f"/api/v1/jobs/{first.job_id}/retrieval-authorization"
        first_status = harness.client.get(first_path).json()
        approved = harness.client.post(
            first_path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(first_status),
        )
        assert approved.status_code == 200, approved.text
        retried = harness.client.post(
            f"/api/v1/jobs/{first.job_id}:retry",
            headers=harness.write_headers,
        )
        assert retried.status_code == 200, retried.text
        first_completed = _wait_for_job(harness, first.job_id)
        assert first_completed["state"] == "succeeded"

        second = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="第二份远程资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成设备乙每二十一天维护。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="remote-ingestion-second",
        )
        second_blocked = _wait_for_job(harness, second.job_id)
        assert second_blocked["error_code"] == (
            "RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED"
        )
        assert _provider_usage_count(harness, second.job_id) == 0
        second_path = f"/api/v1/jobs/{second.job_id}/retrieval-authorization"
        second_status = harness.client.get(second_path)
        assert second_status.status_code == 200, second_status.text
        payload = second_status.json()
        assert payload["authorization_state"] == "MISSING"
        assert payload["source_document_count"] == 2
        assert (
            payload["predecessor_index_revision_id"]
            == first_completed["revision_id"]
        )
        assert (
            payload["target_index_revision_id"] == second_blocked["revision_id"]
        )

        approved = harness.client.post(
            second_path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(payload),
        )
        assert approved.status_code == 200, approved.text
        retried = harness.client.post(
            f"/api/v1/jobs/{second.job_id}:retry",
            headers=harness.write_headers,
        )
        assert retried.status_code == 200, retried.text
        second_completed = _wait_for_job(harness, second.job_id)
        assert second_completed["state"] == "succeeded"
        assert second_completed["revision_id"] != first_completed["revision_id"]
        retired_first = harness.client.get(f"/api/v1/jobs/{first.job_id}")
        assert retired_first.status_code == 200, retired_first.text
        assert retired_first.json()["revision_available"] is True
        with harness.runtime.connections.transaction() as connection:
            row = connection.execute(
                "SELECT count(*) AS value FROM revision_documents "
                "WHERE revision_id=?",
                (second_completed["revision_id"],),
            ).fetchone()
        assert row is not None and int(row["value"]) == 2
    finally:
        harness.close()


def test_concurrent_retry_advances_authorized_job_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个并发重试只能有一个原子地增加 attempt 并重新排队。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        blocked = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="并发重试资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的并发重试测试。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="concurrent-retry",
        )
        _wait_for_job(harness, blocked.job_id)
        path = f"/api/v1/jobs/{blocked.job_id}/retrieval-authorization"
        status = harness.client.get(path).json()
        approved = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(status),
        )
        assert approved.status_code == 200, approved.text
        barrier = Barrier(2)

        def retry() -> Job | Exception:
            barrier.wait()
            try:
                return harness.runtime.p09.store.retry_job(blocked.job_id)
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(executor.submit(retry) for _ in range(2))
            results = tuple(future.result() for future in futures)

        jobs = tuple(result for result in results if isinstance(result, Job))
        errors = tuple(
            result for result in results if isinstance(result, Exception)
        )
        assert len(jobs) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], RevisionStateError)
        current = harness.runtime.p09.store.get_job(blocked.job_id)
        assert current.state.value == "queued"
        assert current.attempt == 1
        with harness.runtime.connections.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM ingestion_requests WHERE job_id=?",
                (blocked.job_id,),
            ).fetchone()
        assert row is not None and row["state"] == "queued"
    finally:
        harness.close()


def test_profile_reapproval_supersedes_expired_ingestion_manifest_for_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次入库清单到期后，新的活动语料批准必须能恢复查询。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        queued = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="查询续期资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的查询续期测试。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="query-reauthorization",
        )
        _wait_for_job(harness, queued.job_id)
        job_path = f"/api/v1/jobs/{queued.job_id}/retrieval-authorization"
        job_status = harness.client.get(job_path).json()
        approved = harness.client.post(
            job_path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(job_status),
        )
        assert approved.status_code == 200, approved.text
        retried = harness.client.post(
            f"/api/v1/jobs/{queued.job_id}:retry",
            headers=harness.write_headers,
        )
        assert retried.status_code == 200, retried.text
        completed = _wait_for_job(harness, queued.job_id)
        assert completed["state"] == "succeeded"
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE retrieval_ingestion_authorization_manifests "
                "SET expires_at=? WHERE job_id=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    queued.job_id,
                ),
            )
        expired = harness.runtime.retrieval_authorizations.status(profile_id)
        assert expired.authorization_state == "EXPIRED"

        renewed = _approve(harness, profile_id)

        assert renewed["authorization_state"] == "APPROVED"
        assert renewed["manifest"]["manifest_id"].startswith("rauth_")
        renewed_campaign_id = renewed["manifest"]["budget_campaign_id"]
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
                expected_index_revision_id=completed["revision_id"],
            ):
                adapter.embed(
                    EmbeddingRequest(
                        slot_id="primary",
                        role=EmbeddingRequestRole.QUERY,
                        texts=("公开合成续期查询",),
                    )
                )
        finally:
            adapter.close()
        ledger = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3",
            read_only=True,
        )
        attempts = ledger.attempts(renewed_campaign_id)
        assert len(attempts) == 1
        assert attempts[0]["operation"] == "embedding.query"
        assert attempts[0]["forwarded"]
    finally:
        harness.close()


def test_expired_ingestion_approval_stops_retry_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job 授权到期后必须再次停在可恢复状态且不触发 Provider。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        queued = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="授权到期资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的到期授权测试。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="expired-ingestion-approval",
        )
        _wait_for_job(harness, queued.job_id)
        path = f"/api/v1/jobs/{queued.job_id}/retrieval-authorization"
        status = harness.client.get(path).json()
        approved = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(status),
        )
        assert approved.status_code == 200, approved.text
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE retrieval_ingestion_authorization_manifests "
                "SET expires_at=? WHERE job_id=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    queued.job_id,
                ),
            )

        retried = harness.client.post(
            f"/api/v1/jobs/{queued.job_id}:retry",
            headers=harness.write_headers,
        )
        assert retried.status_code == 200, retried.text
        blocked = _wait_for_job(harness, queued.job_id)

        assert blocked["state"] == "failed_retryable"
        assert blocked["attempt"] == 1
        assert blocked["required_action"] == "approve_retrieval"
        assert blocked["error_code"] == (
            "RETRIEVAL_INGESTION_AUTHORIZATION_EXPIRED"
        )
        assert _provider_usage_count(harness, queued.job_id) == 0
        assert harness.client.get(path).json()["authorization_state"] == (
            "EXPIRED"
        )
        _assert_unapproved_retry_blocked(harness, queued.job_id, attempt=1)
    finally:
        harness.close()


def test_profile_drift_stops_approved_ingestion_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接配置漂移会使 Job 批准失效，且必须先重新验证方案。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        queued = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="方案漂期资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的连接漂移测试。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="profile-drift-ingestion",
        )
        _wait_for_job(harness, queued.job_id)
        path = f"/api/v1/jobs/{queued.job_id}/retrieval-authorization"
        status = harness.client.get(path).json()
        approved = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(status),
        )
        assert approved.status_code == 200, approved.text
        connection = harness.runtime.control.get_connection(jina_connection_id)
        changed = harness.client.patch(
            f"/api/v1/provider-connections/{jina_connection_id}",
            headers=harness.write_headers,
            json={
                "expected_version": connection.configuration_version,
                "display_name": "Jina 漂移后的连接",
            },
        )
        assert changed.status_code == 200, changed.text

        retried = harness.client.post(
            f"/api/v1/jobs/{queued.job_id}:retry",
            headers=harness.write_headers,
        )
        assert retried.status_code == 200, retried.text
        blocked = _wait_for_job(harness, queued.job_id)

        assert blocked["error_code"] == "RETRIEVAL_INGESTION_PROFILE_INVALID"
        assert blocked["required_action"] == "approve_retrieval"
        assert _provider_usage_count(harness, queued.job_id) == 0
        current = harness.client.get(path).json()
        assert current["authorization_state"] == "BLOCKED"
        assert current["next_action"] == "repair_profile"
        assert current["approval_allowed"] is False
        assert current["reason_codes"] == [
            "RETRIEVAL_INGESTION_PROFILE_INVALID"
        ]
        refused = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(current),
        )
        assert refused.status_code == 409
    finally:
        harness.close()


def test_replaced_profile_job_returns_to_documents_instead_of_dead_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job 冻结的 Profile 被替代后应引导新建任务，而非伪称可修复。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        original_profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{original_profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        queued = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="方案替代资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的方案替代测试。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="replaced-profile-ingestion",
        )
        assert _wait_for_job(harness, queued.job_id)["required_action"] == (
            "approve_retrieval"
        )

        replacement_profile_id = _create_jina_profile(
            harness,
            knowledge_base_id,
            jina_connection_id,
            validate=False,
        )
        preview = harness.client.get(
            f"/api/v1/retrieval-profiles/{replacement_profile_id}:preview"
        )
        assert preview.status_code == 200, preview.text
        replacement = harness.client.post(
            f"/api/v1/retrieval-profiles/{replacement_profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": preview.json()["impact"]},
        )
        assert replacement.status_code == 200, replacement.text
        assert replacement.json()["status"] == "active"

        path = f"/api/v1/jobs/{queued.job_id}/retrieval-authorization"
        stale = harness.client.get(path)
        assert stale.status_code == 200, stale.text
        payload = stale.json()
        assert payload["authorization_state"] == "STALE_PROFILE"
        assert payload["next_action"] == "review_document"
        assert payload["approval_allowed"] is False
        assert payload["reason_codes"] == [
            "RETRIEVAL_INGESTION_PROFILE_CHANGED"
        ]
        refused = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(payload),
        )
        assert refused.status_code == 409
        assert _provider_usage_count(harness, queued.job_id) == 0
    finally:
        harness.close()


def test_budget_blocked_ingestion_can_be_reauthorized_without_new_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过低预算在出网前阻断后可续批并重试同一 Job。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        queued = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="预算续批资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的预算续批测试文本。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="budget-reauthorization",
        )
        _wait_for_job(harness, queued.job_id)
        path = f"/api/v1/jobs/{queued.job_id}/retrieval-authorization"
        initial = harness.client.get(path).json()
        low_budget = {
            **_ingestion_approval(initial),
            "estimated_token_limit": 1,
        }
        approved = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=low_budget,
        )
        assert approved.status_code == 200, approved.text
        low_campaign_id = approved.json()["manifest"]["budget_campaign_id"]
        retried = harness.client.post(
            f"/api/v1/jobs/{queued.job_id}:retry",
            headers=harness.write_headers,
        )
        assert retried.status_code == 200, retried.text
        budget_blocked = _wait_for_job(harness, queued.job_id)
        assert budget_blocked["error_code"] == (
            "RETRIEVAL_INGESTION_BUDGET_BLOCKED"
        )
        assert budget_blocked["required_action"] == "approve_retrieval"
        ledger = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3",
            read_only=True,
        )
        attempts = ledger.attempts(low_campaign_id)
        assert attempts
        assert not any(item["forwarded"] for item in attempts)

        exhausted = harness.client.get(path).json()
        assert exhausted["budget_state"] == "EXHAUSTED"
        assert exhausted["next_action"] == "reauthorize"
        assert exhausted["approval_allowed"] is True
        renewed = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(exhausted),
        )
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["manifest"]["budget_campaign_id"] != (
            low_campaign_id
        )
        resumed = harness.client.post(
            f"/api/v1/jobs/{queued.job_id}:retry",
            headers=harness.write_headers,
        )
        assert resumed.status_code == 200, resumed.text
        assert _wait_for_job(harness, queued.job_id)["state"] == "succeeded"
    finally:
        harness.close()


def test_ingestion_manifest_cannot_consume_another_job_campaign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制库错绑到另一 Job Campaign 时必须在 Provider 前失败。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        queued = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="错绑预算资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的错绑预算测试。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="cross-job-campaign",
        )
        _wait_for_job(harness, queued.job_id)
        path = f"/api/v1/jobs/{queued.job_id}/retrieval-authorization"
        status = harness.client.get(path).json()
        approved = harness.client.post(
            path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(status),
        )
        assert approved.status_code == 200, approved.text
        original_campaign_id = approved.json()["manifest"]["budget_campaign_id"]
        ledger = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3"
        )
        original = ledger.campaign(original_campaign_id)
        foreign = replace(
            original,
            campaign_id="retrieval-ingestion-budget-foreign-job",
            authorization_id="retrieval-ingestion-foreign-job",
            scope=(f"{project_id}:{knowledge_base_id}:job_{'f' * 32}"),
        )
        ledger.create_campaign(foreign)
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE retrieval_ingestion_authorization_manifests "
                "SET budget_campaign_id=?,authorization_id=? WHERE job_id=?",
                (foreign.campaign_id, foreign.authorization_id, queued.job_id),
            )

        mismatched = harness.client.get(path)
        assert mismatched.status_code == 200, mismatched.text
        assert mismatched.json()["authorization_state"] == "BLOCKED"
        assert mismatched.json()["next_action"] == "reauthorize"
        assert mismatched.json()["reason_codes"] == [
            "RETRIEVAL_INGESTION_CAMPAIGN_MISMATCH"
        ]
        retried = harness.client.post(
            f"/api/v1/jobs/{queued.job_id}:retry",
            headers=harness.write_headers,
        )
        assert retried.status_code == 200, retried.text
        blocked = _wait_for_job(harness, queued.job_id)
        assert blocked["error_code"] == (
            "RETRIEVAL_INGESTION_CAMPAIGN_MISMATCH"
        )
        assert blocked["required_action"] == "approve_retrieval"
        assert _provider_usage_count(harness, queued.job_id) == 0
        assert (
            ProviderBudgetLedger(
                harness.runtime.data_dir / "provider-budget.sqlite3",
                read_only=True,
            ).attempts(foreign.campaign_id)
            == []
        )
    finally:
        harness.close()


def test_unrelated_document_change_reauthorizes_without_discarding_pending_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """其他文档先入库后，未过时的目标版本可按新语料续批并完成。"""
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
        _approve(harness, profile_id)
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        activation = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activation.status_code == 200, activation.text
        activation_job_id = activation.json()["activation_job_id"]
        assert isinstance(activation_job_id, str)
        assert _wait_for_job(harness, activation_job_id)["state"] == "succeeded"

        pending = harness.runtime.sdk.create_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            content=build_package(
                "<w:p><w:r><w:t>公开合成的待处理目标新版本。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="pending-target-version",
        )
        assert _wait_for_job(harness, pending.job_id)["required_action"] == (
            "approve_retrieval"
        )
        pending_path = f"/api/v1/jobs/{pending.job_id}/retrieval-authorization"
        initial_status = harness.client.get(pending_path).json()
        initial_approval = harness.client.post(
            pending_path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(initial_status),
        )
        assert initial_approval.status_code == 200, initial_approval.text

        unrelated = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="无关并发资料.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的另一份并发资料。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="unrelated-document",
        )
        assert _wait_for_job(harness, unrelated.job_id)["required_action"] == (
            "approve_retrieval"
        )
        unrelated_path = (
            f"/api/v1/jobs/{unrelated.job_id}/retrieval-authorization"
        )
        unrelated_status = harness.client.get(unrelated_path).json()
        unrelated_approval = harness.client.post(
            unrelated_path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(unrelated_status),
        )
        assert unrelated_approval.status_code == 200, unrelated_approval.text
        unrelated_retry = harness.client.post(
            f"/api/v1/jobs/{unrelated.job_id}:retry",
            headers=harness.write_headers,
        )
        assert unrelated_retry.status_code == 200, unrelated_retry.text
        assert _wait_for_job(harness, unrelated.job_id)["state"] == "succeeded"

        changed = harness.client.get(pending_path)
        assert changed.status_code == 200, changed.text
        changed_status = changed.json()
        assert changed_status["authorization_state"] == "STALE_JOB"
        assert changed_status["next_action"] == "reauthorize"
        assert changed_status["approval_allowed"] is True
        assert changed_status["reason_codes"] == [
            "RETRIEVAL_INGESTION_JOB_CHANGED"
        ]
        renewed = harness.client.post(
            pending_path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(changed_status),
        )
        assert renewed.status_code == 200, renewed.text
        pending_retry = harness.client.post(
            f"/api/v1/jobs/{pending.job_id}:retry",
            headers=harness.write_headers,
        )
        assert pending_retry.status_code == 200, pending_retry.text
        completed = _wait_for_job(harness, pending.job_id)
        assert completed["state"] == "succeeded"

        with harness.runtime.connections.transaction() as connection:
            rows = connection.execute(
                "SELECT document_id,document_version_id "
                "FROM revision_documents WHERE revision_id=? "
                "ORDER BY document_id",
                (completed["revision_id"],),
            ).fetchall()
        assert {str(row["document_id"]) for row in rows} == {
            document_id,
            unrelated.document_id,
        }
        assert any(
            row["document_id"] == document_id
            and row["document_version_id"] == pending.document_version_id
            for row in rows
        )
    finally:
        harness.close()


def test_superseded_document_job_cannot_roll_back_current_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同文档新版本先成功后，滞留旧 Job 不得被批准成回滚。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        older = harness.runtime.sdk.create_document(
            project_id,
            knowledge_base_id,
            display_name="同文档版本竞争.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成的旧版本。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="superseded-v1",
        )
        assert _wait_for_job(harness, older.job_id)["required_action"] == (
            "approve_retrieval"
        )
        assert older.document_id is not None
        newer = harness.runtime.sdk.create_document_version(
            project_id,
            knowledge_base_id,
            older.document_id,
            content=build_package(
                "<w:p><w:r><w:t>公开合成的新版本。</w:t></w:r></w:p>"
            ),
            media_type=_DOCX_MEDIA_TYPE,
            idempotency_key="superseded-v2",
        )
        _wait_for_job(harness, newer.job_id)
        newer_path = f"/api/v1/jobs/{newer.job_id}/retrieval-authorization"
        newer_status = harness.client.get(newer_path).json()
        newer_approved = harness.client.post(
            newer_path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(newer_status),
        )
        assert newer_approved.status_code == 200, newer_approved.text
        newer_retry = harness.client.post(
            f"/api/v1/jobs/{newer.job_id}:retry",
            headers=harness.write_headers,
        )
        assert newer_retry.status_code == 200, newer_retry.text
        assert _wait_for_job(harness, newer.job_id)["state"] == "succeeded"

        older_path = f"/api/v1/jobs/{older.job_id}/retrieval-authorization"
        stale = harness.client.get(older_path)
        assert stale.status_code == 200, stale.text
        stale_status = stale.json()
        assert stale_status["authorization_state"] == "STALE_JOB"
        assert stale_status["next_action"] == "review_document"
        assert stale_status["approval_allowed"] is False
        assert stale_status["reason_codes"] == [
            "RETRIEVAL_INGESTION_TARGET_VERSION_SUPERSEDED"
        ]
        refused = harness.client.post(
            older_path + ":approve",
            headers=harness.write_headers,
            json=_ingestion_approval(stale_status),
        )
        assert refused.status_code == 409
        current = harness.runtime.sdk.get_document(
            project_id, knowledge_base_id, older.document_id
        )
        assert current.current_version_id == newer.document_version_id
        assert _provider_usage_count(harness, older.job_id) == 0
    finally:
        harness.close()


def test_product_startup_recovers_queued_remote_job_after_profile_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启恢复须先绑定 Product Profile，再把旧 queued Job 持久化为待授权。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        profile_id = _create_jina_profile(
            harness, knowledge_base_id, jina_connection_id
        )
        activated = harness.client.post(
            f"/api/v1/retrieval-profiles/{profile_id}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text
        monkeypatch.setattr(
            type(harness.runtime.providers),
            "test_only_transport",
            property(lambda _self: False),
        )
        with harness.runtime.profiles.revision_lifecycle_lease(
            knowledge_base_id, harness.runtime.p09.lifecycle
        ) as lifecycle:
            queued = lifecycle.create_document(
                project_id,
                knowledge_base_id,
                display_name="重启恢复资料.docx",
                content=build_package(
                    "<w:p><w:r><w:t>公开合成的重启恢复测试。</w:t></w:r></w:p>"
                ),
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="startup-recovery-ingestion",
            )
        assert queued.state.value == "queued"
        settings = harness.runtime.settings
        bootstrap_token = harness.bootstrap_token
        harness.close()

        runtime = build_product_runtime(
            settings,
            transport_factory=build_offline_mock_transport,
        )
        client = TestClient(create_product_app(runtime))
        session = client.post(
            "/api/v1/console/session",
            json={"bootstrap_token": bootstrap_token},
        )
        session.raise_for_status()
        harness = ProductHarness(
            runtime=runtime,
            client=client,
            csrf=str(session.json()["csrf_token"]),
            bootstrap_token=bootstrap_token,
        )
        recovered = _wait_for_job(harness, queued.job_id)

        assert recovered["state"] == "failed_retryable"
        assert recovered["attempt"] == 0
        assert recovered["required_action"] == "approve_retrieval"
        assert recovered["error_code"] == (
            "RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED"
        )
        assert _provider_usage_count(harness, queued.job_id) == 0
    finally:
        harness.close()
