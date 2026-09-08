"""默认 Product Operational Trace 的端到端合同。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from time import monotonic, sleep
from typing import NoReturn
from zipfile import ZipFile

import pytest

from rag_app.core.events import TraceEvent
from rag_app.core.identifiers import new_id
from rag_app.tracing import TraceUnavailableError
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _upload(
    harness: ProductHarness,
    project_id: str,
    knowledge_base_id: str,
) -> tuple[str, str, str]:
    job = harness.runtime.sdk.create_document(
        project_id,
        knowledge_base_id,
        display_name="公开 Trace 合同.docx",
        content=build_package(
            "<w:p><w:r><w:t>设备 TR-21 的维护周期为 14 天。</w:t></w:r></w:p>"
        ),
        media_type=_MEDIA_TYPE,
        idempotency_key="trace-product-fixture",
    )
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            assert current.state.value == "succeeded", current
            assert current.document_id is not None
            return current.job_id, current.document_id, current.revision_id
        sleep(0.01)
    raise AssertionError("Trace 合成文档上传超时")


def test_failed_query_is_visible_in_operational_trace(tmp_path: Path) -> None:
    """查询失败也必须结算 History 与同 ID 技术 Trace。"""
    harness = build_product_harness(tmp_path)
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    response = harness.client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}:answer",
        json={"query": "尚未上传资料时应当失败"},
        headers=harness.write_headers,
    )
    assert response.status_code == 409
    trace_id = response.json()["error"]["trace_id"]

    detail = harness.client.get(f"/api/v1/admin/operational-traces/{trace_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["trace"]["status"] == "FAILED"
    assert payload["trace"]["project_id"] == project_id
    assert payload["trace"]["knowledge_base_id"] == knowledge_base_id
    assert payload["trace"]["capture_complete"] is True
    assert [item["name"] for item in payload["spans"]] == [
        "rag.query",
        "request.admission",
        "history.settlement",
    ]
    assert detail.headers["Cache-Control"] == "no-store"

    page = harness.client.get("/api/v1/admin/operational-traces")
    assert page.status_code == 200
    assert page.json()["items"][0]["trace_id"] == trace_id
    harness.close()


def test_safe_trace_store_failure_keeps_query_error_and_audits_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SAFE 技术库故障不能覆盖业务错误，且 History 留下安全审计事件。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )

        def fail_create(*_args: object, **_kwargs: object) -> NoReturn:
            raise OSError("synthetic trace store outage")

        monkeypatch.setattr(
            harness.runtime.traces.store,
            "create_trace",
            fail_create,
        )
        response = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer",
            json={"query": "保留原始无活动索引错误"},
            headers=harness.write_headers,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "INDEX_NOT_READY"
        trace_id = response.json()["error"]["trace_id"]
        harness.runtime.traces.recorder.flush()
        assert any(
            event.event_name == "trace.capture_failed"
            for event in harness.runtime.history.events(trace_id)
        )
    finally:
        harness.close()


def test_full_preflight_fails_before_history_or_query_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FULL 容量不足必须在 History 和业务查询开始前拒绝。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )

        def reject_full() -> NoReturn:
            raise TraceUnavailableError("synthetic full capacity exhaustion")

        monkeypatch.setattr(
            harness.runtime.traces.recorder,
            "require_full_capacity",
            reject_full,
        )
        response = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer",
            json={"query": "不得进入查询链", "trace_mode": "FULL"},
            headers=harness.write_headers,
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "TRACE_FULL_UNAVAILABLE"
        trace_id = response.json()["error"]["trace_id"]
        assert (
            harness.runtime.history.list_history(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )["total"]
            == 0
        )
        assert harness.runtime.history.events(trace_id) == ()
        assert (
            harness.client.get(
                f"/api/v1/admin/operational-traces/{trace_id}"
            ).status_code
            == 404
        )
    finally:
        harness.close()


def test_legacy_flat_history_is_explicitly_incomplete(tmp_path: Path) -> None:
    """旧平面事件可读，但不得伪造不存在的 span。"""
    harness = build_product_harness(tmp_path)
    try:
        trace_id = new_id("trace")
        event = TraceEvent(
            trace_id=trace_id,
            event_name="retrieval.snapshot",
            occurred_at=datetime.now(UTC),
            attributes=(("revision_id", "legacy-public-fixture"),),
        )
        harness.runtime.history.record(event)

        response = harness.client.get(
            f"/api/v1/admin/operational-traces/{trace_id}"
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["trace"] == {
            "trace_id": trace_id,
            "schema_version": "legacy-flat-1",
            "capture_complete": False,
            "capture_incomplete_reason": "legacy_flat_events",
        }
        assert payload["spans"] == []
        assert payload["candidate_decisions"] == []
        assert payload["artifacts"] == []
        assert payload["legacy_flat_events"] == [event.model_dump(mode="json")]
    finally:
        harness.close()


def test_non_admin_query_cannot_request_full_trace(tmp_path: Path) -> None:
    """普通 query token 不得借查询接口开启 FULL 捕获。"""
    harness = build_product_harness(tmp_path)
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    issued = harness.runtime.auth.create_access_token(
        name="query only",
        scopes=("query:read",),
        project_id=project_id,
        knowledge_base_id=knowledge_base_id,
    )
    harness.client.cookies.clear()
    response = harness.client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}:answer",
        headers={"Authorization": f"Bearer {issued.token}"},
        json={"query": "不能开启完整追踪", "trace_mode": "FULL"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["stage"] == "trace.mode"
    trace_list = harness.client.get(
        "/api/v1/admin/operational-traces",
        headers={"Authorization": f"Bearer {issued.token}"},
    )
    assert trace_list.status_code == 403
    assert trace_list.json()["error"]["code"] == "TOKEN_DENIED"
    harness.close()


def test_diagnostic_and_full_trace_keep_content_boundaries(
    tmp_path: Path,
) -> None:
    """候选漏斗、耗时和 Artifact 可见，但问题正文不进入技术库。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        endpoint = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer"
        )
        content_marker = "公开问题标记-不得进入技术追踪"
        diagnostic = harness.client.post(
            endpoint,
            json={
                "query": f"TR-21 {content_marker}",
                "trace_mode": "DIAGNOSTIC",
            },
            headers=harness.write_headers,
        )
        assert diagnostic.status_code == 200, diagnostic.text
        trace_id = diagnostic.json()["trace_id"]
        detail = harness.client.get(
            f"/api/v1/admin/operational-traces/{trace_id}"
        )
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["trace"]["mode"] == "DIAGNOSTIC"
        assert payload["trace"]["status"] == "ANSWERED"
        assert payload["candidate_decisions"]
        assert any(
            item["score_type"] == "rrf"
            for item in payload["candidate_decisions"]
        )
        assert any(
            item["score_type"] == "rrf_contribution"
            and item["channel"]
            and item["contribution"] > 0
            for item in payload["candidate_decisions"]
        )
        assert any(
            item["name"].startswith("timing.") for item in payload["spans"]
        )
        assert payload["artifacts"] == []
        assert content_marker not in json.dumps(payload, ensure_ascii=False)

        full = harness.client.post(
            endpoint,
            json={"query": "TR-21", "trace_mode": "FULL"},
            headers=harness.write_headers,
        )
        assert full.status_code == 200, full.text
        full_id = full.json()["trace_id"]
        full_detail = harness.client.get(
            f"/api/v1/admin/operational-traces/{full_id}"
        ).json()
        assert full_detail["trace"]["mode"] == "FULL"
        assert full_detail["artifacts"][0]["kind"] == "retrieval_diagnostics"
        artifact_id = full_detail["artifacts"][0]["artifact_id"]
        artifact = harness.client.get(
            f"/api/v1/admin/operational-traces/{full_id}/artifacts/"
            f"{artifact_id}"
        )
        assert artifact.status_code == 200
        assert (
            artifact.headers["X-Artifact-SHA256"]
            == hashlib.sha256(artifact.content).hexdigest()
        )
        assert "channel_chunk_ids" in artifact.json()
    finally:
        harness.close()


def test_ingestion_trace_and_stable_batch_export(tmp_path: Path) -> None:
    """Job/Document/Revision 身份贯通，ZIP 清单与成员摘要稳定。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        job_id, document_id, revision_id = _upload(
            harness, project_id, knowledge_base_id
        )
        page = harness.client.get(
            "/api/v1/admin/operational-traces",
            params={"job_id": job_id},
        )
        assert page.status_code == 200
        assert page.json()["total"] == 1
        root = page.json()["items"][0]
        assert root["kind"] == "ingestion"
        assert root["status"] == "SUCCEEDED"
        assert root["document_id"] == document_id
        assert root["revision_id"] == revision_id
        trace_id = root["trace_id"]
        detail = harness.client.get(
            f"/api/v1/admin/operational-traces/{trace_id}"
        ).json()
        assert any(
            span["name"] == "ingestion.document_persistence"
            and span["status"] == "OK"
            for span in detail["spans"]
        )

        exported = harness.client.post(
            "/api/v1/admin/operational-traces:export",
            json={"trace_ids": [trace_id]},
            headers=harness.write_headers,
        )
        assert exported.status_code == 200
        repeated = harness.client.post(
            "/api/v1/admin/operational-traces:export",
            json={"trace_ids": [trace_id]},
            headers=harness.write_headers,
        )
        assert repeated.content == exported.content
        with ZipFile(BytesIO(exported.content)) as archive:
            assert archive.namelist() == [
                "manifest.json",
                f"traces/{trace_id}.json",
            ]
            manifest = json.loads(archive.read("manifest.json"))
            trace_bytes = archive.read(f"traces/{trace_id}.json")
        assert manifest["items"] == [
            {
                "trace_id": trace_id,
                "path": f"traces/{trace_id}.json",
                "sha256": hashlib.sha256(trace_bytes).hexdigest(),
                "bytes": len(trace_bytes),
            }
        ]
    finally:
        harness.close()


def test_trace_token_scopes_and_session_csrf_are_independent(
    tmp_path: Path,
) -> None:
    """summary/detail/full/export 各自授权，Session 写操作仍校验 CSRF。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        failed = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer",
            json={"query": "尚未建立索引"},
            headers=harness.write_headers,
        )
        trace_id = failed.json()["error"]["trace_id"]
        summary = harness.runtime.auth.create_access_token(
            name="Trace summary",
            scopes=("trace:summary",),
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        detail = harness.runtime.auth.create_access_token(
            name="Trace detail",
            scopes=("trace:detail",),
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        harness.client.cookies.clear()
        summary_headers = {"Authorization": f"Bearer {summary.token}"}
        detail_headers = {"Authorization": f"Bearer {detail.token}"}
        assert (
            harness.client.get(
                "/api/v1/admin/operational-traces",
                headers=summary_headers,
                params={
                    "project_id": project_id,
                    "knowledge_base_id": knowledge_base_id,
                },
            ).status_code
            == 200
        )
        assert (
            harness.client.get(
                f"/api/v1/admin/operational-traces/{trace_id}",
                headers=summary_headers,
            ).status_code
            == 403
        )
        assert (
            harness.client.get(
                f"/api/v1/admin/operational-traces/{trace_id}",
                headers=detail_headers,
            ).status_code
            == 200
        )
        assert (
            harness.client.get(
                f"/api/v1/admin/operational-traces/{trace_id}/export",
                headers=detail_headers,
            ).status_code
            == 403
        )

        login = harness.client.post(
            "/api/v1/console/session",
            json={"bootstrap_token": harness.bootstrap_token},
        )
        assert login.status_code == 200
        csrf_missing = harness.client.post(
            "/api/v1/admin/operational-traces:export",
            json={"trace_ids": [trace_id]},
        )
        assert csrf_missing.status_code == 403
        assert csrf_missing.json()["error"]["code"] == "CSRF_REQUIRED"
    finally:
        harness.close()
