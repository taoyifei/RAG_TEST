from pathlib import Path

from fastapi.testclient import TestClient

from rag_app.api.p09 import create_p09_app
from rag_app.composition.p09_runtime import build_p09_runtime

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_QUERY_TOKEN = "query-secret"  # noqa: S105
_ADMIN_TOKEN = "admin-secret"  # noqa: S105


def test_v1_requires_auth_and_exposes_stable_error_envelope(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = TestClient(
            create_p09_app(
                runtime,
                query_token=_QUERY_TOKEN,
                admin_token=_ADMIN_TOKEN,
            )
        )
        unauthorized = client.get("/api/v1/projects")
        missing = client.get(
            "/api/v1/projects/prj_00000000000000000000000000000000",
            headers={"Authorization": "Bearer admin-secret"},
        )

        assert unauthorized.status_code == 401
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        assert "trace_id" in missing.json()["error"]


def test_openapi_declares_v1_lifecycle_and_debug_is_admin_only(
    tmp_path: Path,
) -> None:
    with build_p09_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        client = TestClient(
            create_p09_app(
                runtime,
                query_token=_QUERY_TOKEN,
                admin_token=_ADMIN_TOKEN,
            )
        )
        schema = client.app.openapi()
        forbidden = client.get(
            "/api/v1/admin/retrieval-diagnostics/trace_00000000000000000000000000000000",
            headers={"Authorization": "Bearer query-secret"},
        )

        assert "/api/v1/projects" in schema["paths"]
        search_path = (
            "/api/v1/projects/{project_id}/knowledge-bases/{kb_id}:search"
        )
        assert search_path in schema["paths"]
        assert forbidden.status_code == 403
