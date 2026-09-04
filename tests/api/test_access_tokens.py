"""作用域 API Token 的一次显示、HMAC、授权和吊销回归。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
)


def test_access_token_is_one_time_scoped_hashed_and_revocable(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        issued = harness.client.post(
            "/api/v1/access-tokens",
            headers=harness.write_headers,
            json={
                "name": "查询集成",
                "scopes": ["query:read"],
                "project_id": project_id,
                "knowledge_base_id": knowledge_base_id,
            },
        )
        issued.raise_for_status()
        token = str(issued.json()["token"])
        token_id = str(issued.json()["token_id"])
        listed = harness.client.get("/api/v1/access-tokens")
        database = (
            harness.runtime.settings.data_dir / "universal-rag.sqlite3"
        ).read_bytes()

        assert token not in listed.text
        assert token.encode() not in database
        external = TestClient(harness.client.app)
        allowed = external.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:search",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "青岛啤酒"},
        )
        assert allowed.status_code != 403
        denied = external.get(
            "/api/v1/system/components",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert denied.status_code == 403

        revoked = harness.client.post(
            f"/api/v1/access-tokens/{token_id}:revoke",
            headers=harness.write_headers,
        )
        revoked.raise_for_status()
        after_revoke = external.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:search",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": "青岛啤酒"},
        )
        assert after_revoke.status_code == 403
        external.close()
    finally:
        harness.close()
