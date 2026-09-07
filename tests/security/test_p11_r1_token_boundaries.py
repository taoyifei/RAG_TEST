"""P11-R1 外部 Token 控制面越权回归，全部使用临时库。"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from rag_app.api.product import create_product_app
from rag_app.product.models import ProviderConnection
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)


@pytest.mark.parametrize(
    "scope", ["system:read", "knowledge:write", "query:read"]
)
def test_external_tokens_cannot_write_control_plane(tmp_path: Path, scope: str):
    requests: list[httpx.Request] = []

    def transport(connection: ProviderConnection) -> httpx.MockTransport:
        del connection

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        return httpx.MockTransport(handler)

    harness = build_product_harness(tmp_path, transport_factory=transport)
    try:
        credential_id, _, connection_id, _ = create_provider_connections(
            harness
        )
        token = harness.runtime.auth.create_access_token(
            name="测试", scopes=(scope,)
        )
        before_credentials = harness.runtime.credentials.list()
        before_connections = harness.runtime.control.list_connections()
        before_tokens = harness.runtime.auth.list_access_tokens()
        with TestClient(create_product_app(harness.runtime)) as client:
            headers = {"Authorization": f"Bearer {token.token}"}
            routes = [
                ("POST", "/api/v1/provider-credentials", {}),
                (
                    "POST",
                    f"/api/v1/provider-credentials/{credential_id}:rotate",
                    {"secret_value": "synthetic-next"},
                ),
                ("POST", "/api/v1/provider-connections", {}),
                ("PATCH", f"/api/v1/provider-connections/{connection_id}", {}),
                ("DELETE", f"/api/v1/provider-connections/{connection_id}", {}),
                (
                    "POST",
                    f"/api/v1/provider-connections/{connection_id}:validate",
                    {
                        "operation": "embedding.document",
                        "model": "jina-embeddings-v5-text-small",
                    },
                ),
                ("POST", "/api/v1/system/providers:probe", {}),
                (
                    "POST",
                    "/api/v1/access-tokens",
                    {"name": "升级", "scopes": ["knowledge:write"]},
                ),
                ("POST", f"/api/v1/access-tokens/{token.token_id}:revoke", {}),
                ("POST", "/api/v1/retrieval-profiles/unknown:activate", {}),
                ("GET", "/api/v1/provider-credentials", None),
                ("GET", "/api/v1/unknown-sensitive-path", None),
            ]
            for method, path, body in routes:
                response = client.request(
                    method, path, headers=headers, json=body
                )
                assert response.status_code == 403, (
                    method,
                    path,
                    response.text,
                )
        assert requests == []
        assert harness.runtime.credentials.list() == before_credentials
        assert harness.runtime.control.list_connections() == before_connections
        assert harness.runtime.auth.list_access_tokens() == before_tokens
        assert harness.runtime.control.list_validations(connection_id) == ()
    finally:
        harness.close()


def test_scoped_queries_and_session_csrf(tmp_path: Path):
    harness = build_product_harness(tmp_path)
    try:
        project_id, kb_id = create_project_and_knowledge_base(harness)
        token = harness.runtime.auth.create_access_token(
            name="查询",
            scopes=("query:read",),
            project_id=project_id,
            knowledge_base_id=kb_id,
        )
        with TestClient(create_product_app(harness.runtime)) as client:
            headers = {"Authorization": f"Bearer {token.token}"}
            base = f"/api/v1/projects/{project_id}/knowledge-bases/{kb_id}"
            for method, path in [
                ("POST", base.replace(kb_id, "kb_" + "0" * 32) + ":search"),
                ("GET", base + "/documents"),
                (
                    "GET",
                    base
                    + "/artifacts/unknown?document_id=x&document_version_id=y",
                ),
                ("GET", "/api/v1/jobs/job_" + "0" * 32),
                ("GET", "/api/v1/retrieval-profiles/unknown:preview"),
            ]:
                assert (
                    client.request(
                        method,
                        path,
                        headers=headers,
                        json={"query": "公开合成"},
                    ).status_code
                    == 403
                )
            assert client.get(base).status_code == 401
            assert (
                client.get(
                    base, headers={"Authorization": "Bearer invalid"}
                ).status_code
                == 403
            )
            harness.runtime.auth.revoke_access_token(token.token_id)
            assert (
                client.post(
                    base + ":search",
                    headers=headers,
                    json={"query": "公开合成"},
                ).status_code
                == 403
            )
        payload = {
            "provider_type": "jina",
            "source": "database_encrypted",
            "secret_value": "synthetic-key",
        }
        assert (
            harness.client.post(
                "/api/v1/provider-credentials", json=payload
            ).status_code
            == 403
        )
        assert (
            harness.client.post(
                "/api/v1/provider-credentials",
                json=payload,
                headers=harness.write_headers,
            ).status_code
            == 201
        )
    finally:
        harness.close()


def test_expired_tokens_and_real_job_scope(tmp_path: Path):
    harness = build_product_harness(tmp_path)
    try:
        project_id, kb_id = create_project_and_knowledge_base(harness)
        other = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases",
            headers={**harness.write_headers, "Idempotency-Key": "kb-other"},
            json={"name": "其他知识库", "description": "合成范围测试"},
        )
        assert other.status_code == 201
        other_id = other.json()["knowledge_base_id"]
        job = harness.runtime.sdk.create_document(
            project_id,
            other_id,
            display_name="公开合成.docx",
            content=build_package(
                "<w:p><w:r><w:t>公开合成权限测试</w:t></w:r></w:p>"
            ),
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            idempotency_key="scope-job",
        )
        token = harness.runtime.auth.create_access_token(
            name="范围读取",
            scopes=("knowledge:read", "query:read"),
            project_id=project_id,
            knowledge_base_id=kb_id,
        )
        with TestClient(create_product_app(harness.runtime)) as client:
            headers = {"Authorization": f"Bearer {token.token}"}
            assert (
                client.get(
                    f"/api/v1/jobs/{job.job_id}", headers=headers
                ).status_code
                == 403
            )
            other_base = (
                f"/api/v1/projects/{project_id}/knowledge-bases/{other_id}"
            )
            assert (
                client.get(
                    other_base + "/documents", headers=headers
                ).status_code
                == 403
            )
            assert (
                client.post(
                    other_base + ":search",
                    json={"query": "公开"},
                    headers=headers,
                ).status_code
                == 403
            )
            own_base = f"/api/v1/projects/{project_id}/knowledge-bases/{kb_id}"
            assert (
                client.get(own_base + "/documents", headers=headers).status_code
                == 200
            )
            with harness.runtime.connections.transaction(
                write=True
            ) as database:
                database.execute(
                    "UPDATE api_access_tokens SET "
                    "expires_at='2000-01-01T00:00:00+00:00' WHERE token_id=?",
                    (token.token_id,),
                )
            assert (
                client.get(own_base + "/documents", headers=headers).status_code
                == 403
            )
            assert (
                client.post(
                    own_base + ":search",
                    json={"query": "公开"},
                    headers=headers,
                ).status_code
                == 403
            )
    finally:
        harness.close()
