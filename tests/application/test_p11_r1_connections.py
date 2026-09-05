"""P11-R1 端点、配置并发与凭据保留的隔离合同测试。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx
import pytest

from rag_app.adapters.providers.aliyun_endpoint import (
    NATIVE_EMBEDDING_PATH,
    AliyunEndpointConfig,
    resolve_endpoint,
)
from rag_app.core.errors import Conflict
from rag_app.core.models import EmbeddingRequest, EmbeddingRequestRole
from rag_app.product.models import ProviderConnection, ProviderConnectionDraft
from tests.product_support import build_product_harness


@pytest.mark.parametrize(
    "workspace", ["ws-demo000000001", "llm-demo000000001", " A_B-1 "]
)
@pytest.mark.parametrize("mode", ["workspace_host", "beijing_dashscope"])
def test_opaque_workspace_and_explicit_endpoint(workspace: str, mode: str):
    config = AliyunEndpointConfig.model_validate(
        {
            "workspace_id": workspace,
            "endpoint_mode": mode,
            "api_host": "https://api-demo.cn-beijing.maas.aliyuncs.com:443/"
            if mode == "workspace_host"
            else None,
        }
    )
    expected = (
        "https://api-demo.cn-beijing.maas.aliyuncs.com"
        if mode == "workspace_host"
        else "https://dashscope.aliyuncs.com"
    )
    assert (
        resolve_endpoint(config) + NATIVE_EMBEDDING_PATH
        == expected
        + "/api/v1/services/embeddings/text-embedding/text-embedding"
    )
    assert config.workspace_id == workspace.strip(" ")


@pytest.mark.parametrize(
    "workspace",
    [
        "",
        "a b",
        "a\tb",
        "a\nb",
        "a\x00b",
        "a/b",
        "a:b",
        "a.b",
        "a%2fb",
        "ａ",
        "a" * 201,
        "\tws-demo",
    ],
)
def test_workspace_injection_rejected(workspace: str):
    with pytest.raises(ValueError):
        AliyunEndpointConfig(workspace_id=workspace)


@pytest.mark.parametrize(
    "host",
    [
        "api-demo.cn-beijing.maas.aliyuncs.com",
        "https://api-demo.cn-beijing.maas.aliyuncs.com",
        "https://api-demo.cn-beijing.maas.aliyuncs.com:443/",
    ],
)
def test_equivalent_trusted_host_inputs(host: str):
    assert (
        resolve_endpoint(
            AliyunEndpointConfig(
                workspace_id="ws-demo",
                api_host=host,
            )
        )
        == "https://api-demo.cn-beijing.maas.aliyuncs.com"
    )


@pytest.mark.parametrize(
    "host",
    [
        "http://demo.cn-beijing.maas.aliyuncs.com",
        "https://user@demo.cn-beijing.maas.aliyuncs.com",
        "https://demo.cn-beijing.maas.aliyuncs.com:444",
        "https://127.0.0.1",
        "https://demo.cn-beijing.maas.aliyuncs.com/a",
        "https://demo.cn-beijing.maas.aliyuncs.com?x=1",
        "https://demo.cn-beijing.maas.aliyuncs.com#x",
        "https://demo.cn-beijing.maas.aliyuncs.com.evil.invalid",
        "https://a.b.cn-beijing.maas.aliyuncs.com",
        "https://a_b.cn-beijing.maas.aliyuncs.com",
        "https://-a.cn-beijing.maas.aliyuncs.com",
        "https://a-.cn-beijing.maas.aliyuncs.com",
        "https://dashscope.aliyuncs.com",
        "https://demo.cn-beijing.maas.aliyuncs.com%2f",
        "https://demo.cn-beijing.maas.aliyuncs.com?",
        "https://demo.cn-beijing.maas.aliyuncs.com\n",
        "demo.cn-shanghai.maas.aliyuncs.com",
        "https://10.0.0.1",
        "https://[::1]",
        "https://demo.cn-beijing.maas.aliyuncs.com:abc",
        "https://ｄemo.cn-beijing.maas.aliyuncs.com",
        "https://demo.cn-beijing.maas.aliyuncs.com\\evil",
        "https://demo.cn-beijing.maas.aliyuncs.com\x00",
    ],
)
def test_untrusted_origin_rejected(host: str):
    with pytest.raises(ValueError):
        resolve_endpoint(
            AliyunEndpointConfig(workspace_id="ws-demo", api_host=host)
        )


def test_patch_preserves_secret_and_shared_probe_adapter_contract(
    tmp_path: Path,
):
    requests: list[httpx.Request] = []

    def transport(connection: ProviderConnection) -> httpx.MockTransport:
        del connection

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "status_code": 200,
                    "usage": {"total_tokens": 8},
                    "code": "",
                    "request_id": "synthetic-request-1",
                    "output": {
                        "embeddings": [
                            {"text_index": 0, "embedding": [0.125] * 1024}
                        ]
                    },
                },
            )

        return httpx.MockTransport(handler)

    harness = build_product_harness(tmp_path, transport_factory=transport)
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "aliyun-model-studio", "synthetic-preserved-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="百炼",
                provider_type="aliyun-model-studio",
                credential_id=credential.credential_id,
                workspace_id="ws-demo000000001",
                region="cn-beijing",
                api_host="https://old.cn-beijing.maas.aliyuncs.com",
            )
        )
        with harness.runtime.connections.transaction() as database:
            before = tuple(
                database.execute(
                    "SELECT * FROM provider_credentials"
                ).fetchone()
            )
        probe = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.document",
            model="qwen3.7-text-embedding",
        )
        assert probe.status == "succeeded", probe.model_dump()
        old_client = next(iter(harness.runtime.providers._clients.values()))
        response = harness.client.patch(
            f"/api/v1/provider-connections/{connection.connection_id}",
            headers=harness.write_headers,
            json={
                "expected_version": 1,
                "endpoint_mode": "beijing_dashscope",
                "api_host": None,
                "workspace_id": "llm-demo000000001",
            },
        )
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["connection_id"] == connection.connection_id
        assert saved["credential_id"] == credential.credential_id
        assert saved["configuration_version"] == 2
        assert old_client.is_closed
        assert len(requests) == 1
        assert (
            harness.runtime.control.latest_status(
                connection.connection_id, "embedding.document"
            )
            == "not_verified"
        )
        with harness.runtime.connections.transaction() as database:
            assert (
                tuple(
                    database.execute(
                        "SELECT * FROM provider_credentials"
                    ).fetchone()
                )
                == before
            )
        assert harness.runtime.credentials.resolve(
            credential.credential_id
        ) == ("synthetic-preserved-secret", 1)
        assert (
            harness.client.patch(
                f"/api/v1/provider-connections/{connection.connection_id}",
                headers=harness.write_headers,
                json={"expected_version": 1, "display_name": "丢失更新"},
            ).status_code
            == 409
        )
        assert (
            harness.client.patch(
                f"/api/v1/provider-connections/{connection.connection_id}",
                headers=harness.write_headers,
                json={"expected_version": 2, "secret_value": ""},
            ).status_code
            == 422
        )
        probe = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.document",
            model="qwen3.7-text-embedding",
        )
        adapter = harness.runtime.providers.embedding_adapter(
            connection.connection_id,
            slot_id="standby",
            model="qwen3.7-text-embedding",
            dimension=1024,
            document_policy_identity="document",
            query_policy_identity="query",
        )
        try:
            adapter.embed(
                EmbeddingRequest(
                    slot_id="standby",
                    role=EmbeddingRequestRole.DOCUMENT,
                    texts=("验收示例：审批完成后归档。",),
                )
            )
        finally:
            adapter.close()
        assert (
            str(requests[-1].url)
            == str(requests[-2].url)
            == "https://dashscope.aliyuncs.com" + NATIVE_EMBEDDING_PATH
        )
        assert requests[-1].content == requests[-2].content
        assert probe.configuration_version == 2
        assert probe.request_dispatched is True
        assert probe.provider_request_id == "synthetic-request-1"
        assert "synthetic-preserved-secret" not in probe.model_dump_json()
        assert (
            len(
                harness.runtime.control.list_validations(
                    connection.connection_id
                )
            )
            == 2
        )
    finally:
        harness.close()


def test_concurrent_updates_and_inflight_probe(tmp_path: Path):
    entered, release = Event(), Event()

    def transport(connection: ProviderConnection) -> httpx.MockTransport:
        del connection

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            entered.set()
            assert release.wait(10)
            return httpx.Response(
                403,
                json={
                    "code": "Model.AccessDenied",
                    "request_id": "synthetic-request-2",
                    "message": "secret-do-not-record",
                },
            )

        return httpx.MockTransport(handler)

    harness = build_product_harness(tmp_path, transport_factory=transport)
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="Jina",
                provider_type="jina",
                credential_id=credential.credential_id,
            )
        )
        with ThreadPoolExecutor(max_workers=3) as pool:
            probe = pool.submit(
                harness.runtime.providers.validate,
                connection.connection_id,
                operation="embedding.document",
                model="jina-embeddings-v5-text-small",
            )
            assert entered.wait(10)
            client = next(iter(harness.runtime.providers._clients.values()))

            def update(name: str) -> ProviderConnection | None:
                try:
                    return harness.runtime.control.update_connection(
                        connection.connection_id,
                        expected_version=1,
                        changes={"display_name": name},
                    )
                except Conflict:
                    return None

            futures = [
                pool.submit(update, name) for name in ("first", "second")
            ]
            assert sum(item.result() is not None for item in futures) == 1
            invalidation = pool.submit(
                harness.runtime.providers.invalidate_connection,
                connection.connection_id,
            )
            assert not client.is_closed
            assert not invalidation.done()
            release.set()
            result = probe.result()
            invalidation.result()
        assert client.is_closed
        assert result.http_status == 403
        assert result.request_dispatched is True
        assert result.provider_code == "Model.AccessDenied"
        assert "secret-do-not-record" not in result.model_dump_json()
        assert (
            harness.runtime.control.latest_status(
                connection.connection_id, "embedding.document"
            )
            == "not_verified"
        )
        assert (
            harness.runtime.control.get_connection(
                connection.connection_id
            ).last_validation_id
            is None
        )
    finally:
        release.set()
        harness.close()


def test_combined_create_preserves_shared_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    harness = build_product_harness(tmp_path)
    try:
        invalid = harness.client.post(
            "/api/v1/provider-connections",
            headers=harness.write_headers,
            json={
                "display_name": "百炼",
                "provider_type": "aliyun-model-studio",
                "workspace_id": "ws-demo",
                "region": "cn-beijing",
                "credential": {
                    "provider_type": "aliyun-model-studio",
                    "source": "database_encrypted",
                    "secret_value": "synthetic-secret",
                },
            },
        )
        assert invalid.status_code == 400
        assert harness.runtime.credentials.list() == ()
        credential = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-shared"
        )
        saved = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="shared",
                provider_type="jina",
                credential_id=credential.credential_id,
            )
        )
        harness.runtime.credentials.remove_new_orphan(credential.credential_id)
        assert (
            harness.runtime.credentials.get(credential.credential_id)
            == credential
        )

        def fail(draft: ProviderConnectionDraft) -> None:
            del draft
            raise ValueError("synthetic insert failure")

        monkeypatch.setattr(harness.runtime.control, "create_connection", fail)
        response = harness.client.post(
            "/api/v1/provider-connections",
            headers=harness.write_headers,
            json={
                "display_name": "new",
                "provider_type": "jina",
                "credential": {
                    "provider_type": "jina",
                    "source": "database_encrypted",
                    "secret_value": "synthetic-new",
                },
            },
        )
        assert response.status_code == 400
        assert harness.runtime.credentials.list() == (credential,)
        assert (
            harness.runtime.control.get_connection(
                saved.connection_id
            ).credential_id
            == credential.credential_id
        )
    finally:
        harness.close()
