"""湾事通管理员固定 Scope、History 与 Trace Facade 回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.models import KnowledgeBaseScope
from rag_app.product.provider_runtime import build_offline_mock_transport
from rag_app.tracing.models import TraceIdentity, TraceMode, TraceStatus
from rag_app.tracing.reasons import DecisionCode
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.mode import WANSHITONG_SCOPE_KEY
from rag_app.wanshitong.models import ScopeBinding
from rag_app.wanshitong.upload_validation import DOCX_MEDIA_TYPE
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import build_product_harness
from tests.wanshitong.support import PublicHarness, synthetic_answer


def test_admin_facade_requires_existing_console_session(
    public_harness: PublicHarness,
) -> None:
    with TestClient(public_harness.app) as anonymous:
        for path in (
            "/scope",
            "/overview",
            "/documents",
            "/jobs",
            "/history",
            "/feedback",
            "/feedback/statistics",
            "/operational-traces",
            "/models",
            "/system",
        ):
            response = anonymous.get(ADMIN_BASE_PATH + path)
            assert response.status_code == 401
        trace_id = "trace_" + "a" * 32
        assert anonymous.get(
            ADMIN_BASE_PATH + f"/operational-traces/{trace_id}/export"
        ).status_code == 401
        assert anonymous.post(
            ADMIN_BASE_PATH + "/operational-traces:export",
            json={"trace_ids": [trace_id]},
        ).status_code == 401
        assert anonymous.post(
            ADMIN_BASE_PATH + "/history-traces:export",
            json={"trace_ids": [trace_id]},
        ).status_code == 401
        assert anonymous.get(
            ADMIN_BASE_PATH + f"/feedback/{trace_id}"
        ).status_code == 401
        assert anonymous.patch(
            ADMIN_BASE_PATH + f"/feedback/{trace_id}/review",
            json={
                "expected_version": 0,
                "review_status": "NEW",
                "verification_references": [],
            },
        ).status_code == 401
        assert anonymous.post(
            ADMIN_BASE_PATH + "/feedback/export",
            json={"trace_ids": [trace_id]},
        ).status_code == 401


def test_history_batch_export_contains_original_answers_and_fixed_scope(
    public_harness: PublicHarness,
) -> None:
    """同一批问答原文与技术 Trace 入包，越界或缺正文则整包失败。"""
    runtime = public_harness.product.runtime
    fixed = public_harness.scope_service.binding()
    fixed_scope = KnowledgeBaseScope(
        project_id=fixed.project_id,
        knowledge_base_id=fixed.knowledge_base_id,
    )
    ids = []
    for suffix in ("3", "4"):
        trace_id, _ = _record_full_trace(
            public_harness, suffix=suffix, scope=fixed_scope
        )
        runtime.history.start(
            trace_id,
            fixed_scope,
            f"第 {suffix} 条原始问题？",
            owner_id="public-test-owner",
            save_body=True,
        )
        runtime.history.finish(
            trace_id,
            result=synthetic_answer(trace_id),
            error=None,
            cancelled=False,
        )
        ids.append(trace_id)

    endpoint = ADMIN_BASE_PATH + "/history-traces:export"
    denied_csrf = public_harness.client.post(
        endpoint, json={"trace_ids": ids}
    )
    exported = public_harness.client.post(
        endpoint,
        json={"trace_ids": list(reversed(ids))},
        headers=public_harness.product.write_headers,
    )
    assert denied_csrf.status_code == 403
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("MANIFEST.json"))
        assert manifest["item_count"] == 2
        assert all(item["body_included"] for item in manifest["items"])
        for trace_id in ids:
            history = json.loads(
                archive.read(f"items/{trace_id}/history.json")
            )
            operation = json.loads(
                archive.read(f"items/{trace_id}/operational-trace.json")
            )
            assert history["question"].endswith("条原始问题？")
            assert history["answer"] == (
                "办理材料应在五个工作日内完成核验。"
            )
            assert history["result"] is not None
            assert operation["trace"]["trace_id"] == trace_id

    other_project = runtime.sdk.create_project(
        "越界导出项目", idempotency_key="history-export-other-project"
    )
    other_kb = runtime.sdk.create_knowledge_base(
        other_project.project_id,
        "越界导出知识库",
        idempotency_key="history-export-other-kb",
    )
    outside_scope = KnowledgeBaseScope(
        project_id=other_project.project_id,
        knowledge_base_id=other_kb.knowledge_base_id,
    )
    outside, _ = _record_full_trace(
        public_harness, suffix="5", scope=outside_scope
    )
    runtime.history.start(
        outside,
        outside_scope,
        "越界原始问题",
        owner_id="other-owner",
        save_body=True,
    )
    outside_response = public_harness.client.post(
        endpoint,
        json={"trace_ids": [ids[0], outside]},
        headers=public_harness.product.write_headers,
    )
    assert outside_response.status_code == 404

    missing_body = "trace_" + "6" * 32
    runtime.history.start(
        missing_body,
        fixed_scope,
        "未保存原文的问题",
        owner_id="public-test-owner",
        save_body=False,
    )
    unavailable = public_harness.client.post(
        endpoint,
        json={"trace_ids": [ids[0], missing_body]},
        headers=public_harness.product.write_headers,
    )
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == (
        "HISTORY_BODY_UNAVAILABLE"
    )


def test_admin_facade_uses_only_fixed_scope(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    fixed = public_harness.scope_service.binding()
    other_project = runtime.sdk.create_project(
        "其他项目", idempotency_key="other-project"
    )
    runtime.sdk.create_knowledge_base(
        other_project.project_id,
        "其他知识库",
        idempotency_key="other-kb",
    )

    scope = public_harness.client.get(ADMIN_BASE_PATH + "/scope")
    overview = public_harness.client.get(ADMIN_BASE_PATH + "/overview")
    documents = public_harness.client.get(ADMIN_BASE_PATH + "/documents")
    jobs = public_harness.client.get(ADMIN_BASE_PATH + "/jobs")
    system = public_harness.client.get(ADMIN_BASE_PATH + "/system")

    assert scope.status_code == 200
    assert scope.json()["ready"] is True
    assert scope.json()["project_id"] == fixed.project_id
    assert overview.json()["scope"]["project_id"] == fixed.project_id
    assert overview.json()["scope"]["mode"] == "wanshitong"
    assert documents.json()["items"] == []
    assert jobs.json()["items"] == []
    assert system.status_code == 200
    assert system.json()["mode"] == "wanshitong"
    assert system.json()["scope"]["project_id"] == fixed.project_id
    assert system.json()["query_executor"] == {
        "max_workers": 4,
        "max_queue": 8,
        "in_flight": 0,
        "retry_after_seconds": 5,
    }
    assert system.json()["history"]["retention_days"] == (
        runtime.history.retention_days
    )


def test_scope_recheck_reports_blocker_without_creating_another_scope(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    valid = public_harness.scope_service.binding()
    other_project = runtime.sdk.create_project(
        "错误绑定项目", idempotency_key="broken-binding-project"
    )
    corrupted = ScopeBinding(
        project_id=other_project.project_id,
        knowledge_base_id=valid.knowledge_base_id,
        created_at=valid.created_at,
    )
    with runtime.connections.transaction(write=True) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE namespace=? AND key=?",
            (
                corrupted.model_dump_json(),
                "wanshitong.fixed-scope",
                WANSHITONG_SCOPE_KEY,
            ),
        )
    projects_before = runtime.sdk.list_projects()

    first = public_harness.client.get(ADMIN_BASE_PATH + "/scope")
    second = public_harness.client.get(ADMIN_BASE_PATH + "/scope")

    assert first.status_code == 200
    assert first.json()["ready"] is False
    assert first.json()["project_id"] is None
    assert first.json()["blocker_code"] == "WANSHITONG_SCOPE_INVALID"
    assert "无效" in first.json()["blocker_message"]
    assert second.json() == first.json()
    assert runtime.sdk.list_projects() == projects_before


def test_invalid_persisted_scope_keeps_admin_blocker_available_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    first = build_product_harness(tmp_path)
    settings = first.runtime.settings
    try:
        valid = first.runtime.sdk.list_projects()[0]
        binding = first.client.get(ADMIN_BASE_PATH + "/scope").json()
        other_project = first.runtime.sdk.create_project(
            "持久错误项目", idempotency_key="restart-broken-project"
        )
        corrupted = ScopeBinding(
            project_id=other_project.project_id,
            knowledge_base_id=binding["knowledge_base_id"],
            created_at=binding["created_at"],
        )
        with first.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE metadata SET value=? WHERE namespace=? AND key=?",
                (
                    corrupted.model_dump_json(),
                    "wanshitong.fixed-scope",
                    WANSHITONG_SCOPE_KEY,
                ),
            )
        project_count = len(first.runtime.sdk.list_projects())
        assert valid.project_id != other_project.project_id
    finally:
        first.close()

    runtime = build_product_runtime(
        settings, transport_factory=build_offline_mock_transport
    )
    admin_token = "-".join(("synthetic", "admin", "token"))
    try:
        app = create_product_app(runtime, admin_token=admin_token)
        with TestClient(app) as client:
            response = client.get(
                ADMIN_BASE_PATH + "/scope",
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200
        assert response.json()["ready"] is False
        assert response.json()["blocker_code"] == (
            "WANSHITONG_SCOPE_INVALID"
        )
        assert len(runtime.sdk.list_projects()) == project_count
    finally:
        runtime.close()


def test_history_clear_is_scope_aware_and_requires_csrf(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    fixed = public_harness.scope_service.binding()
    other_project = runtime.sdk.create_project(
        "历史隔离项目", idempotency_key="history-project"
    )
    other_kb = runtime.sdk.create_knowledge_base(
        other_project.project_id,
        "历史隔离知识库",
        idempotency_key="history-kb",
    )
    fixed_trace = "trace_" + "1" * 32
    other_trace = "trace_" + "2" * 32
    runtime.history.start(
        fixed_trace,
        KnowledgeBaseScope(
            project_id=fixed.project_id,
            knowledge_base_id=fixed.knowledge_base_id,
        ),
        "固定范围问题",
        owner_id="public-owner-fixed",
        save_body=False,
    )
    runtime.history.start(
        other_trace,
        KnowledgeBaseScope(
            project_id=other_project.project_id,
            knowledge_base_id=other_kb.knowledge_base_id,
        ),
        "其他范围问题",
        owner_id="public-owner-other",
        save_body=False,
    )

    listed = public_harness.client.get(ADMIN_BASE_PATH + "/history")
    denied = public_harness.client.delete(ADMIN_BASE_PATH + "/history")
    cleared = public_harness.client.delete(
        ADMIN_BASE_PATH + "/history",
        headers=public_harness.product.write_headers,
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["owner_masked_id"].startswith(
        "anonymous-"
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "CSRF_REQUIRED"
    assert cleared.status_code == 200
    assert cleared.json() == {"deleted_count": 1}
    assert runtime.history.has_record(fixed_trace) is False
    assert runtime.history.has_record(other_trace) is True


def test_history_trace_and_public_session_remain_reachable(
    public_harness: PublicHarness,
) -> None:
    history = public_harness.client.get(ADMIN_BASE_PATH + "/history")
    traces = public_harness.client.get(
        ADMIN_BASE_PATH + "/operational-traces"
    )
    logged_out = public_harness.client.delete(
        "/api/v1/console/session",
        headers=public_harness.product.write_headers,
    )
    resumed_public = public_harness.client.post("/api/public/session")

    assert history.status_code == 200
    assert traces.status_code == 200
    assert logged_out.status_code == 204
    assert resumed_public.status_code == 200
    assert resumed_public.json()["session_id"] == public_harness.session_id


def test_trace_artifact_rechecks_fixed_scope_and_live_source(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    fixed = public_harness.scope_service.binding()
    document_job = runtime.sdk.create_document(
        fixed.project_id,
        fixed.knowledge_base_id,
        display_name="Trace 来源.docx",
        content=build_package(
            "<w:p><w:r><w:t>Trace 来源合成文本。</w:t></w:r></w:p>"
        ),
        media_type=DOCX_MEDIA_TYPE,
        idempotency_key="trace-source-document",
    )
    assert document_job.document_id is not None
    trace_id, artifact_id = _record_full_trace(
        public_harness,
        suffix="3",
        scope=KnowledgeBaseScope(
            project_id=fixed.project_id,
            knowledge_base_id=fixed.knowledge_base_id,
        ),
        document_id=document_job.document_id,
    )
    artifact_path = (
        ADMIN_BASE_PATH
        + f"/operational-traces/{trace_id}/artifacts/{artifact_id}"
    )
    readable = public_harness.client.get(artifact_path)
    filtered = public_harness.client.get(
        ADMIN_BASE_PATH + "/operational-traces",
        params={
            "page": "1",
            "page_size": "30",
            "trace_id": trace_id,
            "kind": "query",
        },
    )
    with runtime.connections.transaction(write=True) as connection:
        connection.execute(
            "UPDATE documents SET deleted_at=? WHERE document_id=?",
            (datetime.now(UTC).isoformat(), document_job.document_id),
        )
    deleted_source = public_harness.client.get(artifact_path)

    other_project = runtime.sdk.create_project(
        "Trace 隔离项目", idempotency_key="trace-isolation-project"
    )
    other_kb = runtime.sdk.create_knowledge_base(
        other_project.project_id,
        "Trace 隔离知识库",
        idempotency_key="trace-isolation-kb",
    )
    other_trace, other_artifact = _record_full_trace(
        public_harness,
        suffix="4",
        scope=KnowledgeBaseScope(
            project_id=other_project.project_id,
            knowledge_base_id=other_kb.knowledge_base_id,
        ),
    )
    cross_scope = public_harness.client.get(
        ADMIN_BASE_PATH
        + f"/operational-traces/{other_trace}/artifacts/{other_artifact}"
    )

    assert readable.status_code == 200, readable.text
    assert readable.json() == {"safe": True}
    assert filtered.status_code == 200
    assert filtered.json()["page"] == 1
    assert filtered.json()["page_size"] == 30
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["trace_id"] == trace_id
    assert deleted_source.status_code == 403
    assert deleted_source.json()["error"]["stage"] == "trace.source"
    assert cross_scope.status_code == 404


def test_trace_download_restores_single_and_batch_with_fixed_scope(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    fixed = public_harness.scope_service.binding()
    fixed_scope = KnowledgeBaseScope(
        project_id=fixed.project_id,
        knowledge_base_id=fixed.knowledge_base_id,
    )
    first, _ = _record_full_trace(public_harness, suffix="5", scope=fixed_scope)
    second, _ = _record_full_trace(
        public_harness, suffix="6", scope=fixed_scope
    )
    other_project = runtime.sdk.create_project(
        "导出隔离项目", idempotency_key="export-isolation-project"
    )
    other_kb = runtime.sdk.create_knowledge_base(
        other_project.project_id,
        "导出隔离知识库",
        idempotency_key="export-isolation-kb",
    )
    outside, _ = _record_full_trace(
        public_harness,
        suffix="7",
        scope=KnowledgeBaseScope(
            project_id=other_project.project_id,
            knowledge_base_id=other_kb.knowledge_base_id,
        ),
    )
    export_path = ADMIN_BASE_PATH + "/operational-traces:export"
    first_path = ADMIN_BASE_PATH + f"/operational-traces/{first}/export"

    single = public_harness.client.get(first_path)
    missing_csrf = public_harness.client.post(
        export_path, json={"trace_ids": [first]}
    )
    batch = public_harness.client.post(
        export_path,
        json={"trace_ids": [second, first]},
        headers=public_harness.product.write_headers,
    )
    outside_single = public_harness.client.get(
        ADMIN_BASE_PATH + f"/operational-traces/{outside}/export"
    )
    outside_batch = public_harness.client.post(
        export_path,
        json={"trace_ids": [first, outside]},
        headers=public_harness.product.write_headers,
    )
    missing_batch = public_harness.client.post(
        export_path,
        json={"trace_ids": [first, "trace_" + "9" * 32]},
        headers=public_harness.product.write_headers,
    )

    assert single.status_code == 200, single.text
    assert single.headers["content-disposition"] == (
        f'attachment; filename="{first}.json"'
    )
    assert single.headers["cache-control"] == "no-store"
    assert single.content == runtime.traces.store.export_trace(first)
    assert missing_csrf.status_code == 403
    assert batch.status_code == 200, batch.text
    assert batch.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(batch.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert [item["trace_id"] for item in manifest["items"]] == [
            first,
            second,
        ]
        assert archive.read(f"traces/{first}.json") == single.content
        assert archive.read(f"traces/{second}.json") == (
            runtime.traces.store.export_trace(second)
        )
    assert outside_single.status_code == 404
    assert outside_batch.status_code == 404
    assert missing_batch.status_code == 404


def test_trace_download_rechecks_deleted_source(
    public_harness: PublicHarness,
) -> None:
    runtime = public_harness.product.runtime
    fixed = public_harness.scope_service.binding()
    document_job = runtime.sdk.create_document(
        fixed.project_id,
        fixed.knowledge_base_id,
        display_name="导出来源.docx",
        content=build_package(
            "<w:p><w:r><w:t>导出来源合成文本。</w:t></w:r></w:p>"
        ),
        media_type=DOCX_MEDIA_TYPE,
        idempotency_key="trace-export-source",
    )
    assert document_job.document_id is not None
    trace_id, _ = _record_full_trace(
        public_harness,
        suffix="8",
        scope=KnowledgeBaseScope(
            project_id=fixed.project_id,
            knowledge_base_id=fixed.knowledge_base_id,
        ),
        document_id=document_job.document_id,
    )
    with runtime.connections.transaction(write=True) as connection:
        connection.execute(
            "UPDATE documents SET deleted_at=? WHERE document_id=?",
            (datetime.now(UTC).isoformat(), document_job.document_id),
        )
    single = public_harness.client.get(
        ADMIN_BASE_PATH + f"/operational-traces/{trace_id}/export"
    )
    batch = public_harness.client.post(
        ADMIN_BASE_PATH + "/operational-traces:export",
        json={"trace_ids": [trace_id]},
        headers=public_harness.product.write_headers,
    )

    assert single.status_code == 403
    assert batch.status_code == 403
    assert single.json()["error"]["stage"] == "trace.source"


def _record_full_trace(
    harness: PublicHarness,
    *,
    suffix: str,
    scope: KnowledgeBaseScope,
    document_id: str | None = None,
) -> tuple[str, str]:
    trace_id = "trace_" + suffix * 32
    session = harness.product.runtime.traces.recorder.begin_query(
        trace_id,
        TraceMode.FULL,
        datetime.now(UTC),
        TraceIdentity(
            pipeline_fingerprint="sha256:" + "1" * 64,
            serving_fingerprint="sha256:" + "2" * 64,
            release_revision="wb-05-test",
            active_collection="synthetic",
            index_manifest_sha256="3" * 64,
            payload_schema_version=2,
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            document_id=document_id,
        ),
    )
    artifact = session.artifact("synthetic", {"safe": True})
    assert artifact is not None
    session.finish(
        status=TraceStatus.SUCCEEDED,
        reason_code=DecisionCode.ANSWERED,
    )
    harness.product.runtime.traces.flush()
    return trace_id, artifact.artifact_id


def test_universal_mode_keeps_original_management_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAG_PRODUCT_MODE", raising=False)
    harness = build_product_harness(tmp_path)
    try:
        wanshitong = harness.client.get(ADMIN_BASE_PATH + "/scope")
        project = harness.client.post(
            "/api/v1/projects",
            headers={
                **harness.write_headers,
                "Idempotency-Key": "universal-project",
            },
            json={"name": "Universal 项目"},
        )

        assert wanshitong.status_code == 404
        assert project.status_code == 201
    finally:
        harness.close()
