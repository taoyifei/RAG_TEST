"""P10 控制台只读 API 与静态入口回归。"""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

from fastapi.testclient import TestClient

from rag_app.api.p10 import create_p10_app
from rag_app.composition.p09_runtime import build_p09_runtime
from tests.adapters.parsers.docx.fixtures import build_package

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_ADMIN = {"Authorization": "Bearer admin-secret"}
_QUERY = {"Authorization": "Bearer query-secret"}
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _client(tmp_path: Path) -> tuple[TestClient, object]:
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text(
        '<main id="root">P10 console</main>', encoding="utf-8"
    )
    (assets / "app.js").write_text("// built", encoding="utf-8")
    runtime = build_p09_runtime(_PROFILE, data_dir=tmp_path / "data")
    app = create_p10_app(
        runtime,
        query_token="query-secret",  # noqa: S106
        admin_token="admin-secret",  # noqa: S106
        debug_enabled=True,
        frontend_dir=frontend,
    )
    return TestClient(app), runtime


def _scope(client: TestClient) -> tuple[str, str]:
    project = client.post(
        "/api/v1/projects",
        json={"name": "P10 项目"},
        headers={**_ADMIN, "Idempotency-Key": "p10-project"},
    )
    project_id = str(project.json()["project_id"])
    knowledge_base = client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases",
        json={"name": "P10 知识库"},
        headers={**_ADMIN, "Idempotency-Key": "p10-kb"},
    )
    return project_id, str(knowledge_base.json()["knowledge_base_id"])


def _upload(
    client: TestClient, project_id: str, knowledge_base_id: str
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}/documents",
        params={"display_name": "青岛啤酒采购流程.docx"},
        content=build_package(
            "<w:p><w:r><w:t>青岛啤酒采购流程需要采购申请审批</w:t></w:r></w:p>"
        ),
        headers={
            **_ADMIN,
            "Idempotency-Key": "p10-document",
            "Content-Type": _MEDIA_TYPE,
        },
    )
    assert response.status_code == 202, response.text
    job = dict(response.json())
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = client.get(f"/api/v1/jobs/{job['job_id']}", headers=_ADMIN)
        job = dict(current.json())
        if job["state"] not in {"queued", "running"}:
            break
        sleep(0.01)
    assert job["state"] == "succeeded"
    return job


def test_console_lists_jobs_and_inspects_revision_chunks(
    tmp_path: Path,
) -> None:
    client, runtime = _client(tmp_path)
    try:
        project_id, knowledge_base_id = _scope(client)
        job = _upload(client, project_id, knowledge_base_id)
        revision_path = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/revisions/{job['revision_id']}"
        )

        jobs = client.get(
            "/api/v1/jobs",
            params={
                "project_id": project_id,
                "knowledge_base_id": knowledge_base_id,
            },
            headers=_ADMIN,
        )
        revision = client.get(revision_path, headers=_ADMIN)
        chunks = client.get(
            revision_path + "/chunks",
            params={"document_id": job["document_id"], "page_size": 20},
            headers=_ADMIN,
        )
        reports = client.get(revision_path + "/reports", headers=_ADMIN)
        search = client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:search",
            json={"query": "青岛啤酒", "limit": 5},
            headers=_QUERY,
        )
        diagnostics = client.get(
            "/api/v1/admin/retrieval-diagnostics/"
            + search.json()["trace_id"],
            headers=_ADMIN,
        )

        assert jobs.status_code == 200
        assert jobs.json()["items"][0]["job_id"] == job["job_id"]
        assert revision.status_code == 200
        detail = revision.json()
        assert detail["active"] is True
        assert detail["actual_document_count"] == 1
        assert detail["actual_chunk_count"] >= 1
        assert detail["fts_count"] == detail["actual_chunk_count"]
        assert detail["writer_status"] == "released"
        assert detail["slot_coverages"][0]["valid_vector_count"] >= 1
        assert chunks.status_code == 200
        first = chunks.json()["items"][0]
        assert "青岛啤酒采购流程" in first["citation_text"]
        assert first["embedding_text"]
        assert first["lexical_text"]
        assert first["source_spans"]
        assert reports.status_code == 200
        assert reports.json()["items"][0]["chunking_report"]["chunk_count"] >= 1
        assert search.status_code == 200
        evidence = search.json()["evidence"][0]
        assert evidence["selection_reason"] == "retrieval_candidate"
        assert evidence["table_context"] is False
        assert evidence["publishable"] is True
        assert diagnostics.status_code == 200
        assert diagnostics.json()["fusion"][0]["contributions"]
    finally:
        runtime.close()


def test_console_revision_scope_is_fail_closed(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    try:
        project_id, knowledge_base_id = _scope(client)
        job = _upload(client, project_id, knowledge_base_id)
        other_project = client.post(
            "/api/v1/projects",
            json={"name": "其他项目"},
            headers={**_ADMIN, "Idempotency-Key": "other-project"},
        ).json()["project_id"]
        wrong_scope = client.get(
            f"/api/v1/projects/{other_project}/knowledge-bases/"
            f"{knowledge_base_id}/revisions/{job['revision_id']}",
            headers=_ADMIN,
        )

        assert wrong_scope.status_code == 404
        assert wrong_scope.json()["error"]["code"] == "NOT_FOUND"
    finally:
        runtime.close()


def test_console_system_quality_and_spa_fallback(tmp_path: Path) -> None:
    client, runtime = _client(tmp_path)
    try:
        system = client.get("/api/v1/system/components", headers=_ADMIN)
        root = client.get("/")
        deep_link = client.get("/retrieval")
        asset = client.get("/assets/app.js")

        assert system.status_code == 200
        payload = system.json()
        assert payload["offline_evaluation_v3_ready"] is True
        assert payload["primary_live_evaluation_status"] == "not_verified"
        assert payload["standby_live_evaluation_status"] == "not_verified"
        assert payload["remote_production_profile_ready"] is False
        assert payload["lexical_analyzer_id"] == payload["analyzer_id"]
        assert payload["active_revision_schema"] == "chunk-v3/fts-v2"
        assert root.status_code == 200
        assert deep_link.status_code == 200
        assert "P10 console" in root.text
        assert root.text == deep_link.text
        assert asset.text == "// built"
    finally:
        runtime.close()
