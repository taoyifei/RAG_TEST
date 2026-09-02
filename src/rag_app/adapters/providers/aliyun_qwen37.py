"""阿里百炼 qwen3.7 原生文本 Embedding adapter。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictInt

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
from rag_app.adapters.providers.validation import ordered_vectors
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
_HOST = re.compile(r"^[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com$")
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
    api_key_env: str = "DASHSCOPE_API_KEY"
    workspace_id_env: str = "ALIYUN_MODEL_STUDIO_WORKSPACE_ID"
    region_env: str = "ALIYUN_MODEL_STUDIO_REGION"
    region: str = _REGION
    document_egress_allowed: bool = False
    query_egress_allowed: bool = False
    max_input_tokens: StrictInt = Field(default=128000, gt=0)
    query_instruct: str = _QUERY_INSTRUCTION


class AliyunQwen37EmbeddingAdapter:
    """固定北京业务空间原生 endpoint 的 Qwen3.7 adapter。"""

    def __init__(
        self,
        config: AliyunQwen37EmbeddingConfig,
        *,
        http_client: ProviderHttpClient | None = None,
    ) -> None:
        """保存配置并延迟构造受控业务空间连接池。

        Args:
            config: slot、模型、环境变量名和出网授权。
            http_client: 可注入 MockTransport 的固定 endpoint 客户端。

        Returns:
            无返回值。

        """
        if config.model != _MODEL or config.dimension != _DIMENSION:
            raise ValueError("Qwen3.7 adapter 只接受固定模型和 1024 维。")
        if config.region != _REGION:
            raise ValueError("Qwen3.7 V1 只允许 cn-beijing。")
        self._config = config
        self._http = http_client
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
        api_key = os.environ.get(self._config.api_key_env)
        if not api_key:
            raise ProviderAuthenticationError(
                "阿里百炼 API Key 环境变量未配置。",
                stage="provider.aliyun.embedding",
                details={"api_key_env": self._config.api_key_env},
            )
        client = self._client()
        limits = BatchLimits(
            max_items=16,
            max_input_tokens=self._config.max_input_tokens,
        )
        batches = batch_texts(request.texts, limits)
        vectors: list[tuple[float, ...]] = []
        calls: list[ProviderCall] = []
        for batch in batches:
            parameters: dict[str, object] = {
                "text_type": request.role.value,
                "dimension": self._config.dimension,
                "output_type": "dense",
            }
            if request.role is EmbeddingRequestRole.QUERY:
                parameters["instruct"] = self._config.query_instruct
            try:
                response = client.request_json(
                    "POST",
                    _PATH,
                    payload={
                        "model": self._config.model,
                        "input": {"texts": list(batch)},
                        "parameters": parameters,
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    provider_id="aliyun-qwen37",
                    operation="embedding",
                    model=self._config.model,
                    input_count=len(batch),
                    estimated_tokens=sum(
                        estimate_tokens(text) for text in batch
                    ),
                )
            except ProviderHttpError as failure:
                raise provider_error(
                    failure, stage="provider.aliyun.embedding"
                ) from None
            try:
                payload = _mapping(response.payload)
                status_code = payload.get("status_code")
                if status_code not in (200, "200"):
                    raise ValueError("Qwen3.7 status_code 不是 200。")
                if payload.get("code") not in (None, ""):
                    raise ValueError("Qwen3.7 成功响应包含错误 code。")
                output = _mapping(payload.get("output"))
                batch_vectors = ordered_vectors(
                    output.get("embeddings"),
                    expected_count=len(batch),
                    dimension=self._config.dimension,
                    index_field="text_index",
                    vector_field="embedding",
                )
            except (TypeError, ValueError) as error:
                raise invalid_response_error(
                    type(error).__name__,
                    response.call,
                    stage="provider.aliyun.embedding",
                ) from None
            vectors.extend(batch_vectors)
            calls.append(response.call)
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
            request_policy_identity=self._config.request_policy_identity,
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
        configured = all(
            (
                os.environ.get(self._config.api_key_env),
                os.environ.get(self._config.workspace_id_env),
            )
        ) and self._resolved_region() == _REGION
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

    def _client(self) -> ProviderHttpClient:
        if self._closed:
            raise RuntimeError("Qwen3.7 adapter 已关闭。")
        if self._http is not None:
            return self._http
        region = self._resolved_region()
        workspace_id = os.environ.get(self._config.workspace_id_env, "")
        if (
            not workspace_id
            or re.fullmatch(r"[a-z0-9-]+", workspace_id) is None
        ):
            raise ConfigurationError(
                "阿里 Workspace ID 缺失或格式无效。",
                stage="provider.aliyun.config",
                details={"workspace_id_env": self._config.workspace_id_env},
            )
        host = f"{workspace_id}.{region}.maas.aliyuncs.com"
        if _HOST.fullmatch(host) is None:
            raise ConfigurationError(
                "阿里业务空间 host 不在 V1 allowlist。",
                stage="provider.aliyun.config",
            )
        self._http = ProviderHttpClient(f"https://{host}")
        return self._http

    def _resolved_region(self) -> str:
        region = os.environ.get(self._config.region_env, self._config.region)
        if region != _REGION:
            raise ConfigurationError(
                "Qwen3.7 V1 只允许 cn-beijing。",
                stage="provider.aliyun.config",
                details={"region_env": self._config.region_env},
            )
        return region


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("Provider response 必须是 object。")
    return value


__all__ = [
    "AliyunQwen37EmbeddingAdapter",
    "AliyunQwen37EmbeddingConfig",
]
