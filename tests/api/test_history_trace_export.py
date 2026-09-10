"""History + Operational Trace 支持包的稳定产品合同。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

import rag_app.api.operational_trace as operational_trace_module
import rag_app.product.history_trace_export as export_module
from rag_app.core.errors import Conflict, IndexNotReady
from rag_app.core.identifiers import new_id
from rag_app.core.models import KnowledgeBaseScope
from rag_app.product.history_trace_export import HistoryTraceExportService
from rag_app.tracing.store import (
    ArtifactExpiredError,
    ArtifactIntegrityError,
    TraceNotFoundError,
)
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)


def _failed_query(
    harness: ProductHarness,
) -> tuple[str, str, str, str]:
    """创建不依赖 Provider 的失败查询及其双存储记录。"""
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    question = "尚未建立索引时的支持包问题"
    response = harness.client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}:answer",
        json={"query": question},
        headers=harness.write_headers,
    )
    assert response.status_code == 409
    return (
        project_id,
        knowledge_base_id,
        str(response.json()["error"]["trace_id"]),
        question,
    )


def _zip_members(payload: bytes) -> tuple[list[str], dict[str, bytes]]:
    """返回 ZIP 稳定顺序及全部成员内容。"""
    with ZipFile(BytesIO(payload)) as archive:
        names = archive.namelist()
        return names, {name: archive.read(name) for name in names}


def test_support_zip_is_canonical_and_body_requires_explicit_opt_in(
    tmp_path: Path,
) -> None:
    """同一快照与冻结时间字节一致，默认不包含问答正文。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, trace_id, question = _failed_query(harness)
        response = harness.client.get(
            f"/api/v1/admin/history-traces/{trace_id}/export"
        )
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/zip"
        assert int(response.headers["Content-Length"]) == len(response.content)
        assert (
            response.headers["X-Archive-SHA256"]
            == hashlib.sha256(response.content).hexdigest()
        )
        names, members = _zip_members(response.content)
        assert names == [
            "MANIFEST.json",
            f"items/{trace_id}/history.json",
            f"items/{trace_id}/operational-trace.json",
        ]
        manifest = json.loads(members["MANIFEST.json"])
        history = json.loads(members[f"items/{trace_id}/history.json"])
        assert manifest["requested_trace_ids"] == [trace_id]
        assert manifest["items"][0]["history_status"] == "AVAILABLE"
        assert manifest["items"][0]["body_included"] is False
        assert (
            manifest["items"][0]["body_unavailable_reason"] == "NOT_REQUESTED"
        )
        assert "question" not in history
        assert (
            sum(item["bytes"] for item in manifest["members"])
            == (manifest["total_uncompressed_bytes"])
        )

        body_response = harness.client.get(
            f"/api/v1/admin/history-traces/{trace_id}/export",
            params={"include_history_body": "true"},
        )
        assert body_response.status_code == 200
        _, body_members = _zip_members(body_response.content)
        body_history = json.loads(
            body_members[f"items/{trace_id}/history.json"]
        )
        assert body_history["body_included"] is True
        assert body_history["question"] == question

        service = HistoryTraceExportService(
            harness.runtime.history,
            harness.runtime.traces,
            source_revision=harness.runtime.traces.release_revision,
        )
        frozen_at = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        first = b"".join(
            service.export(
                (trace_id,),
                include_history_body=False,
                body_authorized=True,
                generated_at=frozen_at,
            ).iter_bytes()
        )
        second = b"".join(
            service.export(
                (trace_id,),
                include_history_body=False,
                body_authorized=True,
                generated_at=frozen_at,
            ).iter_bytes()
        )
        assert first == second
        with harness.runtime.connections.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM history_export_leases"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM history_export_journal "
                    "WHERE state='COMPLETED'"
                ).fetchone()[0]
                >= 4
            )
        assert (
            harness.runtime.traces.set_feedback(trace_id, useful=True) is True
        )
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE query_history SET project_id=? WHERE trace_id=?",
                ("prj_" + "f" * 32, trace_id),
            )
        unavailable_body = harness.client.get(
            f"/api/v1/admin/history-traces/{trace_id}/export",
            params={"include_history_body": "true"},
        )
        assert unavailable_body.status_code == 200
        _, unavailable_members = _zip_members(unavailable_body.content)
        unavailable_history = json.loads(
            unavailable_members[f"items/{trace_id}/history.json"]
        )
        assert unavailable_history["body_included"] is False
        assert unavailable_history["body_unavailable_reason"] == (
            "SCOPE_UNAVAILABLE"
        )
        assert "question" not in unavailable_history
    finally:
        harness.close()


def test_current_trace_accepts_bare_alias_without_duplicate_identity(
    tmp_path: Path,
) -> None:
    """当前 canonical 记录可用裸 ID 读取，规范化重复项仍返回 422。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, trace_id, question = _failed_query(harness)
        legacy_alias = trace_id.removeprefix("trace_")
        history_alias = harness.client.get(f"/api/v1/history/{legacy_alias}")
        assert history_alias.status_code == 200
        assert history_alias.json()["trace_id"] == trace_id
        detail_alias = harness.client.get(
            f"/api/v1/admin/operational-traces/{legacy_alias}"
        )
        assert detail_alias.status_code == 200
        assert detail_alias.json()["trace"]["trace_id"] == trace_id
        technical_alias = harness.client.get(
            f"/api/v1/admin/operational-traces/{legacy_alias}/export"
        )
        assert technical_alias.status_code == 200
        assert technical_alias.json()["trace"]["trace_id"] == trace_id
        support_alias = harness.client.get(
            f"/api/v1/admin/history-traces/{legacy_alias}/export",
            params={"include_history_body": "true"},
        )
        assert support_alias.status_code == 200
        _, alias_members = _zip_members(support_alias.content)
        alias_history = json.loads(
            alias_members[f"items/{legacy_alias}/history.json"]
        )
        alias_operation = json.loads(
            alias_members[f"items/{legacy_alias}/operational-trace.json"]
        )
        assert alias_history["question"] == question
        assert alias_operation["trace"]["trace_id"] == trace_id
        for endpoint in (
            "/api/v1/admin/history-traces:export",
            "/api/v1/admin/operational-traces:export",
        ):
            alias_duplicate = harness.client.post(
                endpoint,
                headers=harness.write_headers,
                json={"trace_ids": [trace_id, legacy_alias]},
            )
            assert alias_duplicate.status_code == 422
    finally:
        harness.close()


def test_legacy_flat_and_history_only_use_the_same_export_resolver(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """两种旧 ID 均可查看和下载，但不伪造 span 或模型调用。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        legacy_id = "a" * 32
        occurred_at = datetime(2026, 9, 9, 10, 0, tzinfo=UTC).isoformat()
        event = {
            "schema_version": "1",
            "trace_id": legacy_id,
            "event_name": "retrieval.snapshot",
            "occurred_at": occurred_at,
            "attributes": {},
        }
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO query_trace_events("
                "trace_id, occurred_at, event_name, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    legacy_id,
                    occurred_at,
                    "retrieval.snapshot",
                    json.dumps(event, separators=(",", ":")),
                ),
            )

        legacy_detail = harness.client.get(
            f"/api/v1/admin/operational-traces/{legacy_id}"
        )
        assert legacy_detail.status_code == 200
        assert legacy_detail.json()["trace"]["schema_version"] == (
            "legacy-flat-1"
        )
        legacy_export = harness.client.get(
            f"/api/v1/admin/operational-traces/{legacy_id}/export"
        )
        assert legacy_export.status_code == 200
        exported_event = legacy_export.json()["legacy_flat_events"][0]
        assert exported_event["trace_id"] == legacy_id
        assert exported_event["event_name"] == event["event_name"]
        assert exported_event["attributes"] == []
        support = harness.client.get(
            f"/api/v1/admin/history-traces/{legacy_id}/export"
        )
        assert support.status_code == 200
        _, members = _zip_members(support.content)
        manifest = json.loads(members["MANIFEST.json"])
        assert manifest["items"][0]["operational_trace_schema"] == (
            "legacy-flat-1"
        )
        assert manifest["items"][0]["capture_complete"] is False
        assert (
            harness.runtime.traces.set_feedback(legacy_id, useful=True) is False
        )

        history_only_id = "b" * 32
        harness.runtime.history.start(
            history_only_id,
            KnowledgeBaseScope(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            ),
            "迁移前仅保存了问答历史",
            owner_id="local-admin",
            save_body=True,
            conversation_context_digest="e" * 64,
        )
        history_only_detail = harness.client.get(
            f"/api/v1/admin/operational-traces/{history_only_id}"
        )
        assert history_only_detail.status_code == 200
        assert history_only_detail.json()["trace"] == {
            "trace_id": history_only_id,
            "schema_version": "missing-pre-v3",
            "capture_complete": False,
            "status": "NOT_CAPTURED_BEFORE_V3",
            "project_id": project_id,
            "knowledge_base_id": knowledge_base_id,
        }
        history_only_export = harness.client.get(
            f"/api/v1/admin/operational-traces/{history_only_id}/export"
        )
        assert history_only_export.status_code == 200
        assert history_only_export.json()["status"] == (
            "NOT_CAPTURED_BEFORE_V3"
        )
        history_only_support = harness.client.get(
            f"/api/v1/admin/history-traces/{history_only_id}/export"
        )
        _, history_only_members = _zip_members(history_only_support.content)
        history_payload = json.loads(
            history_only_members[f"items/{history_only_id}/history.json"]
        )
        assert history_payload["conversation_context_digest"] == "e" * 64
        batch = harness.client.post(
            "/api/v1/admin/history-traces:export",
            headers=harness.write_headers,
            json={
                "trace_ids": [history_only_id, legacy_id],
                "include_history_body": False,
            },
        )
        assert batch.status_code == 200
        batch_names, batch_members = _zip_members(batch.content)
        assert batch_names == [
            "MANIFEST.json",
            f"items/{legacy_id}/history.json",
            f"items/{legacy_id}/operational-trace.json",
            f"items/{history_only_id}/history.json",
            f"items/{history_only_id}/operational-trace.json",
        ]
        batch_manifest = json.loads(batch_members["MANIFEST.json"])
        assert batch_manifest["item_count"] == 2
        assert batch_manifest["canonical_order"] == [
            legacy_id,
            history_only_id,
        ]
        technical_batch = harness.client.post(
            "/api/v1/admin/operational-traces:export",
            headers=harness.write_headers,
            json={"trace_ids": [history_only_id, legacy_id]},
        )
        assert technical_batch.status_code == 200
        technical_names, _ = _zip_members(technical_batch.content)
        assert technical_names == [
            "manifest.json",
            f"traces/{legacy_id}.json",
            f"traces/{history_only_id}.json",
        ]
        assert (
            harness.runtime.traces.set_feedback(history_only_id, useful=False)
            is False
        )
        with pytest.raises(TraceNotFoundError):
            harness.runtime.traces.set_feedback("d" * 32, useful=True)
    finally:
        harness.close()


def test_export_validation_missing_limits_and_history_clear_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """422、404、413、409 与 History-only clear 语义保持稳定。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, trace_id, _ = _failed_query(harness)
        duplicate = harness.client.post(
            "/api/v1/admin/history-traces:export",
            headers=harness.write_headers,
            json={"trace_ids": [trace_id, trace_id]},
        )
        assert duplicate.status_code == 422
        technical_duplicate = harness.client.post(
            "/api/v1/admin/operational-traces:export",
            headers=harness.write_headers,
            json={"trace_ids": [trace_id, trace_id]},
        )
        assert technical_duplicate.status_code == 422
        invalid = harness.client.get(
            "/api/v1/admin/history-traces/trace_NOT_HEX/export"
        )
        assert invalid.status_code == 422
        missing_id = "c" * 32
        missing = harness.client.get(
            f"/api/v1/admin/history-traces/{missing_id}/export"
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["details"]["missing_trace_ids"] == [
            missing_id
        ]
        partial_missing = harness.client.post(
            "/api/v1/admin/history-traces:export",
            headers=harness.write_headers,
            json={"trace_ids": [trace_id, missing_id]},
        )
        assert partial_missing.status_code == 404
        assert partial_missing.json()["error"]["details"][
            "missing_trace_ids"
        ] == [missing_id]

        with harness.runtime.history.export_snapshots(
            (trace_id,),
            include_body=False,
            body_authorized=True,
        ):
            locked = harness.client.delete(
                "/api/v1/history", headers=harness.write_headers
            )
            assert locked.status_code == 409
            with pytest.raises(Conflict):
                harness.runtime.history.clear()

        monkeypatch.setattr(export_module, "MAX_HISTORY_TRACE_MEMBER_BYTES", 1)
        oversized = harness.client.get(
            f"/api/v1/admin/history-traces/{trace_id}/export"
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == (
            "EXPORT_MEMBER_BYTES_EXCEEDED"
        )
        monkeypatch.undo()

        cleared = harness.client.delete(
            "/api/v1/history", headers=harness.write_headers
        )
        assert cleared.status_code == 204
        assert (
            harness.client.get(
                f"/api/v1/admin/operational-traces/{trace_id}"
            ).status_code
            == 200
        )
        trace_only = harness.client.get(
            f"/api/v1/admin/history-traces/{trace_id}/export"
        )
        assert trace_only.status_code == 200
        _, trace_only_members = _zip_members(trace_only.content)
        trace_only_manifest = json.loads(trace_only_members["MANIFEST.json"])
        assert trace_only_manifest["items"][0]["history_status"] == "MISSING"
        assert (
            trace_only_manifest["items"][0]["body_unavailable_reason"]
            == "HISTORY_MISSING"
        )
        issued = harness.runtime.auth.create_access_token(
            name="technical trace only",
            scopes=("trace:export",),
        )
        harness.client.cookies.clear()
        denied = harness.client.get(
            f"/api/v1/admin/history-traces/{trace_id}/export",
            headers={"Authorization": f"Bearer {issued.token}"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "TOKEN_DENIED"
    finally:
        harness.close()


def test_technical_batch_authorizes_all_scopes_before_loading_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨 scope 批量请求必须在读取任一 Artifact 前失败关闭。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id, allowed_trace, _ = _failed_query(harness)
        second_project = harness.client.post(
            "/api/v1/projects",
            headers={
                **harness.write_headers,
                "Idempotency-Key": "technical-export-project-2",
            },
            json={"name": "技术导出隔离项目"},
        )
        second_project.raise_for_status()
        second_project_id = str(second_project.json()["project_id"])
        second_kb = harness.client.post(
            f"/api/v1/projects/{second_project_id}/knowledge-bases",
            headers={
                **harness.write_headers,
                "Idempotency-Key": "technical-export-kb-2",
            },
            json={"name": "技术导出隔离知识库"},
        )
        second_kb.raise_for_status()
        second_kb_id = str(second_kb.json()["knowledge_base_id"])
        denied_query = harness.client.post(
            f"/api/v1/projects/{second_project_id}/knowledge-bases/"
            f"{second_kb_id}:answer",
            headers=harness.write_headers,
            json={"query": "另一个 scope 的技术 Trace"},
        )
        assert denied_query.status_code == 409
        denied_trace = str(denied_query.json()["error"]["trace_id"])
        issued = harness.runtime.auth.create_access_token(
            name="scoped technical export",
            scopes=("trace:export",),
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        payload_loads = 0

        def _reject_payload_load(_trace_id: str) -> bytes:
            nonlocal payload_loads
            payload_loads += 1
            raise AssertionError("未完成整批授权前不得物化 payload")

        monkeypatch.setattr(
            harness.runtime.traces.store,
            "export_trace",
            _reject_payload_load,
        )
        harness.client.cookies.clear()

        response = harness.client.post(
            "/api/v1/admin/operational-traces:export",
            params={
                "project_id": project_id,
                "knowledge_base_id": knowledge_base_id,
            },
            headers={"Authorization": f"Bearer {issued.token}"},
            json={"trace_ids": [allowed_trace, denied_trace]},
        )

        assert response.status_code == 403
        assert response.json()["error"]["stage"] == "trace.scope"
        assert payload_loads == 0
    finally:
        harness.close()


def test_conversation_digest_is_safe_history_and_trace_metadata(
    tmp_path: Path,
) -> None:
    """会话上下文只以摘要和存在标记进入双存储。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        trace_id = new_id("trace")
        digest = "f" * 64
        harness.runtime.traces.start(
            trace_id,
            KnowledgeBaseScope(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            ),
            "不应进入技术 Trace 的上下文正文",
            owner_id="local-admin",
            save_body=True,
            conversation_context_digest=digest,
        )
        harness.runtime.traces.finish(
            trace_id,
            result=None,
            error=IndexNotReady("测试索引未就绪。", stage="test.query"),
            cancelled=False,
        )

        history = harness.runtime.history.detail(trace_id)
        assert history["conversation_context_digest"] == digest
        detail = harness.runtime.traces.detail(trace_id)
        admission = next(
            span for span in detail.spans if span.name == "request.admission"
        )
        attributes = dict(admission.attributes)
        assert attributes["conversation_context_present"] is True
        assert attributes["conversation_context_digest"] == digest
        assert "上下文正文" not in json.dumps(attributes, ensure_ascii=False)
    finally:
        harness.close()


def test_export_store_unavailable_is_stable_503(tmp_path: Path) -> None:
    """Trace Store 关闭不能被误报为不存在或请求错误。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, trace_id, _ = _failed_query(harness)
        harness.runtime.traces.store.close()
        support = harness.client.get(
            f"/api/v1/admin/history-traces/{trace_id}/export"
        )
        assert support.status_code == 503
        assert support.json()["error"]["code"] == (
            "TRACE_PERSISTENCE_UNAVAILABLE"
        )
        technical = harness.client.get(
            f"/api/v1/admin/operational-traces/{trace_id}/export"
        )
        assert technical.status_code == 503
        assert technical.json()["error"]["code"] == (
            "TRACE_PERSISTENCE_UNAVAILABLE"
        )
    finally:
        harness.close()


def test_expired_artifact_is_stable_404(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前 Trace 的 Artifact 到期不能冒充 Trace 缺失或内部错误。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, trace_id, _ = _failed_query(harness)

        def _expired(_trace_id: str) -> bytes:
            raise ArtifactExpiredError(_trace_id)

        monkeypatch.setattr(
            harness.runtime.traces.store,
            "export_trace",
            _expired,
        )
        support = harness.client.get(
            f"/api/v1/admin/history-traces/{trace_id}/export"
        )
        assert support.status_code == 404
        assert support.json()["error"]["stage"] == "history_trace.export"
        technical = harness.client.get(
            f"/api/v1/admin/operational-traces/{trace_id}/export"
        )
        assert technical.status_code == 404
        assert technical.json()["error"]["stage"] == "trace.export"
    finally:
        harness.close()


def test_corrupt_artifact_is_stable_safe_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """损坏 Artifact 必须失败关闭，且响应不能泄漏内部载荷。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, trace_id, question = _failed_query(harness)

        def _corrupt(_trace_id: str) -> bytes:
            raise ArtifactIntegrityError("private compressed payload")

        monkeypatch.setattr(
            harness.runtime.traces.store,
            "export_trace",
            _corrupt,
        )
        for path in (
            f"/api/v1/admin/history-traces/{trace_id}/export",
            f"/api/v1/admin/operational-traces/{trace_id}/export",
        ):
            response = harness.client.get(path)
            assert response.status_code == 503
            payload = response.json()["error"]
            assert payload["code"] == "TRACE_ARTIFACT_CORRUPT"
            assert payload["stage"] == "trace.export"
            assert question not in response.text
            assert "private compressed payload" not in response.text
    finally:
        harness.close()


def test_technical_export_limit_is_stable_413(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """技术单条导出也在发送响应前执行实际累计上限。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, trace_id, _ = _failed_query(harness)
        monkeypatch.setattr(operational_trace_module, "_MAX_BATCH_BYTES", 1)
        response = harness.client.get(
            f"/api/v1/admin/operational-traces/{trace_id}/export"
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == (
            "TRACE_EXPORT_TOTAL_BYTES_EXCEEDED"
        )
    finally:
        harness.close()
