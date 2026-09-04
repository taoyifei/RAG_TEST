"""按 Connection 与 Credential Version 缓存的 Provider Runtime。"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from rag_app.core.errors import ConfigurationError
from rag_app.core.identifiers import canonical_sha256
from rag_app.product.catalog import CATALOG_VERSION, validate_model
from rag_app.product.control_store import ProductControlStore
from rag_app.product.credential_store import CredentialStore
from rag_app.product.models import ProviderConnection, ProviderValidationRun

TransportFactory = Callable[[ProviderConnection], httpx.BaseTransport]
_SYNTHETIC_TEXT = "公开合成文本：青岛啤酒知识库连接验证。"
_HTTP_BAD_REQUEST = 400
_HTTP_SERVER_ERROR = 500


class ProviderRuntimeRegistry:
    """复用 httpx Client，并在 Credential Rotation 后关闭旧实例。"""

    def __init__(
        self,
        credentials: CredentialStore,
        control: ProductControlStore,
        *,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        """保存安全解析器、控制面和可注入 Transport。

        Args:
            credentials: 只在调用边界解密的 Credential Store。
            control: Provider Connection 与验证记录 Store。
            transport_factory: 测试用 MockTransport 工厂。

        Returns:
            无返回值。

        """
        self._credentials = credentials
        self._control = control
        self._transport_factory = transport_factory
        self._clients: dict[tuple[str, int], httpx.Client] = {}

    @property
    def client_count(self) -> int:
        """返回当前缓存的 Provider Client 数量。

        Args:
            无参数；读取当前 Registry。

        Returns:
            尚未关闭的缓存 Client 数量。

        """
        return len(self._clients)

    def validate(
        self,
        connection_id: str,
        *,
        operation: str,
        model: str,
        expected_dimension: int | None = None,
    ) -> ProviderValidationRun:
        """用合成公开文本执行并持久化一次连接验证。

        Args:
            connection_id: Provider Connection ID。
            operation: embedding.document、embedding.query 或 reranking。
            model: 内置目录模型。
            expected_dimension: Embedding 预期维度。

        Returns:
            不含原文、Secret 和 Provider Body 的验证记录。

        """
        connection = self._control.get_connection(connection_id)
        validate_model(connection.provider_type, model, operation)
        started = datetime.now(UTC)
        monotonic_start = time.monotonic()
        status = "succeeded"
        category = "mock_200" if self._transport_factory else "live_200"
        safe_error: str | None = None
        dimension: int | None = None
        observed_tokens: int | None = None
        credential_key_version = self._credentials.get(
            connection.credential_id
        ).key_version
        try:
            client, credential_key_version = self._client(connection)
            response = client.post(
                _path(connection.provider_type, operation),
                json=_payload(connection, operation, model),
            )
            category = _http_category(response.status_code, category)
            if response.status_code >= _HTTP_BAD_REQUEST:
                raise _ValidationError(_http_error_code(response.status_code))
            payload = response.json()
            dimension, observed_tokens = _validate_payload(
                connection.provider_type,
                operation,
                payload,
                expected_dimension=expected_dimension,
            )
        except httpx.TimeoutException:
            status, category, safe_error = (
                "failed",
                "timeout",
                "PROVIDER_TIMEOUT",
            )
        except httpx.RequestError:
            status, category, safe_error = (
                "failed",
                "network_error",
                "PROVIDER_NETWORK_ERROR",
            )
        except ConfigurationError:
            status, category, safe_error = (
                "failed",
                "credential_unavailable",
                "CREDENTIAL_UNAVAILABLE",
            )
        except json.JSONDecodeError:
            status, category, safe_error = "failed", "bad_json", "INVALID_JSON"
        except _ValidationError as error:
            status, safe_error = "failed", error.code
        except (KeyError, TypeError, ValueError):
            status, category, safe_error = (
                "failed",
                "invalid_contract",
                "INVALID_RESPONSE_CONTRACT",
            )
        finished = datetime.now(UTC)
        validation = ProviderValidationRun(
            validation_id=_identifier("val"),
            connection_id=connection_id,
            catalog_version=CATALOG_VERSION,
            operation=operation,
            provider_model=model,
            credential_key_version=credential_key_version,
            request_policy_identity=canonical_sha256(
                {
                    "endpoint_profile": connection.endpoint_profile,
                    "model": model,
                    "operation": operation,
                    "provider_type": connection.provider_type,
                }
            ),
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            status=status,
            http_category=category,
            dimension=dimension,
            estimated_tokens=16,
            observed_tokens=observed_tokens,
            latency_ms=max(0, int((time.monotonic() - monotonic_start) * 1000)),
            safe_error_code=safe_error,
            synthetic_payload_hash=canonical_sha256(_SYNTHETIC_TEXT),
        )
        return self._control.record_validation(validation)

    def invalidate_credential(self, credential_id: str) -> None:
        """关闭引用已轮换 Credential 的缓存 Client。

        Args:
            credential_id: 已轮换 Credential ID。

        Returns:
            无返回值。

        """
        connection_ids = {
            item.connection_id
            for item in self._control.list_connections()
            if item.credential_id == credential_id
        }
        for key in tuple(self._clients):
            if key[0] in connection_ids:
                self._clients.pop(key).close()

    def close(self) -> None:
        """关闭所有 Provider Client。

        Args:
            无参数；清空当前 Registry。

        Returns:
            无返回值。

        """
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def _client(
        self, connection: ProviderConnection
    ) -> tuple[httpx.Client, int]:
        secret, key_version = self._credentials.resolve(
            connection.credential_id
        )
        cache_key = (connection.connection_id, key_version)
        existing = self._clients.get(cache_key)
        if existing is not None:
            return existing, key_version
        self.invalidate_credential(connection.credential_id)
        headers = {"Authorization": f"Bearer {secret}"}
        if connection.workspace_id:
            headers["X-DashScope-WorkSpace"] = connection.workspace_id
        transport = (
            None
            if self._transport_factory is None
            else self._transport_factory(connection)
        )
        client = httpx.Client(
            base_url=_base_url(connection.provider_type),
            headers=headers,
            timeout=httpx.Timeout(10.0),
            transport=transport,
        )
        self._clients[cache_key] = client
        return client, key_version


class _ValidationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_offline_mock_transport(
    connection: ProviderConnection,
) -> httpx.MockTransport:
    """构造不会访问网络的成功 MockTransport。

    Args:
        connection: 用于选择固定响应形状的连接。

    Returns:
        仅处理内存请求的 httpx MockTransport。

    """

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if "rerank" in request.url.path:
            return httpx.Response(
                200,
                json={"results": [{"index": 0, "relevance_score": 0.9}]},
            )
        dimension = int(body.get("dimensions", 8))
        vector = [0.125] * dimension
        if connection.provider_type == "jina":
            payload: dict[str, Any] = {
                "data": [{"embedding": vector}],
                "usage": {"total_tokens": 8},
            }
        else:
            payload = {
                "output": {"embeddings": [{"embedding": vector}]},
                "usage": {"total_tokens": 8},
            }
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(_handler)


def _payload(
    connection: ProviderConnection,
    operation: str,
    model: str,
) -> dict[str, object]:
    if operation == "reranking":
        return {
            "documents": ["公开合成候选文本。"],
            "model": model,
            "query": _SYNTHETIC_TEXT,
            "top_n": 1,
        }
    task = (
        "retrieval.passage"
        if operation.endswith("document")
        else "retrieval.query"
    )
    payload: dict[str, object] = {
        "dimensions": 1024,
        "input": [_SYNTHETIC_TEXT],
        "model": model,
        "task": task,
    }
    if connection.region:
        payload["region"] = connection.region
    return payload


def _validate_payload(
    provider_type: str,
    operation: str,
    payload: object,
    *,
    expected_dimension: int | None,
) -> tuple[int | None, int | None]:
    if not isinstance(payload, dict):
        raise TypeError("响应必须为 object。")
    if operation == "reranking":
        results = payload["results"]
        if not isinstance(results, list) or not results:
            raise _ValidationError("RERANK_CANDIDATE_MISSING")
        if int(results[0]["index"]) != 0:
            raise _ValidationError("RERANK_CANDIDATE_MISSING")
        return None, _usage_tokens(payload)
    if provider_type == "jina":
        vectors = payload["data"]
    else:
        vectors = payload["output"]["embeddings"]
    if not isinstance(vectors, list) or not vectors:
        raise TypeError("Embedding 候选缺失。")
    vector = vectors[0]["embedding"]
    if not isinstance(vector, list) or not vector:
        raise TypeError("Embedding 向量缺失。")
    dimension = len(vector)
    if expected_dimension is not None and dimension != expected_dimension:
        raise _ValidationError("EMBEDDING_DIMENSION_MISMATCH")
    return dimension, _usage_tokens(payload)


def _usage_tokens(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict) or usage.get("total_tokens") is None:
        return None
    return int(usage["total_tokens"])


def _base_url(provider_type: str) -> str:
    if provider_type == "jina":
        return "https://api.jina.ai"
    return "https://dashscope.aliyuncs.com"


def _path(provider_type: str, operation: str) -> str:
    if provider_type == "jina":
        return "/v1/rerank" if operation == "reranking" else "/v1/embeddings"
    return "/api/v1/services/embeddings/text-embedding/text-embedding"


def _http_category(status_code: int, success: str) -> str:
    if status_code < _HTTP_BAD_REQUEST:
        return success
    if status_code in {401, 403, 429}:
        return f"http_{status_code}"
    if status_code >= _HTTP_SERVER_ERROR:
        return "http_5xx"
    return "http_4xx"


def _http_error_code(status_code: int) -> str:
    return {
        400: "REGION_OR_WORKSPACE_INVALID",
        401: "PROVIDER_AUTHENTICATION_FAILED",
        403: "PROVIDER_AUTHORIZATION_DENIED",
        429: "PROVIDER_RATE_LIMITED",
    }.get(status_code, "PROVIDER_UPSTREAM_ERROR")


def _identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


__all__ = [
    "ProviderRuntimeRegistry",
    "TransportFactory",
    "build_offline_mock_transport",
]
