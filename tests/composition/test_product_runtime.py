"""P10.5 Product Runtime 唯一组合根回归。"""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest

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
        assert migration_count == 14
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

        assert job.state.value == "succeeded"
        assert [(row[0], row[1]) for row in slots] == [
            ("primary", "dense_primary"),
            ("standby", "dense_standby"),
        ]
        assert usage == 2
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

        assert first.selected_embedding_slot == "standby"
        assert first.selected_vector_name == "dense_standby"
        assert second.selected_embedding_slot == "standby"
        assert recovered.selected_embedding_slot == "primary"
        assert recovered.selected_vector_name == "dense_primary"
    finally:
        harness.close()
