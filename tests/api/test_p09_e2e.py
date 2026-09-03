"""P09 API 对 P08.5 检索、错误和上传合同的端到端回归。"""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic, sleep

from fastapi.testclient import TestClient

from rag_app.api.p09 import create_p09_app
from rag_app.composition.p09_runtime import P09Runtime, build_p09_runtime
from tests.adapters.parsers.docx.fixtures import build_package

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_ADMIN = {"Authorization": "Bearer admin-secret"}
_QUERY = {"Authorization": "Bearer query-secret"}
_ADMIN_TOKEN = "admin-secret"  # noqa: S105
_QUERY_TOKEN = "query-secret"  # noqa: S105


def _client(
    runtime: P09Runtime, *, max_upload_bytes: int = 1024 * 1024
) -> TestClient:
    return TestClient(
        create_p09_app(
            runtime,
            query_token=_QUERY_TOKEN,
            admin_token=_ADMIN_TOKEN,
            debug_enabled=True,
            max_upload_bytes=max_upload_bytes,
        )
    )


def _scope(client: TestClient) -> tuple[str, str]:
    project = client.post(
        "/api/v1/projects",
        json={"name": "项目"},
        headers={**_ADMIN, "Idempotency-Key": "project-key"},
    )
    assert project.status_code == 201
    project_id = str(project.json()["project_id"])
    knowledge_base = client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases",
        json={"name": "知识库"},
        headers={**_ADMIN, "Idempotency-Key": "kb-key"},
    )
    assert knowledge_base.status_code == 201
    return project_id, str(knowledge_base.json()["knowledge_base_id"])


def _upload(
    client: TestClient,
    project_id: str,
    knowledge_base_id: str,
    *,
    key: str = "document-key",
    text: str = "中文普通短语 财务制度",
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}/documents",
        params={"display_name": f"{key}.docx"},
        content=build_package(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"),
        headers={
            **_ADMIN,
            "Idempotency-Key": key,
            "Content-Type": _MEDIA_TYPE,
        },
    )
    assert response.status_code == 202, response.text
    return _wait_job(client, dict(response.json()))


def _wait_job(
    client: TestClient, job: dict[str, object]
) -> dict[str, object]:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = client.get(f"/api/v1/jobs/{job['job_id']}", headers=_ADMIN)
        assert current.status_code == 200, current.text
        job = dict(current.json())
        if job["state"] not in {"queued", "running"}:
            return job
        sleep(0.01)
    raise AssertionError(f"Job 未在期限内结束：{job['job_id']}")


def test_full_lifecycle_switches_revision_and_rejects_invalid_state(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project_id, knowledge_base_id = _scope(client)
        first = _upload(
            client, project_id, knowledge_base_id, text="旧版财务制度"
        )
        document_id = str(first["document_id"])
        document_path = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/documents/{document_id}"
        )
        before = client.get(document_path, headers=_ADMIN).json()
        renamed = client.patch(
            document_path,
            json={"display_name": "重命名财务制度.docx"},
            headers=_ADMIN,
        )
        assert renamed.status_code == 200
        assert (
            renamed.json()["current_version_id"]
            == before["current_version_id"]
        )
        assert (
            renamed.json()["active_index_revision_id"]
            == before["active_index_revision_id"]
        )

        version_response = client.post(
            document_path + "/versions",
            content=build_package(
                "<w:p><w:r><w:t>新版差旅报销制度</w:t></w:r></w:p>"
            ),
            headers={
                **_ADMIN,
                "Idempotency-Key": "new-version",
                "Content-Type": _MEDIA_TYPE,
            },
        )
        assert version_response.status_code == 202, version_response.text
        second = _wait_job(client, dict(version_response.json()))
        after = client.get(document_path, headers=_ADMIN).json()
        versions = client.get(document_path + "/versions", headers=_ADMIN)

        assert second["state"] == "succeeded"
        assert second["revision_id"] != first["revision_id"]
        assert after["active_index_revision_id"] == second["revision_id"]
        assert after["current_version_id"] == second["document_version_id"]
        assert [item["status"] for item in versions.json()["items"]] == [
            "superseded",
            "ready",
        ]

        deleted = client.delete(document_path, headers=_ADMIN)
        invalid_rename = client.patch(
            document_path,
            json={"display_name": "禁止重命名.docx"},
            headers=_ADMIN,
        )
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleting"
        assert invalid_rename.status_code == 409
        assert invalid_rename.json()["error"]["code"] == "REVISION_STATE_ERROR"


def test_cjk_fts_v2_cache_and_public_diagnostics(tmp_path: Path) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project_id, knowledge_base_id = _scope(client)
        job = _upload(client, project_id, knowledge_base_id)
        path = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:search"
        )

        first = client.post(
            path,
            json={"query": "普通中文短语", "limit": 5},
            headers=_QUERY,
        )
        second = client.post(
            path,
            json={"query": "普通中文短语", "limit": 5},
            headers=_QUERY,
        )
        status = client.get("/api/v1/system/components", headers=_ADMIN)

        assert first.status_code == second.status_code == 200
        assert first.json()["evidence_count"] >= 1
        assert second.json()["cache_hit"] is True
        assert second.json()["diagnostics_summary"]["provider_call_count"] == 0
        assert "diagnostics" not in second.json()
        assert second.json()["active_index_revision_id"] == job["revision_id"]
        assert second.json()["project_id"] == project_id
        assert second.json()["knowledge_base_id"] == knowledge_base_id
        assert status.json()["lexical_schema"] == "fts-v2"
        assert status.json()["reindex_required"] is False
        assert status.json()["remote_dense_confidence_calibrated"] is False
        assert status.json()["remote_production_profile_ready"] is False


def test_debug_diagnostics_and_sse_final_are_controlled(tmp_path: Path) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project_id, knowledge_base_id = _scope(client)
        _upload(client, project_id, knowledge_base_id)
        path = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer"
        )
        regular = client.post(
            path,
            json={"query": "财务制度", "limit": 5},
            headers=_QUERY,
        )
        streamed = client.post(
            path,
            json={"query": "财务制度", "limit": 5, "stream": True},
            headers=_QUERY,
        )
        trace_id = regular.json()["trace_id"]
        diagnostic = client.get(
            f"/api/v1/admin/retrieval-diagnostics/{trace_id}",
            headers=_ADMIN,
        )
        safe_trace = client.get(
            "/api/v1/admin/traces",
            params={"query_id": trace_id},
            headers=_ADMIN,
        )
        disabled = TestClient(
            create_p09_app(
                runtime,
                query_token=_QUERY_TOKEN,
                admin_token=_ADMIN_TOKEN,
                debug_enabled=False,
            )
        ).get(
            f"/api/v1/admin/retrieval-diagnostics/{trace_id}",
            headers=_ADMIN,
        )

        final_line = next(
            line
            for event, line in zip(
                streamed.text.splitlines(),
                streamed.text.splitlines()[1:],
                strict=False,
            )
            if event == "event: final" and line.startswith("data: ")
        )
        final = json.loads(final_line.removeprefix("data: "))
        assert diagnostic.status_code == 200
        assert safe_trace.status_code == 200
        assert safe_trace.json()["trace_id"] == trace_id
        assert safe_trace.json()["events"]
        assert disabled.status_code == 403
        assert final["answer"] == regular.json()["answer"]
        assert final["evidence"] == regular.json()["evidence"]
        assert (
            final["active_index_revision_id"]
            == regular.json()["active_index_revision_id"]
        )
        assert final["evidence_count"] <= 8


def test_index_corruption_and_fts_v1_have_distinct_errors(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path / "corrupt") as runtime:
        client = _client(runtime)
        project_id, knowledge_base_id = _scope(client)
        job = _upload(client, project_id, knowledge_base_id)
        with runtime.retrieval_runtime.persistence.connections.transaction(
            write=True
        ) as connection:
            connection.execute(
                "DELETE FROM revision_embedding_coverage WHERE revision_id=?",
                (job["revision_id"],),
            )
        response = client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:search",
            json={"query": "财务制度"},
            headers=_QUERY,
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INDEX_CORRUPT"

    with build_p09_runtime(_PROFILE, data_dir=tmp_path / "fts-v1") as runtime:
        client = _client(runtime)
        project_id, knowledge_base_id = _scope(client)
        job = _upload(client, project_id, knowledge_base_id)
        legacy = json.dumps(
            {"fts_schema_version": "1", "analyzer_id": "legacy"}
        )
        with runtime.retrieval_runtime.persistence.connections.transaction(
            write=True
        ) as connection:
            connection.execute(
                "UPDATE index_revisions SET lexical_schema_json=? "
                "WHERE index_revision_id=?",
                (legacy, job["revision_id"]),
            )
        status = client.get("/api/v1/system/components", headers=_ADMIN)
        response = client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:search",
            json={"query": "财务制度"},
            headers=_QUERY,
        )
        assert status.json()["reindex_required"] is True
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REINDEX_REQUIRED"


def test_upload_limit_idempotency_and_spool_cleanup(tmp_path: Path) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime, max_upload_bytes=8)
        project_id, knowledge_base_id = _scope(client)
        conflicting = client.post(
            "/api/v1/projects",
            json={"name": "另一个项目"},
            headers={**_ADMIN, "Idempotency-Key": "project-key"},
        )
        oversized = client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/documents",
            params={"display_name": "safe.docx"},
            content=b"123456789",
            headers={
                **_ADMIN,
                "Idempotency-Key": "oversized",
                "Content-Type": _MEDIA_TYPE,
            },
        )

        assert conflicting.status_code == 409
        assert conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
        assert not tuple((tmp_path / "upload-spool").iterdir())


def test_artifact_download_requires_complete_reference_scope(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project_id, knowledge_base_id = _scope(client)
        first = _upload(client, project_id, knowledge_base_id, key="first")
        second = _upload(
            client,
            project_id,
            knowledge_base_id,
            key="second",
            text="完全不同的受控内容",
        )
        first_versions = runtime.sdk.list_document_versions(
            project_id, knowledge_base_id, str(first["document_id"])
        )
        second_versions = runtime.sdk.list_document_versions(
            project_id, knowledge_base_id, str(second["document_id"])
        )
        artifact_id = first_versions[0].source_artifact_id
        wrong_scope = client.get(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/artifacts/{artifact_id}",
            params={
                "document_id": second["document_id"],
                "document_version_id": second_versions[0].document_version_id,
            },
            headers=_ADMIN,
        )
        right_scope = client.get(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/artifacts/{artifact_id}",
            params={
                "document_id": first["document_id"],
                "document_version_id": first_versions[0].document_version_id,
            },
            headers=_ADMIN,
        )

        assert wrong_scope.status_code == 404
        assert right_scope.status_code == 200
        assert right_scope.content
