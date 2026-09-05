"""Provider Client 缓存、轮换失效与安全错误归类回归。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.identifiers import canonical_sha256
from rag_app.product.models import ProviderConnectionDraft
from tests.product_support import (
    build_product_harness,
    create_provider_connections,
)

_OVERSIZED_RESPONSE_BYTES = 4 * 1024 * 1024 + 1
_TOO_LARGE_USAGE = 2**63


def test_provider_client_is_cached_and_rotation_closes_old_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        credential_id, _, connection_id, _ = create_provider_connections(
            harness
        )
        for operation in ("embedding.document", "embedding.query"):
            result = harness.runtime.providers.validate(
                connection_id,
                operation=operation,
                model="jina-embeddings-v5-text-small",
                expected_dimension=1024,
            )
            assert result.status == "succeeded"
        assert harness.runtime.providers.client_count == 1

        harness.runtime.credentials.rotate(
            credential_id, "rotated-synthetic-jina-value"
        )
        harness.runtime.providers.invalidate_credential(credential_id)
        assert harness.runtime.providers.client_count == 0
        harness.runtime.providers.validate(
            connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )
        assert harness.runtime.providers.client_count == 1
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("operation", "text_type", "has_instruct", "status_code"),
    (
        ("embedding.document", "document", False, 200),
        ("embedding.query", "query", True, "200"),
    ),
)
def test_aliyun_validation_uses_native_workspace_contract(
    tmp_path: Path,
    operation: str,
    text_type: str,
    has_instruct: bool,
    status_code: int | str,
) -> None:
    requests: list[httpx.Request] = []

    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "code": "",
                    "status_code": status_code,
                    "output": {
                        "embeddings": [
                            {
                                "embedding": [0.125] * 1024,
                                "text_index": 0,
                            }
                        ]
                    },
                    "usage": {"total_tokens": 17},
                },
            )

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "aliyun-model-studio",
            "synthetic-aliyun-contract-secret",
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="百炼合同校验",
                provider_type="aliyun-model-studio",
                credential_id=credential.credential_id,
                workspace_id="llm-workspace1",
                api_host="https://llm-workspace1.cn-beijing.maas.aliyuncs.com",
                region="cn-beijing",
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation=operation,
            model="qwen3.7-text-embedding",
            expected_dimension=1024,
        )

        assert result.status == "succeeded"
        assert result.dimension == 1024
        assert result.observed_tokens == 17
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == (
            "https://llm-workspace1.cn-beijing.maas.aliyuncs.com/"
            "api/v1/services/embeddings/text-embedding/text-embedding"
        )
        assert "X-DashScope-WorkSpace" not in request.headers
        body = json.loads(request.content)
        assert set(body) == {"input", "model", "parameters"}
        assert result.synthetic_payload_hash == canonical_sha256(body)
        assert body["input"] == {
            "texts": ["验收示例：审批完成后归档。"]
        }
        assert body["model"] == "qwen3.7-text-embedding"
        parameters = body["parameters"]
        assert isinstance(parameters, dict)
        assert parameters["dimension"] == 1024
        assert parameters["output_type"] == "dense"
        assert parameters["text_type"] == text_type
        assert ("instruct" in parameters) is has_instruct
        if has_instruct:
            assert parameters["instruct"] == (
                "Given a user query, retrieve the most relevant passages "
                "from enterprise DOCX knowledge bases."
            )
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("operation", "task"),
    (
        ("embedding.document", "retrieval.passage"),
        ("embedding.query", "retrieval.query"),
    ),
)
def test_jina_validation_uses_strict_embedding_contract(
    tmp_path: Path,
    operation: str,
    task: str,
) -> None:
    requests: list[httpx.Request] = []

    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "embedding": [0.125] * 1024,
                            "index": 0,
                        }
                    ],
                    "model": "jina-embeddings-v5-text-small",
                    "usage": {"total_tokens": 9},
                },
            )

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-jina-contract-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="Jina 合同校验",
                provider_type="jina",
                credential_id=credential.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation=operation,
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.status == "succeeded"
        assert result.observed_tokens == 9
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == "https://api.jina.ai/v1/embeddings"
        body = json.loads(request.content)
        assert result.synthetic_payload_hash == canonical_sha256(body)
        assert body == {
            "dimensions": 1024,
            "embedding_type": "float",
            "input": ["验收示例：审批完成后归档。"],
            "model": "jina-embeddings-v5-text-small",
            "normalized": True,
            "task": task,
            "truncate": False,
        }
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (httpx.Response(302), "PROVIDER_UPSTREAM_ERROR"),
        (httpx.Response(400), "PROVIDER_REQUEST_INVALID"),
        (httpx.Response(422), "PROVIDER_REQUEST_INVALID"),
        (httpx.Response(401), "PROVIDER_AUTHENTICATION_FAILED"),
        (httpx.Response(403), "PROVIDER_AUTHORIZATION_DENIED"),
        (
            httpx.Response(429, headers={"Retry-After": "1"}),
            "PROVIDER_RATE_LIMITED",
        ),
        (httpx.Response(503), "PROVIDER_UPSTREAM_ERROR"),
        (
            httpx.Response(
                200,
                content=b"not-json",
                headers={"content-type": "application/json"},
            ),
            "INVALID_JSON",
        ),
        (
            httpx.Response(
                200,
                json={
                    "data": [{"embedding": [0.1], "index": 0}],
                    "model": "jina-embeddings-v5-text-small",
                    "usage": {"total_tokens": 1},
                },
            ),
            "EMBEDDING_DIMENSION_MISMATCH",
        ),
        (
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "embedding": [0.1] * 1024,
                            "index": 0,
                        }
                    ],
                    "usage": {"total_tokens": 1},
                },
            ),
            "INVALID_RESPONSE_CONTRACT",
        ),
        (
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "embedding": [0.1] * 1024,
                            "index": 0,
                        }
                    ],
                    "model": "jina-embeddings-v4",
                    "usage": {"total_tokens": 1},
                },
            ),
            "INVALID_RESPONSE_CONTRACT",
        ),
    ],
)
def test_provider_validation_persists_safe_failures(
    tmp_path: Path,
    response: httpx.Response,
    error_code: str,
) -> None:
    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return response

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "safe-synthetic-value"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="失败分类",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.status == "failed"
        assert result.safe_error_code == error_code
        assert "safe-synthetic-value" not in json.dumps(
            result.model_dump(mode="json")
        )
    finally:
        harness.close()


def test_provider_validation_rejects_redirect_even_with_valid_json(
    tmp_path: Path,
) -> None:
    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda _request: httpx.Response(
                302,
                json={
                    "data": [{"embedding": [0.125] * 1024, "index": 0}],
                    "model": "jina-embeddings-v5-text-small",
                    "usage": {"total_tokens": 1},
                },
            )
        )

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-redirect-value"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="重定向分类",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.status == "failed"
        assert result.http_category == "http_3xx"
        assert result.safe_error_code == "PROVIDER_UPSTREAM_ERROR"
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("response", "error_code"),
    (
        (
            httpx.Response(
                200,
                content=b"{}",
                headers={"content-type": "text/plain"},
            ),
            "INVALID_CONTENT_TYPE",
        ),
        (
            httpx.Response(200, content=b"x" * _OVERSIZED_RESPONSE_BYTES),
            "RESPONSE_TOO_LARGE",
        ),
    ),
)
def test_provider_validation_bounds_response_envelope(
    tmp_path: Path,
    response: httpx.Response,
    error_code: str,
) -> None:
    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _request: response)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-envelope-value"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="响应外壳校验",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.status == "failed"
        assert result.safe_error_code == error_code
    finally:
        harness.close()


def test_provider_validation_classifies_timeout_without_leaking_secret(
    tmp_path: Path,
) -> None:
    credential_value = "synthetic-timeout-value"

    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", credential_value
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="超时分类",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.http_category == "timeout"
        assert result.safe_error_code == "PROVIDER_TIMEOUT"
        assert credential_value not in json.dumps(
            result.model_dump(mode="json")
        )
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("error_type", "http_category", "safe_error_code"),
    (
        (httpx.ConnectError, "connect_error", "PROVIDER_CONNECT_ERROR"),
        (httpx.ReadError, "read_error", "PROVIDER_READ_ERROR"),
        (httpx.WriteError, "write_error", "PROVIDER_WRITE_ERROR"),
        (
            httpx.RemoteProtocolError,
            "remote_protocol_error",
            "PROVIDER_REMOTE_PROTOCOL_ERROR",
        ),
        (httpx.RequestError, "network_error", "PROVIDER_NETWORK_ERROR"),
    ),
)
def test_provider_validation_classifies_safe_request_errors(
    tmp_path: Path,
    error_type: type[httpx.RequestError],
    http_category: str,
    safe_error_code: str,
) -> None:
    credential_value = "synthetic-request-error-secret"

    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            raise error_type(credential_value, request=request)

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", credential_value
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="网络错误安全分类",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.http_category == http_category
        assert result.safe_error_code == safe_error_code
        assert credential_value not in json.dumps(
            result.model_dump(mode="json")
        )
    finally:
        harness.close()


def test_reranker_validation_requires_the_synthetic_candidate(
    tmp_path: Path,
) -> None:
    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "model": "jina-reranker-v3.5",
                    "results": [],
                    "usage": {"total_tokens": 1},
                },
            )
        )

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-reranker-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="候选校验",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="reranking",
            model="jina-reranker-v3.5",
        )

        assert result.http_category == "mock_200"
        assert result.safe_error_code == "RERANK_CANDIDATE_MISSING"
    finally:
        harness.close()


def test_reranker_validation_sends_and_checks_all_synthetic_candidates(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "model": "jina-reranker-v3.5",
                    "results": [
                        {"index": 1, "relevance_score": 0.25},
                        {"index": 0, "relevance_score": 0.75},
                    ],
                    "usage": {"total_tokens": 13},
                },
            )

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-reranker-contract-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="完整候选校验",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="reranking",
            model="jina-reranker-v3.5",
        )

        assert result.status == "succeeded"
        assert result.observed_tokens == 13
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == "https://api.jina.ai/v1/rerank"
        body = json.loads(request.content)
        assert body["documents"] == [
            "公开合成候选：青岛啤酒创建于 1903 年。",
            "公开合成候选：这段测试文本不包含私有信息。",
        ]
        assert body["model"] == "jina-reranker-v3.5"
        assert body["return_documents"] is False
        assert body["top_n"] == len(body["documents"])
        assert result.estimated_tokens > len(body["query"])
    finally:
        harness.close()


@pytest.mark.parametrize(
    "usage_value",
    (None, True, 1.5, -1, 0, _TOO_LARGE_USAGE),
)
def test_jina_embedding_validation_requires_strict_usage(
    tmp_path: Path,
    usage_value: object,
) -> None:
    payload: dict[str, object] = {
        "data": [{"embedding": [0.125] * 1024, "index": 0}],
        "model": "jina-embeddings-v5-text-small",
    }
    if usage_value is not None:
        payload["usage"] = {"total_tokens": usage_value}

    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)
        )

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-usage-value"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="严格 Usage 校验",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.status == "failed"
        assert result.safe_error_code == "INVALID_RESPONSE_CONTRACT"
        assert result.observed_tokens is None
    finally:
        harness.close()


def test_jina_reranker_validation_requires_usage(
    tmp_path: Path,
) -> None:
    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "model": "jina-reranker-v3.5",
                    "results": [
                        {"index": 0, "relevance_score": 0.75},
                        {"index": 1, "relevance_score": 0.25},
                    ],
                },
            )
        )

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-reranker-usage-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="Reranker Usage 校验",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="reranking",
            model="jina-reranker-v3.5",
        )

        assert result.status == "failed"
        assert result.safe_error_code == "INVALID_RESPONSE_CONTRACT"
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("response", "error_code"),
    (
        (
            {
                "status_code": 200,
                "output": {
                    "embeddings": [
                        {"embedding": [0.125] * 1024, "text_index": 0}
                    ]
                },
                "usage": {"total_tokens": 1},
            },
            "INVALID_RESPONSE_CONTRACT",
        ),
        (
            {
                "code": "",
                "status_code": 200,
                "output": {
                    "embeddings": [
                        {"embedding": [0.125] * 1024, "text_index": 0}
                    ]
                },
            },
            "INVALID_RESPONSE_CONTRACT",
        ),
        (
            {
                "code": "",
                "status_code": 200,
                "output": {
                    "embeddings": [{"embedding": [0.125], "text_index": 0}]
                },
                "usage": {"total_tokens": 1},
            },
            "EMBEDDING_DIMENSION_MISMATCH",
        ),
    ),
)
def test_aliyun_validation_fails_closed_on_response_contract(
    tmp_path: Path,
    response: dict[str, object],
    error_code: str,
) -> None:
    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response)
        )

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "aliyun-model-studio", "synthetic-aliyun-response-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="百炼响应校验",
                provider_type="aliyun-model-studio",
                credential_id=created.credential_id,
                workspace_id="llm-workspace1",
                api_host="https://llm-workspace1.cn-beijing.maas.aliyuncs.com",
                region="cn-beijing",
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="qwen3.7-text-embedding",
            expected_dimension=1024,
        )

        assert result.status == "failed"
        assert result.safe_error_code == error_code
    finally:
        harness.close()


@pytest.mark.parametrize("workspace_id", ("bad workspace", "INVALID WORKSPACE"))
def test_aliyun_invalid_workspace_has_safe_configuration_error(
    tmp_path: Path,
    workspace_id: str,
) -> None:
    requests: list[httpx.Request] = []

    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "aliyun-model-studio", "synthetic-invalid-workspace-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="百炼配置校验",
                provider_type="aliyun-model-studio",
                credential_id=created.credential_id,
                workspace_id="ws-demo000000001",
                api_host="https://safe.cn-beijing.maas.aliyuncs.com",
                region="cn-beijing",
            )
        )
        # 模拟迁移保留的旧无效元数据；新创建接口会更早拒绝。

        factory = SqliteConnectionFactory(
            harness.runtime.settings.data_dir / "universal-rag.sqlite3"
        )
        with factory.transaction(write=True) as database:
            database.execute(
                "UPDATE provider_connections SET config_json="
                "json_set(config_json, '$.workspace_id', ?) "
                "WHERE connection_id=?",
                (workspace_id, connection.connection_id),
            )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.document",
            model="qwen3.7-text-embedding",
            expected_dimension=1024,
        )

        assert result.status == "failed"
        assert result.http_category == "invalid_configuration"
        assert result.safe_error_code == "PROVIDER_CONFIGURATION_INVALID"
        assert requests == []
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("status_code", "response_body", "safe_error_code"),
    (
        (
            400,
            {"code": "InvalidApiKey"},
            "PROVIDER_AUTHENTICATION_FAILED",
        ),
        (
            400,
            {"error": {"code": "invalid_api_key"}},
            "PROVIDER_AUTHENTICATION_FAILED",
        ),
        (
            401,
            {"code": "NOT AUTHORIZED"},
            "PROVIDER_AUTHENTICATION_FAILED",
        ),
        (
            404,
            {"code": "WorkSpaceNotFound"},
            "REGION_OR_WORKSPACE_INVALID",
        ),
        (
            403,
            {"code": "Workspace.AccessDenied"},
            "PROVIDER_AUTHORIZATION_DENIED",
        ),
        (
            403,
            {"code": "Model.AccessDenied"},
            "PROVIDER_AUTHORIZATION_DENIED",
        ),
        (
            400,
            {"code": "InvalidParameter"},
            "PROVIDER_REQUEST_INVALID",
        ),
        (
            400,
            {"code": "synthetic-sensitive-upstream-text"},
            "PROVIDER_REQUEST_INVALID",
        ),
    ),
)
def test_aliyun_error_body_uses_safe_allowlisted_classification(
    tmp_path: Path,
    status_code: int,
    response_body: dict[str, object],
    safe_error_code: str,
) -> None:
    untrusted_text = "synthetic-sensitive-upstream-text"

    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda _request: httpx.Response(
                status_code,
                json={**response_body, "message": untrusted_text},
            )
        )

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "aliyun-model-studio", "synthetic-aliyun-request-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="百炼请求分类",
                provider_type="aliyun-model-studio",
                credential_id=created.credential_id,
                workspace_id="llm-workspace1",
                api_host="https://llm-workspace1.cn-beijing.maas.aliyuncs.com",
                region="cn-beijing",
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="qwen3.7-text-embedding",
            expected_dimension=1024,
        )

        assert result.status == "failed"
        assert result.safe_error_code == safe_error_code
        persisted = harness.runtime.control.list_validations(
            connection.connection_id
        )
        safe_records = json.dumps(
            [item.model_dump(mode="json") for item in persisted]
        )
        assert untrusted_text not in safe_records
    finally:
        harness.close()
