"""按 Connection 与 Credential Version 缓存的 Provider Runtime。"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

import httpx

from rag_app.adapters.providers import (
    AliyunQwen37EmbeddingAdapter,
    AliyunQwen37EmbeddingConfig,
    JinaEmbeddingConfig,
    JinaRerankerConfig,
    JinaRerankerV35Adapter,
    JinaV5TextEmbeddingAdapter,
)
from rag_app.adapters.providers.aliyun_contract import (
    decode_embeddings,
    embedding_payload,
)
from rag_app.adapters.providers.aliyun_endpoint import (
    AliyunEndpointConfig,
    resolve_endpoint,
)
from rag_app.adapters.providers.batching import estimate_tokens
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.validation import (
    finite_score,
    ordered_vectors,
    usage_tokens,
)
from rag_app.core.errors import ConfigurationError
from rag_app.core.identifiers import canonical_sha256
from rag_app.product.catalog import CATALOG_VERSION, validate_model
from rag_app.product.control_store import ProductControlStore
from rag_app.product.credential_store import CredentialStore
from rag_app.product.models import ProviderConnection, ProviderValidationRun
from rag_app.product.resolved_profile import (
    ResolvedEmbeddingSpec,
    resolve_embedding,
)

TransportFactory = Callable[[ProviderConnection], httpx.BaseTransport]
_SYNTHETIC_TEXT = "公开合成文本：青岛啤酒知识库连接验证。"
_SYNTHETIC_RERANK_DOCUMENTS = (
    "公开合成候选：青岛啤酒创建于 1903 年。",
    "公开合成候选：这段测试文本不包含私有信息。",
)
_MAX_PROVIDER_CODE = 80
_HTTP_OK = 200
_EMBEDDING_DIMENSION = 1024
_HTTP_REDIRECT = 300
_HTTP_BAD_REQUEST = 400
_HTTP_SERVER_ERROR = 500
_MAX_VALIDATION_RESPONSE_BYTES = 4 * 1024 * 1024
_ALIYUN_SAFE_ERROR_CODES = {
    "access_denied": "PROVIDER_AUTHORIZATION_DENIED",
    "accessdenied": "PROVIDER_AUTHORIZATION_DENIED",
    "accessdenied.unpurchased": "PROVIDER_AUTHORIZATION_DENIED",
    "endpoint.accessdenied": "PROVIDER_AUTHORIZATION_DENIED",
    "invalid_api_key": "PROVIDER_AUTHENTICATION_FAILED",
    "invalidapikey": "PROVIDER_AUTHENTICATION_FAILED",
    "model.accessdenied": "PROVIDER_AUTHORIZATION_DENIED",
    "not authorized": "PROVIDER_AUTHORIZATION_DENIED",
    "notauthorized": "PROVIDER_AUTHORIZATION_DENIED",
    "workspace.accessdenied": "PROVIDER_AUTHORIZATION_DENIED",
    "workspacenotfound": "REGION_OR_WORKSPACE_INVALID",
}


@dataclass
class _ProbeDiagnostics:
    request_dispatched: bool = False
    http_status: int | None = None
    provider_code: str | None = None
    provider_request_id: str | None = None
    endpoint_host: str | None = None
    stage: str = "local_configuration"


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
        self._clients: dict[tuple[str, int, int, str], httpx.Client] = {}
        self._lock = RLock()

    @property
    def client_count(self) -> int:
        """返回当前缓存的 Provider Client 数量。

        Args:
            无参数；读取当前 Registry。

        Returns:
            尚未关闭的缓存 Client 数量。

        """
        return len(self._clients)

    @property
    def test_only_transport(self) -> bool:
        """说明当前是否只能提供 Mock 合同证据。

        Args:
            无参数；读取组合根配置。

        Returns:
            注入测试 Transport 时为 True。

        """
        return self._transport_factory is not None

    def validate(
        self,
        connection_id: str,
        *,
        operation: str,
        model: str,
        expected_dimension: int | None = None,
        request_policy: dict[str, object] | None = None,
    ) -> ProviderValidationRun:
        """持有请求租约，等待在途 Probe 完成后才允许关闭旧 Client。

        Args:
            connection_id: 当前连接。
            operation: 独立测试操作。
            model: 目录模型。
            expected_dimension: 预期向量维度。
            request_policy: 当前角色的非 Secret 实际请求策略。

        Returns:
            安全持久化验证摘要。

        """
        with self._lock:
            return self._validate(
                connection_id,
                operation=operation,
                model=model,
                expected_dimension=expected_dimension,
                request_policy=request_policy,
            )

    def invalidate_connection(self, connection_id: str) -> None:
        """在在途 Probe 结束后关闭指定连接缓存。

        Args:
            connection_id: 已修改的连接 ID。

        Returns:
            无返回值。

        """
        with self._lock:
            for key in tuple(self._clients):
                if key[0] == connection_id:
                    self._clients.pop(key).close()

    def _validate(
        self,
        connection_id: str,
        *,
        operation: str,
        model: str,
        expected_dimension: int | None = None,
        request_policy: dict[str, object] | None = None,
    ) -> ProviderValidationRun:
        """用合成公开文本执行并持久化一次连接验证。

        Args:
            connection_id: Provider Connection ID。
            operation: embedding.document、embedding.query 或 reranking。
            model: 内置目录模型。
            expected_dimension: Embedding 预期维度。
            request_policy: 需要验证的完整角色策略。

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
        spec = _validation_spec(
            connection, operation, model, expected_dimension, request_policy
        )
        request_payload = _payload(connection, operation, model, resolved=spec)
        credential_key_version = self._credentials.get(
            connection.credential_id
        ).key_version
        diagnostics = _ProbeDiagnostics()
        resolved_endpoint_identity = None
        try:
            diagnostics.endpoint_host = urlsplit(_base_url(connection)).hostname
            resolved_endpoint_identity = canonical_sha256(_base_url(connection))
            client, credential_key_version = self._client(connection)
            diagnostics.request_dispatched = True
            diagnostics.stage = "transport"
            response = client.post(
                _path(connection.provider_type, operation),
                json=request_payload,
            )
            diagnostics.http_status = response.status_code
            diagnostics.provider_code, diagnostics.provider_request_id = (
                _safe_response_details(response)
            )
            diagnostics.stage = "http"
            category = _http_category(response.status_code, category)
            if response.status_code != _HTTP_OK:
                raise _ValidationError(
                    _http_error_code(
                        connection.provider_type,
                        response,
                    )
                )
            diagnostics.stage = "response_contract"
            dimension, observed_tokens = _probe_response_contract(
                response,
                connection.provider_type,
                operation,
                model,
                expected_dimension,
            )
        except httpx.TimeoutException:
            status, category, safe_error = (
                "failed",
                "timeout",
                "PROVIDER_TIMEOUT",
            )
        except httpx.RequestError as error:
            category, safe_error = _request_error_details(error)
            status = "failed"
        except _ProviderConfigurationError:
            status, category, safe_error = (
                "failed",
                "invalid_configuration",
                "PROVIDER_CONFIGURATION_INVALID",
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
            configuration_version=connection.configuration_version,
            stage=diagnostics.stage,
            request_dispatched=diagnostics.request_dispatched,
            http_status=diagnostics.http_status,
            provider_code=diagnostics.provider_code,
            provider_request_id=diagnostics.provider_request_id,
            endpoint_mode=connection.endpoint_mode
            if connection.provider_type == "aliyun-model-studio"
            else None,
            endpoint_host=diagnostics.endpoint_host,
            catalog_version=CATALOG_VERSION,
            operation=operation,
            provider_model=model,
            credential_key_version=credential_key_version,
            request_policy_identity=(
                canonical_sha256({"model": model, "operation": operation})
                if spec is None
                else spec.policy_identity(operation)
            ),
            endpoint_identity=resolved_endpoint_identity,
            validation_mode="mock" if self._transport_factory else "live",
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            status=status,
            http_category=category,
            dimension=dimension,
            estimated_tokens=_estimated_tokens(connection, operation),
            observed_tokens=observed_tokens,
            latency_ms=max(0, int((time.monotonic() - monotonic_start) * 1000)),
            safe_error_code=safe_error,
            synthetic_payload_hash=canonical_sha256(request_payload),
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
        for connection_id in connection_ids:
            self.invalidate_connection(connection_id)

    def embedding_adapter(  # noqa: PLR0913
        self,
        connection_id: str,
        *,
        slot_id: str,
        model: str,
        dimension: int,
        document_policy_identity: str,
        query_policy_identity: str,
        resolved: ResolvedEmbeddingSpec | None = None,
    ) -> JinaV5TextEmbeddingAdapter | AliyunQwen37EmbeddingAdapter:
        """创建使用页面托管连接且调用时解密的 Embedding adapter。

        Args:
            connection_id: 已保存的 Provider Connection。
            slot_id: primary 或 standby slot。
            model: 内置目录模型。
            dimension: 固定向量维度。
            document_policy_identity: 文档请求策略身份。
            query_policy_identity: 查询请求策略身份。
            resolved: 产品组合根传入的完整已解析策略。

        Returns:
            不把密钥复制到配置或环境变量的真实 Provider adapter。

        """
        connection = self._control.get_connection(connection_id)
        validate_model(connection.provider_type, model, "embedding.document")
        resolver = self._secret_resolver(connection)
        http_client = self._adapter_http_client(
            connection,
            selected_slot=slot_id,
        )
        common = {
            "slot_id": slot_id,
            "model": model,
            "dimension": dimension,
            "request_policy_identity": canonical_sha256(
                {
                    "document": document_policy_identity,
                    "query": query_policy_identity,
                }
            ),
            "document_request_policy_identity": document_policy_identity,
            "query_request_policy_identity": query_policy_identity,
            "document_egress_allowed": True,
            "query_egress_allowed": True,
        }
        if resolved is not None:
            if (resolved.connection_id, resolved.model, resolved.dimension) != (
                connection_id,
                model,
                dimension,
            ):
                raise ValueError("Resolved Embedding 与 Adapter 引用不一致。")
            common.update(
                {
                    "normalization": resolved.normalization,
                    "adapter_revision": resolved.adapter_revision,
                    "max_input_tokens": resolved.max_input_tokens,
                }
            )
            document = dict(resolved.document_policy)
            query = dict(resolved.query_policy)
            if connection.provider_type == "jina":
                common.update(
                    {
                        "document_task": document["task"],
                        "query_task": query["task"],
                    }
                )
            else:
                common.update(
                    {
                        "document_text_type": document["text_type"],
                        "query_text_type": query["text_type"],
                        "query_instruct": query["query_instruct"],
                    }
                )
        if connection.provider_type == "jina":
            return JinaV5TextEmbeddingAdapter(
                JinaEmbeddingConfig.model_validate(common),
                http_client=http_client,
                api_key_resolver=resolver,
            )
        return AliyunQwen37EmbeddingAdapter(
            AliyunQwen37EmbeddingConfig.model_validate(
                {
                    **common,
                    "region": connection.region or "cn-beijing",
                    "endpoint_mode": connection.endpoint_mode,
                    "api_host": connection.api_host,
                }
            ),
            http_client=http_client,
            api_key_resolver=resolver,
            workspace_id=connection.workspace_id,
            region=connection.region,
        )

    def reranker_adapter(
        self,
        connection_id: str,
        *,
        model: str,
    ) -> JinaRerankerV35Adapter:
        """创建调用时解析页面托管密钥的 Jina Reranker。

        Args:
            connection_id: 已保存的 Jina Connection。
            model: 内置目录 Reranker 模型。

        Returns:
            复用安全传输边界的 Jina Reranker adapter。

        """
        connection = self._control.get_connection(connection_id)
        validate_model(connection.provider_type, model, "reranking")
        if connection.provider_type != "jina":
            raise ValueError("V1 Reranker 只支持 Jina。")
        return JinaRerankerV35Adapter(
            JinaRerankerConfig(model=model, egress_allowed=True),
            http_client=self._adapter_http_client(
                connection,
                reranker_mode="remote",
            ),
            api_key_resolver=self._secret_resolver(connection),
        )

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
        if not connection.enabled:
            raise _ProviderConfigurationError(
                "连接已停用。", stage="provider.config"
            )
        secret, key_version = self._credentials.resolve(
            connection.credential_id
        )
        endpoint = _base_url(connection)
        cache_key = (
            connection.connection_id,
            connection.configuration_version,
            key_version,
            endpoint,
        )
        existing = self._clients.get(cache_key)
        if existing is not None:
            return existing, key_version
        self.invalidate_credential(connection.credential_id)
        headers = {"Authorization": f"Bearer {secret}"}
        transport = (
            None
            if self._transport_factory is None
            else self._transport_factory(connection)
        )
        client = httpx.Client(
            base_url=_base_url(connection),
            headers=headers,
            timeout=httpx.Timeout(10.0),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )
        self._clients[cache_key] = client
        return client, key_version

    def _secret_resolver(
        self, connection: ProviderConnection
    ) -> Callable[[], str]:
        def _resolve() -> str:
            value, _ = self._credentials.resolve(connection.credential_id)
            return value

        return _resolve

    def _adapter_http_client(
        self,
        connection: ProviderConnection,
        *,
        selected_slot: str | None = None,
        reranker_mode: str | None = None,
    ) -> ProviderHttpClient:
        if not connection.enabled:
            raise ConfigurationError("连接已停用。", stage="provider.config")
        transport = (
            None
            if self._transport_factory is None
            else self._transport_factory(connection)
        )
        client = (
            None if transport is None else httpx.Client(transport=transport)
        )
        if connection.provider_type == "jina":
            base_url = "https://api.jina.ai/v1"
        else:
            base_url = _base_url(connection)
        return ProviderHttpClient(
            base_url,
            client=client,
            observer=lambda call: self._control.record_provider_call(
                connection.connection_id,
                call,
                selected_slot=selected_slot,
                failover=(
                    selected_slot == "standby"
                    and call.operation == "embedding.query"
                ),
                reranker_mode=reranker_mode,
            ),
            defer_success_observation=True,
        )


class _ValidationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ProviderConfigurationError(ConfigurationError):
    """标记 Provider endpoint 的非敏感配置错误。"""


def _request_error_details(error: httpx.RequestError) -> tuple[str, str]:
    """把传输异常映射为不含异常消息的稳定分类。"""
    if isinstance(error, httpx.ConnectError):
        return "connect_error", "PROVIDER_CONNECT_ERROR"
    if isinstance(error, httpx.ReadError):
        return "read_error", "PROVIDER_READ_ERROR"
    if isinstance(error, httpx.WriteError):
        return "write_error", "PROVIDER_WRITE_ERROR"
    if isinstance(error, httpx.RemoteProtocolError):
        return "remote_protocol_error", "PROVIDER_REMOTE_PROTOCOL_ERROR"
    return "network_error", "PROVIDER_NETWORK_ERROR"


def build_offline_mock_transport(
    connection: ProviderConnection,
) -> httpx.MockTransport:
    """构造不会访问网络的成功 MockTransport。

    Args:
        connection: 用于选择固定响应形状的连接。

    Returns:
        仅处理内存请求的 httpx MockTransport。

    """

    def _handler(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        body = json.loads(request.content.decode("utf-8"))
        if "rerank" in request.url.path:
            documents = body.get("documents")
            if (
                request.url.host != "api.jina.ai"
                or request.url.path != "/v1/rerank"
                or body.get("model") != "jina-reranker-v3.5"
                or not isinstance(documents, list)
                or not documents
                or any(
                    not isinstance(item, str) or not item for item in documents
                )
                or body.get("top_n") != len(documents)
                or body.get("top_n") <= 0
                or body.get("return_documents") is not False
            ):
                return httpx.Response(400)
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "results": [
                        {"index": index, "relevance_score": 0.9 - index / 100}
                        for index, _ in enumerate(documents)
                    ],
                    "usage": {"total_tokens": 12},
                },
            )
        if connection.provider_type == "jina":
            if (
                request.url.host != "api.jina.ai"
                or request.url.path != "/v1/embeddings"
                or body.get("model") != "jina-embeddings-v5-text-small"
                or body.get("task")
                not in {"retrieval.passage", "retrieval.query"}
                or body.get("dimensions") != _EMBEDDING_DIMENSION
                or body.get("normalized") is not True
                or body.get("embedding_type") != "float"
                or body.get("truncate") is not False
            ):
                return httpx.Response(400)
            inputs = body.get("input")
            if not isinstance(inputs, list):
                return httpx.Response(400)
            dimension = int(body.get("dimensions", 8))
            vector = [0.125] * dimension
            payload: dict[str, Any] = {
                "data": [
                    {"embedding": vector, "index": index}
                    for index, _ in enumerate(inputs)
                ],
                "model": body["model"],
                "usage": {"total_tokens": 8},
            }
        else:
            raw_input = body.get("input")
            parameters = body.get("parameters")

            expected_host = urlsplit(_base_url(connection)).hostname
            if (
                request.url.host != expected_host
                or not isinstance(raw_input, dict)
                or not isinstance(parameters, dict)
                or body.get("model") != "qwen3.7-text-embedding"
                or parameters.get("text_type") not in {"document", "query"}
                or parameters.get("dimension") != _EMBEDDING_DIMENSION
                or parameters.get("output_type") != "dense"
                or any(key in body for key in ("dimensions", "region", "task"))
            ):
                return httpx.Response(400)
            text_type = parameters["text_type"]
            if (
                text_type == "query"
                and not isinstance(parameters.get("instruct"), str)
            ) or (text_type == "document" and "instruct" in parameters):
                return httpx.Response(400)
            texts = raw_input.get("texts")
            if not isinstance(texts, list):
                return httpx.Response(400)
            dimension = int(parameters.get("dimension", 8))
            vector = [0.125] * dimension
            payload = {
                "code": "",
                "status_code": 200,
                "output": {
                    "embeddings": [
                        {"embedding": vector, "text_index": index}
                        for index, _ in enumerate(texts)
                    ]
                },
                "usage": {"total_tokens": 8},
            }
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(_handler)


def _payload(
    connection: ProviderConnection,
    operation: str,
    model: str,
    *,
    resolved: ResolvedEmbeddingSpec | None = None,
) -> dict[str, object]:
    if operation == "reranking":
        return {
            "documents": list(_SYNTHETIC_RERANK_DOCUMENTS),
            "model": model,
            "query": _SYNTHETIC_TEXT,
            "return_documents": False,
            "top_n": len(_SYNTHETIC_RERANK_DOCUMENTS),
        }
    is_document = operation.endswith("document")
    if connection.provider_type == "jina":
        config = JinaEmbeddingConfig(
            slot_id="validation",
            model=model,
            request_policy_identity="validation",
        )
        return {
            "dimensions": config.dimension,
            "embedding_type": config.embedding_type,
            "input": [_SYNTHETIC_TEXT],
            "model": model,
            "normalized": config.normalization == "l2-v1",
            "task": (
                config.document_task if is_document else config.query_task
            ),
            "truncate": False,
        }
    aliyun_config = AliyunQwen37EmbeddingConfig(
        slot_id="validation",
        model=model,
        request_policy_identity="validation",
        region=connection.region or "cn-beijing",
    )
    return embedding_payload(
        [_SYNTHETIC_TEXT],
        model=model,
        dimension=aliyun_config.dimension,
        text_type="document" if is_document else "query",
        instruct=aliyun_config.query_instruct
        if resolved is None
        else str(dict(resolved.query_policy)["query_instruct"]),
    )


def _validation_spec(
    connection: ProviderConnection,
    operation: str,
    model: str,
    dimension: int | None,
    policy: dict[str, object] | None,
) -> ResolvedEmbeddingSpec | None:
    if operation == "reranking":
        if policy:
            raise ValueError("Reranker 暂不支持可编辑请求策略。")
        return None
    return resolve_embedding(
        connection,
        model,
        dimension or 1024,
        (policy or {}) if operation == "embedding.document" else {},
        (policy or {}) if operation == "embedding.query" else {},
    )


def _probe_response_contract(
    response: httpx.Response,
    provider_type: str,
    operation: str,
    model: str,
    dimension: int | None,
) -> tuple[int | None, int | None]:
    if len(response.content) > _MAX_VALIDATION_RESPONSE_BYTES:
        raise _ValidationError("RESPONSE_TOO_LARGE")
    if (
        "application/json"
        not in response.headers.get("content-type", "").casefold()
    ):
        raise _ValidationError("INVALID_CONTENT_TYPE")
    return _validate_payload(
        provider_type,
        operation,
        response.json(),
        expected_model=model,
        expected_dimension=dimension,
    )


def _validate_payload(  # noqa: PLR0912
    provider_type: str,
    operation: str,
    payload: object,
    *,
    expected_model: str,
    expected_dimension: int | None,
) -> tuple[int | None, int | None]:
    if not isinstance(payload, dict):
        raise TypeError("响应必须为 object。")
    if provider_type == "jina":
        observed_model = payload.get("model")
        if observed_model != expected_model:
            raise ValueError("Jina 响应模型不匹配。")
    if operation == "reranking":
        results = payload["results"]
        if not isinstance(results, list):
            raise _ValidationError("RERANK_CANDIDATE_MISSING")
        scores: dict[int, float] = {}
        for result in results:
            if not isinstance(result, dict):
                raise TypeError("Reranker 候选必须为 object。")
            index = result.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index in scores
                or not 0 <= index < len(_SYNTHETIC_RERANK_DOCUMENTS)
            ):
                raise _ValidationError("RERANK_CANDIDATE_MISSING")
            score_value = result.get("relevance_score", result.get("score"))
            scores[index] = finite_score(score_value)
            echoed = result.get("document")
            if (
                isinstance(echoed, str)
                and echoed != _SYNTHETIC_RERANK_DOCUMENTS[index]
            ):
                raise ValueError("Reranker document 回显与索引不一致。")
        if set(scores) != set(range(len(_SYNTHETIC_RERANK_DOCUMENTS))):
            raise _ValidationError("RERANK_CANDIDATE_MISSING")
        return None, usage_tokens(payload)
    if provider_type == "jina":
        vectors = payload["data"]
        index_field = "index"
    else:
        dimension = expected_dimension or _EMBEDDING_DIMENSION
        try:
            _, tokens = decode_embeddings(
                payload, expected_count=1, dimension=dimension
            )
        except ValueError as error:
            if "维度" in str(error):
                raise _ValidationError("EMBEDDING_DIMENSION_MISMATCH") from None
            raise
        return dimension, tokens
    dimension = expected_dimension or _EMBEDDING_DIMENSION
    if isinstance(vectors, list) and vectors:
        first_item = vectors[0]
        if isinstance(first_item, dict):
            first_vector = first_item.get("embedding")
            if (
                isinstance(first_vector, list)
                and len(first_vector) != dimension
            ):
                raise _ValidationError("EMBEDDING_DIMENSION_MISMATCH")
    try:
        ordered_vectors(
            vectors,
            expected_count=1,
            dimension=dimension,
            index_field=index_field,
            vector_field="embedding",
        )
    except ValueError as error:
        if "维度" in str(error):
            raise _ValidationError("EMBEDDING_DIMENSION_MISMATCH") from None
        raise
    return dimension, usage_tokens(payload)


def _estimated_tokens(
    connection: ProviderConnection,
    operation: str,
) -> int:
    texts = [_SYNTHETIC_TEXT]
    if operation == "reranking":
        texts.extend(_SYNTHETIC_RERANK_DOCUMENTS)
    elif (
        connection.provider_type == "aliyun-model-studio"
        and operation.endswith("query")
    ):
        texts.append(
            AliyunQwen37EmbeddingConfig(
                slot_id="validation",
                request_policy_identity="validation",
            ).query_instruct
        )
    return sum(estimate_tokens(text) for text in texts)


def _base_url(connection: ProviderConnection) -> str:
    if connection.provider_type == "jina":
        return "https://api.jina.ai"
    try:
        return resolve_endpoint(
            AliyunEndpointConfig.model_validate(
                {
                    "workspace_id": connection.workspace_id,
                    "region": connection.region,
                    "endpoint_mode": connection.endpoint_mode,
                    "api_host": connection.api_host,
                }
            )
        )
    except ValueError:
        raise _ProviderConfigurationError(
            "百炼端点配置未通过。", stage="provider.aliyun.config"
        ) from None


def _path(provider_type: str, operation: str) -> str:
    if provider_type == "jina":
        return "/v1/rerank" if operation == "reranking" else "/v1/embeddings"
    return "/api/v1/services/embeddings/text-embedding/text-embedding"


def _http_category(status_code: int, success: str) -> str:
    if status_code == _HTTP_OK:
        return success
    if _HTTP_REDIRECT <= status_code < _HTTP_BAD_REQUEST:
        return "http_3xx"
    if status_code in {401, 403, 429}:
        return f"http_{status_code}"
    if status_code >= _HTTP_SERVER_ERROR:
        return "http_5xx"
    return "http_4xx"


def _http_error_code(
    provider_type: str,
    response: httpx.Response,
) -> str:
    if response.status_code == HTTPStatus.UNAUTHORIZED:
        return "PROVIDER_AUTHENTICATION_FAILED"
    if provider_type == "aliyun-model-studio":
        safe_code = _aliyun_safe_error_code(response)
        if safe_code is not None:
            return safe_code
    status_code = response.status_code
    if status_code in {_HTTP_BAD_REQUEST, 422}:
        return "PROVIDER_REQUEST_INVALID"
    return {
        401: "PROVIDER_AUTHENTICATION_FAILED",
        403: "PROVIDER_AUTHORIZATION_DENIED",
        429: "PROVIDER_RATE_LIMITED",
    }.get(status_code, "PROVIDER_UPSTREAM_ERROR")


def _aliyun_safe_error_code(response: httpx.Response) -> str | None:
    if len(response.content) > _MAX_VALIDATION_RESPONSE_BYTES:
        return None
    if (
        "application/json"
        not in response.headers.get("content-type", "").casefold()
    ):
        return None
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if not isinstance(code, str):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
    if not isinstance(code, str):
        return None
    return _ALIYUN_SAFE_ERROR_CODES.get(code.strip().casefold())


def _identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _safe_response_details(
    response: httpx.Response,
) -> tuple[str | None, str | None]:
    if len(response.content) > _MAX_VALIDATION_RESPONSE_BYTES:
        return None, None
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    code = payload.get("code")
    if (
        not isinstance(code, str)
        or len(code) > _MAX_PROVIDER_CODE
        or code.casefold() not in _ALIYUN_SAFE_ERROR_CODES
    ):
        code = None
    request_id = payload.get("request_id")
    if (
        not isinstance(request_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) is None
    ):
        request_id = None
    return code, request_id


__all__ = [
    "ProviderRuntimeRegistry",
    "TransportFactory",
    "build_offline_mock_transport",
]
