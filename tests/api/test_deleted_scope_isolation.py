"""项目归档和知识库逻辑删除后的作用域隔离回归。"""

from pathlib import Path

from httpx import Response

from rag_app.composition.p09_runtime import build_p09_runtime
from tests.adapters.parsers.docx.fixtures import build_package
from tests.api.test_p09_e2e import (
    _ADMIN,
    _MEDIA_TYPE,
    _PROFILE,
    _QUERY,
    _client,
    _scope,
    _upload,
)
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
)


def _assert_inactive(response: Response, stage: str) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "REVISION_STATE_ERROR"
    assert response.json()["error"]["stage"] == stage


def test_deleted_knowledge_base_blocks_old_urls_and_archived_project_scope(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project_id, knowledge_base_id = _scope(client)
        job = _upload(client, project_id, knowledge_base_id)
        base = (
            f"/api/v1/projects/{project_id}/knowledge-bases/{knowledge_base_id}"
        )
        document_id = str(job["document_id"])
        version_id = str(job["document_version_id"])
        document_path = base + f"/documents/{document_id}"
        version_path = document_path + f"/versions/{version_id}"
        version = client.get(version_path, headers=_ADMIN)
        assert version.status_code == 200, version.text
        artifact_id = str(version.json()["source_artifact_id"])
        artifact_path = (
            base
            + f"/artifacts/{artifact_id}?document_id={document_id}"
            + f"&document_version_id={version_id}"
        )

        deleted = client.delete(base, headers=_ADMIN)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "deleting"

        blocked = (
            client.get(base + "/documents", headers=_ADMIN),
            client.get(document_path, headers=_ADMIN),
            client.get(version_path, headers=_ADMIN),
            client.get(artifact_path, headers=_ADMIN),
            client.post(
                base + ":search",
                headers=_QUERY,
                json={"query": "财务制度", "limit": 5},
            ),
            client.post(
                base + "/documents",
                params={"display_name": "blocked.docx"},
                content=build_package(
                    "<w:p><w:r><w:t>不应重新入库</w:t></w:r></w:p>"
                ),
                headers={
                    **_ADMIN,
                    "Idempotency-Key": "blocked-upload",
                    "Content-Type": _MEDIA_TYPE,
                },
            ),
        )
        for response in blocked:
            _assert_inactive(response, "scope.knowledge_base")

        archived = client.delete(
            f"/api/v1/projects/{project_id}", headers=_ADMIN
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"
        _assert_inactive(
            client.get(base + "/documents", headers=_ADMIN),
            "scope.project",
        )
        _assert_inactive(
            client.get(
                f"/api/v1/projects/{project_id}/knowledge-bases",
                headers=_ADMIN,
            ),
            "scope.project",
        )


def test_deleted_knowledge_base_blocks_product_model_settings(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        settings_path = (
            f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings"
        )
        assert harness.client.get(settings_path).status_code == 200

        deleted = harness.client.delete(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}",
            headers=harness.write_headers,
        )
        assert deleted.status_code == 200, deleted.text
        _assert_inactive(
            harness.client.get(settings_path), "scope.knowledge_base"
        )
    finally:
        harness.close()


def test_archived_project_blocks_its_still_active_knowledge_base(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = _client(runtime)
        project_id, knowledge_base_id = _scope(client)
        base = (
            f"/api/v1/projects/{project_id}/knowledge-bases/{knowledge_base_id}"
        )
        assert (
            client.get(base + "/documents", headers=_ADMIN).status_code == 200
        )

        archived = client.delete(
            f"/api/v1/projects/{project_id}", headers=_ADMIN
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"

        blocked = (
            client.get(
                f"/api/v1/projects/{project_id}/knowledge-bases",
                headers=_ADMIN,
            ),
            client.get(base + "/documents", headers=_ADMIN),
            client.post(
                base + ":search",
                headers=_QUERY,
                json={"query": "财务制度", "limit": 5},
            ),
            client.post(
                base + "/documents",
                params={"display_name": "blocked-by-project.docx"},
                content=build_package(
                    "<w:p><w:r><w:t>归档项目不得重新入库</w:t></w:r></w:p>"
                ),
                headers={
                    **_ADMIN,
                    "Idempotency-Key": "blocked-by-project-upload",
                    "Content-Type": _MEDIA_TYPE,
                },
            ),
        )
        for response in blocked:
            _assert_inactive(response, "scope.project")
