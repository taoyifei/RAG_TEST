"""阿里百炼 qwen3.7 原生文本 Embedding adapter。"""

from __future__ import annotations

import os
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from rag_app.adapters.providers.aliyun_contract import (
    decode_embeddings,
    embedding_payload,
)
from rag_app.adapters.providers.aliyun_endpoint import (
    AliyunEndpointConfig,
    resolve_endpoint,
)
from rag_app.adapters.providers.batching import (
    BatchLimits,
    batch_texts,
    estimate_tokens,
)
from rag_app.adapters.providers.http_common import (
    ProviderHttpClient,
    ProviderHttpError,
    invalid_response_error,
    provider_error,
)
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    ConfigurationError,
    PolicyDenied,
    ProviderAuthenticationError,
    ProviderInvalidResponse,
)
from rag_app.core.models import (
    EmbeddingRequest,
    EmbeddingRequestRole,
    EmbeddingResult,
    ProviderCall,
    ProviderHealth,
    ProviderHealthStatus,
)

_MODEL = "qwen3.7-text-embedding"
_REGION = "cn-beijing"
_DIMENSION = 1024
_PATH = "/api/v1/services/embeddings/text-embedding/text-embedding"
_QUERY_INSTRUCTION = (
    "Given a user query, retrieve the most relevant passages from enterprise "
    "DOCX knowledge bases."
)


class AliyunQwen37EmbeddingConfig(BaseModel):
    """Qwen3.7 原生传输的严格非敏感配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str
    provider_id: str = "aliyun-qwen37-embedding"
    model: str = _MODEL
    dimension: StrictInt = Field(default=1024, gt=0)
    request_policy_identity: str
    document_request_policy_identity: str | None = None
    query_request_policy_identity: str | None = None
    adapter_revision: str = "1"
    api_key_env: str = "DASHSCOPE_API_KEY"
    workspace_id_env: str = "ALIYUN_MODEL_STUDIO_WORKSPACE_ID"
    region_env: str = "ALIYUN_MODEL_STUDIO_REGION"
    region: str = _REGION
    endpoint_mode: str = "workspace_host"
    api_host: str | None = None
    api_host_env: str = "ALIYUN_MODEL_STUDIO_API_HOST"
    document_egress_allowed: bool = False
    query_egress_allowed: bool = False
    max_input_tokens: StrictInt = Field(default=128000, gt=0)
    transport: str = "dashscope-native"
    document_text_type: str = "document"
    query_text_type: str = "query"
    query_instruct: str = _QUERY_INSTRUCTION
    output_type: str = "dense"
    normalization: str = "l2-v1"


class AliyunQwen37EmbeddingAdapter:
    """固定北京业务空间原生 endpoint 的 Qwen3.7 adapter。"""

    def __init__(
        self,
        config: AliyunQwen37EmbeddingConfig,
        *,
        http_client: ProviderHttpClient | None = None,
        api_key_resolver: Callable[[], str] | None = None,
        workspace_id: str | None = None,
        region: str | None = None,
    ) -> None:
        """保存配置并延迟构造受控业务空间连接池。

        Args:
            config: slot、模型、环境变量名和出网授权。
            http_client: 可注入 MockTransport 的固定 endpoint 客户端。
            api_key_resolver: 可选页面托管密钥的调用时解析器。
            workspace_id: 可选页面托管连接的业务空间 ID。
            region: 可选页面托管连接的区域。

        Returns:
            无返回值。

        """
        if config.model != _MODEL or config.dimension != _DIMENSION:
            raise ValueError("Qwen3.7 adapter 只接受固定模型和 1024 维。")
        if config.region != _REGION:
            raise ValueError("Qwen3.7 V1 只允许 cn-beijing。")
        if (
            config.transport != "dashscope-native"
            or config.document_text_type != "document"
            or config.query_text_type != "query"
            or config.output_type != "dense"
            or config.normalization != "l2-v1"
        ):
            raise ValueError("Qwen3.7 adapter 配置偏离已支持请求合同。")
        self._config = config
        self._http = http_client
        self._api_key_resolver = api_key_resolver
        self._workspace_id = workspace_id
        self._region = region
        self._closed = False
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name=config.provider_id,
            version=f"{config.model}:dashscope-native:{config.region}",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
                dimensions=(config.dimension,),
                roles=("document", "query"),
            ),
        )

    @property
    def config(self) -> AliyunQwen37EmbeddingConfig:
        """返回不含凭据值的已解析配置。

        Args:
            无参数；读取构造时已验证的配置。

        Returns:
            仅含公开字段和环境变量名的 Qwen3.7 配置。

        """
        return self._config

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回 Qwen3.7 原生传输能力。

        Args:
            无参数；读取当前 adapter。

        Returns:
            组合阶段能力声明。

        """
        return self.descriptor.capabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """区分 document/query 并仅为 query 添加固定 instruct。

        Args:
            request: 显式 standby slot、角色和文本。

        Returns:
            以 ``text_index`` 恢复顺序并执行 ``l2-v1`` 的向量。

        Raises:
            PolicyDenied: 对应阿里出网未授权。
            ConfigurationError: Workspace 或 Region 无效。
            ProviderAuthenticationError: API Key 缺失。
            ProviderInvalidResponse: 原生响应违反合同。

        """
        if request.slot_id != self._config.slot_id:
            raise ValueError("Qwen3.7 Embedding slot 不匹配。")
        self._check_egress(request.role)
        api_key = self._resolve_api_key()
        client = self._client()
        limits = BatchLimits(
            max_items=16,
            max_input_tokens=self._config.max_input_tokens,
        )
        batches = batch_texts(request.texts, limits)
        instruction_tokens = (
            estimate_tokens(self._config.query_instruct)
            if request.role is EmbeddingRequestRole.QUERY
            else 0
        )
        vectors: list[tuple[float, ...]] = []
        calls: list[ProviderCall] = []
        for batch in batches:
            try:
                response = client.request_json(
                    "POST",
                    _PATH,
                    payload=embedding_payload(
                        batch,
                        model=self._config.model,
                        dimension=self._config.dimension,
                        text_type=request.role.value,
                        instruct=self._config.query_instruct,
                    ),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    provider_id="aliyun-qwen37",
                    operation=f"embedding.{request.role.value}",
                    model=self._config.model,
                    input_count=len(batch),
                    estimated_tokens=sum(
                        estimate_tokens(text) + instruction_tokens
                        for text in batch
                    ),
                )
            except ProviderHttpError as failure:
                raise provider_error(
                    failure, stage="provider.aliyun.embedding"
                ) from None
            observed_tokens: int | None = None
            try:
                batch_vectors, observed_tokens = decode_embeddings(
                    response.payload,
                    expected_count=len(batch),
                    dimension=self._config.dimension,
                )
            except (TypeError, ValueError):
                reason_code = "INVALID_RESPONSE_CONTRACT"
                failed_call = client.complete_call(
                    response.call,
                    observed_tokens=observed_tokens,
                    failure_reason_code=reason_code,
                )
                raise invalid_response_error(
                    reason_code,
                    failed_call,
                    stage="provider.aliyun.embedding",
                ) from None
            completed_call = client.complete_call(
                response.call,
                observed_tokens=observed_tokens,
            )
            vectors.extend(batch_vectors)
            calls.append(completed_call)
        if len(vectors) != len(request.texts):
            raise ProviderInvalidResponse(
                "Qwen3.7 跨批向量总数与输入不一致。",
                stage="provider.aliyun.embedding",
            )
        return EmbeddingResult(
            slot_id=request.slot_id,
            role=request.role,
            vectors=tuple(vectors),
            observed_dimension=self._config.dimension,
            request_policy_identity=self._request_policy_identity(request.role),
            calls=tuple(calls),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """只检查 Key、Workspace 和 Region 配置。

        Args:
            network: 当前实现不隐式执行付费模型请求。

        Returns:
            配置不完整时 DEGRADED_CONFIG，否则 UNKNOWN。

        """
        del network
        configured = (
            all(
                (
                    self._resolve_api_key(required=False),
                    self._resolved_workspace_id(),
                )
            )
            and self._resolved_region() == _REGION
        )
        return ProviderHealth(
            status=(
                ProviderHealthStatus.UNKNOWN
                if configured
                else ProviderHealthStatus.DEGRADED_CONFIG
            ),
            reason_code="NOT_PROBED" if configured else "ALIYUN_CONFIG_MISSING",
        )

    def close(self) -> None:
        """幂等关闭已创建的 HTTP 连接池。

        Args:
            无参数；关闭当前 adapter。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        if self._http is not None:
            self._http.close()

    def _check_egress(self, role: EmbeddingRequestRole) -> None:
        allowed = (
            self._config.document_egress_allowed
            if role is EmbeddingRequestRole.DOCUMENT
            else self._config.query_egress_allowed
        )
        if not allowed:
            raise PolicyDenied(
                "阿里 Embedding 出网未授权。",
                stage="provider.aliyun.egress",
                details={"role": role.value},
            )

    def _request_policy_identity(
        self,
        role: EmbeddingRequestRole,
    ) -> str:
        if role is EmbeddingRequestRole.DOCUMENT:
            return (
                self._config.document_request_policy_identity
                or self._config.request_policy_identity
            )
        return (
            self._config.query_request_policy_identity
            or self._config.request_policy_identity
        )

    def _client(self) -> ProviderHttpClient:
        if self._closed:
            raise RuntimeError("Qwen3.7 adapter 已关闭。")
        if self._http is not None:
            return self._http
        try:
            endpoint = resolve_endpoint(
                AliyunEndpointConfig.model_validate(
                    {
                        "region": self._resolved_region(),
                        "workspace_id": self._resolved_workspace_id(),
                        "endpoint_mode": self._config.endpoint_mode,
                        "api_host": self._config.api_host
                        or os.environ.get(self._config.api_host_env),
                    }
                )
            )
        except ValueError:
            raise ConfigurationError(
                "百炼端点配置未通过。", stage="provider.aliyun.config"
            ) from None
        self._http = ProviderHttpClient(endpoint)
        return self._http

    def _resolve_api_key(self, *, required: bool = True) -> str:
        value = (
            self._api_key_resolver()
            if self._api_key_resolver is not None
            else os.environ.get(self._config.api_key_env, "")
        )
        if required and not value:
            raise ProviderAuthenticationError(
                "阿里百炼 API Key 未配置。",
                stage="provider.aliyun.embedding",
            )
        return value

    def _resolved_workspace_id(self) -> str:
        return self._workspace_id or os.environ.get(
            self._config.workspace_id_env, ""
        )

    def _resolved_region(self) -> str:
        region = self._region or os.environ.get(
            self._config.region_env, self._config.region
        )
        if region != _REGION:
            raise ConfigurationError(
                "Qwen3.7 V1 只允许 cn-beijing。",
                stage="provider.aliyun.config",
                details={"region_env": self._config.region_env},
            )
        return region


__all__ = [
    "AliyunQwen37EmbeddingAdapter",
    "AliyunQwen37EmbeddingConfig",
]
