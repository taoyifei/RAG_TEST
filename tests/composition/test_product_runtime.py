"""P10.5 Product Runtime 唯一组合根回归。"""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest

from rag_app.adapters.parsers import word_document
from rag_app.application.provider_health import ProviderCircuitBreaker
from rag_app.core.models import Job
from rag_app.core.policies import CircuitBreakerPolicy
from rag_app.product.models import ProviderConnection
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_DOC_MEDIA_TYPE = "application/msword"
_DOC_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _wait_for_job(harness: ProductHarness, job: Job) -> Job:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            return current
        sleep(0.01)
    raise AssertionError(f"Job 未在期限内结束：{job.job_id}")


def test_product_runtime_migrates_and_keeps_offline_base_mode(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path, master_key=False)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        status = harness.runtime.sdk.health()
        with harness.runtime.connections.transaction() as connection:
            migration_count = int(
                connection.execute(
                    "SELECT count(*) FROM schema_migrations"
                ).fetchone()[0]
            )

        assert project_id.startswith("prj_")
        assert knowledge_base_id.startswith("kb_")
        assert status.runtime_identity == "product-runtime-p10.5"
        assert status.primary_live_evaluation_status == "not_verified"
        assert status.remote_production_profile_ready is False
        assert migration_count == 15
    finally:
        harness.close()


def test_product_runtime_without_provider_keeps_fts_exact_flow(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path, master_key=False)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        content = build_package(
            "<w:p><w:r><w:t>青岛啤酒采购流程使用公开合成文本。</w:t></w:r></w:p>"
        )
        job = _wait_for_job(
            harness,
            harness.runtime.sdk.create_document(
                project_id,
                knowledge_base_id,
                display_name="公开合成文档.docx",
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="offline-base-mode",
            ),
        )
        result = harness.runtime.sdk.search(
            project_id,
            knowledge_base_id,
            "青岛啤酒",
        )

        assert job.state.value == "succeeded"
        assert result.evidence
        assert result.diagnostics is not None
        channels = dict(result.diagnostics.channel_chunk_ids)
        assert channels["lexical"]
    finally:
        harness.close()


def test_product_runtime_rejects_word_signature_masquerade_before_enqueue(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path, master_key=False)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        endpoint = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/documents"
        )
        response = harness.client.post(
            endpoint,
            params={"display_name": "伪装文件.docx"},
            content=_DOC_MAGIC + b"synthetic-legacy-doc",
            headers={
                **harness.write_headers,
                "Content-Type": _DOCX_MEDIA_TYPE,
                "Idempotency-Key": "word-signature-masquerade",
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_DOCUMENT"
        jobs = harness.runtime.sdk.list_jobs(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        assert jobs.total == 0
    finally:
        harness.close()


def test_product_runtime_accepts_mixed_doc_and_docx_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _extract_doc(_content: bytes, _policy: object) -> str:
        return "旧版质量管理制度\n设备巡检要求"

    monkeypatch.setattr(
        word_document,
        "_extract_doc_text",
        _extract_doc,
    )
    harness = build_product_harness(tmp_path, master_key=False)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        endpoint = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/documents"
        )
        legacy_response = harness.client.post(
            endpoint,
            params={"display_name": "旧版制度.doc"},
            content=_DOC_MAGIC + b"synthetic-legacy-doc",
            headers={
                **harness.write_headers,
                "Content-Type": _DOC_MEDIA_TYPE,
                "Idempotency-Key": "legacy-doc",
            },
        )
        assert legacy_response.status_code == 202, legacy_response.text
        legacy_job = _wait_for_job(
            harness,
            Job.model_validate(legacy_response.json()),
        )

        docx_response = harness.client.post(
            endpoint,
            params={"display_name": "新版制度.docx"},
            content=build_package(
                "<w:p><w:r><w:t>新版质量制度</w:t></w:r></w:p>"
            ),
            headers={
                **harness.write_headers,
                "Content-Type": _DOCX_MEDIA_TYPE,
                "Idempotency-Key": "current-docx",
            },
        )
        assert docx_response.status_code == 202, docx_response.text
        docx_job = _wait_for_job(
            harness,
            Job.model_validate(docx_response.json()),
        )
        result = harness.runtime.sdk.search(
            project_id,
            knowledge_base_id,
            "设备巡检",
        )
        documents = harness.runtime.sdk.list_documents(
            project_id,
            knowledge_base_id,
        )

        assert legacy_job.state.value == "succeeded"
        assert docx_job.state.value == "succeeded"
        assert docx_job.revision_id != legacy_job.revision_id
        assert {item.display_name for item in documents} == {
            "旧版制度.doc",
            "新版制度.docx",
        }
        assert result.evidence
    finally:
        harness.close()


def test_active_page_profile_drives_dual_index_and_primary_query(
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
        validate_five_operations(harness, jina_connection, aliyun_connection)
        activate_hot_standby_profile(
            harness,
            knowledge_base_id,
            jina_connection,
            aliyun_connection,
        )
        content = build_package(
            "<w:p><w:r><w:t>青岛啤酒采购流程使用公开合成文本。</w:t></w:r></w:p>"
        )
        job = _wait_for_job(
            harness,
            harness.runtime.sdk.create_document(
                project_id,
                knowledge_base_id,
                display_name="公开合成双槽文档.docx",
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="page-profile-dual-index",
            ),
        )
        result = harness.runtime.sdk.search(
            project_id,
            knowledge_base_id,
            "青岛啤酒采购流程",
        )
        with harness.runtime.connections.transaction() as connection:
            slots = tuple(
                connection.execute(
                    "SELECT slot_id, vector_name FROM embedding_slots "
                    "WHERE revision_id=? ORDER BY role",
                    (job.revision_id,),
                ).fetchall()
            )
            usage = int(
                connection.execute(
                    "SELECT count(*) FROM job_provider_usage WHERE job_id=?",
                    (job.job_id,),
                ).fetchone()[0]
            )
            operation_rows = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT operation, selected_slot, failover "
                    "FROM provider_operation_events "
                    "WHERE selected_slot IS NOT NULL"
                ).fetchall()
            )

        assert job.state.value == "succeeded"
        assert [(row[0], row[1]) for row in slots] == [
            ("primary", "dense_primary"),
            ("standby", "dense_standby"),
        ]
        assert usage == 2
        assert ("embedding.document", "primary", 0) in operation_rows
        assert ("embedding.document", "standby", 0) in operation_rows
        assert ("embedding.query", "primary", 0) in operation_rows
        assert result.selected_embedding_slot == "primary"
        assert result.selected_vector_name == "dense_primary"
        assert result.rerank_execution_mode == "provider"
        assert result.evidence
    finally:
        harness.close()


def test_product_profile_fails_over_and_returns_to_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    fault = {"jina_query": False}
    clock = {"value": 0.0}

    def _transport(connection: ProviderConnection) -> httpx.MockTransport:
        successful = build_offline_mock_transport(connection)

        def _handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if (
                fault["jina_query"]
                and "api.jina.ai" in request.url.host
                and body.get("task") == "retrieval.query"
            ):
                return httpx.Response(503, json={"error": "synthetic"})
            return successful.handle_request(request)

        return httpx.MockTransport(_handler)

    def _circuit() -> ProviderCircuitBreaker:
        return ProviderCircuitBreaker(
            CircuitBreakerPolicy(
                failure_threshold=2,
                open_cooldown_seconds=10,
                recovery_success_threshold=1,
            ),
            clock=lambda: clock["value"],
        )

    harness = build_product_harness(
        tmp_path,
        transport_factory=_transport,
        circuit_factory=_circuit,
    )
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection, aliyun_connection = create_provider_connections(
            harness
        )
        validate_five_operations(harness, jina_connection, aliyun_connection)
        activate_hot_standby_profile(
            harness,
            knowledge_base_id,
            jina_connection,
            aliyun_connection,
        )
        content = build_package(
            "<w:p><w:r><w:t>公开合成的主备切换验收文本。</w:t></w:r></w:p>"
        )
        job = _wait_for_job(
            harness,
            harness.runtime.sdk.create_document(
                project_id,
                knowledge_base_id,
                display_name="公开合成切换文档.docx",
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="failover-profile-index",
            ),
        )
        assert job.state.value == "succeeded"

        fault["jina_query"] = True
        first = harness.runtime.sdk.search(
            project_id, knowledge_base_id, "主备切换第一次"
        )
        second = harness.runtime.sdk.search(
            project_id, knowledge_base_id, "主备切换第二次"
        )
        fault["jina_query"] = False
        clock["value"] = 11.0
        recovered = harness.runtime.sdk.search(
            project_id, knowledge_base_id, "主备切换恢复探测"
        )
        with harness.runtime.connections.transaction() as connection:
            failover_count = int(
                connection.execute(
                    "SELECT count(*) FROM provider_operation_events "
                    "WHERE operation='embedding.query' "
                    "AND selected_slot='standby' AND failover=1"
                ).fetchone()[0]
            )

        assert first.selected_embedding_slot == "standby"
        assert first.selected_vector_name == "dense_standby"
        assert second.selected_embedding_slot == "standby"
        assert failover_count == 2
        assert recovered.selected_embedding_slot == "primary"
        assert recovered.selected_vector_name == "dense_primary"
    finally:
        harness.close()
