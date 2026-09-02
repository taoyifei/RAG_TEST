"""Jina v5 text Embedding 与 v3.5 Reranker adapters。"""

from __future__ import annotations

import os
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
from rag_app.adapters.providers.validation import finite_score, ordered_vectors
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    PolicyDenied,
    ProviderAuthenticationError,
    ProviderInputTooLarge,
    ProviderInvalidResponse,
)
from rag_app.core.models import (
    EmbeddingRequest,
    EmbeddingRequestRole,
    EmbeddingResult,
    ProviderCall,
    ProviderHealth,
    ProviderHealthStatus,
    RerankExecutionMode,
    RerankItem,
    RerankRequest,
    RerankResult,
)

_JINA_BASE_URL = "https://api.jina.ai/v1"
_JINA_EMBEDDING_MODEL = "jina-embeddings-v5-text-small"
_JINA_RERANKER_MODEL = "jina-reranker-v3.5"
_DIMENSION = 1024


class JinaEmbeddingConfig(BaseModel):
    """Jina Embedding 的严格非敏感配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str
    provider_id: str = "jina-embedding"
    model: str = _JINA_EMBEDDING_MODEL
    dimension: StrictInt = Field(default=1024, gt=0)
    request_policy_identity: str
    document_request_policy_identity: str | None = None
    query_request_policy_identity: str | None = None
    adapter_revision: str = "1"
    api_key_env: str = "JINA_API_KEY"
    document_egress_allowed: bool = False
    query_egress_allowed: bool = False
    max_input_tokens: StrictInt = Field(default=32768, gt=0)
    document_task: str = "retrieval.passage"
    query_task: str = "retrieval.query"
    embedding_type: str = "float"
    normalization: str = "l2-v1"


class JinaRerankerConfig(BaseModel):
    """Jina Reranker 的严格非敏感配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = _JINA_RERANKER_MODEL
    api_key_env: str = "JINA_API_KEY"
    egress_allowed: bool = False
    max_total_tokens: StrictInt = Field(default=32768, gt=0)
    max_candidates: StrictInt = Field(default=100, gt=0)
    request_policy_revision: str = "1"


class JinaV5TextEmbeddingAdapter:
    """固定角色 task、1024 维和 ``l2-v1`` 的 Jina adapter。"""

    def __init__(
        self,
        config: JinaEmbeddingConfig,
        *,
        http_client: ProviderHttpClient | None = None,
    ) -> None:
        """保存非敏感配置并创建长生命周期连接池。

        Args:
            config: slot、模型、维度和出网授权。
            http_client: 可注入 MockTransport 的同步客户端。

        Returns:
            无返回值。

        """
        if (
            config.model != _JINA_EMBEDDING_MODEL
            or config.dimension != _DIMENSION
        ):
            raise ValueError("Jina v5 adapter 只接受固定模型和 1024 维。")
        if config.normalization != "l2-v1":
            raise ValueError("Jina v5 adapter 只接受 l2-v1 normalization。")
        self._config = config
        self._http = http_client or ProviderHttpClient(_JINA_BASE_URL)
        self._closed = False
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name=config.provider_id,
            version=config.model,
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
                dimensions=(config.dimension,),
                roles=("document", "query"),
            ),
        )

    @property
    def config(self) -> JinaEmbeddingConfig:
        """返回不含凭据值的已解析配置。

        Args:
            无参数；读取构造时已验证的配置。

        Returns:
            仅含公开字段和环境变量名的 Jina Embedding 配置。

        """
        return self._config

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回 Jina 固定批量、角色和维度能力。

        Args:
            无参数；读取当前 adapter。

        Returns:
            组合阶段能力声明。

        """
        return self.descriptor.capabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """按 API task 生成并校验 Jina 向量。

        Args:
            request: 显式 slot、DOCUMENT/QUERY 角色和文本。

        Returns:
            跨批恢复顺序且执行 ``l2-v1`` 的向量。

        Raises:
            PolicyDenied: 角色对应 Jina 出网未授权。
            ProviderAuthenticationError: API Key 环境变量缺失。
            ProviderInputTooLarge: 本地保守 Token 限制被超过。
            ProviderInvalidResponse: Jina 响应违反合同。

        """
        if request.slot_id != self._config.slot_id:
            raise ValueError("Jina Embedding slot 不匹配。")
        self._check_egress(request.role)
        api_key = os.environ.get(self._config.api_key_env)
        if not api_key:
            raise ProviderAuthenticationError(
                "Jina API Key 环境变量未配置。",
                stage="provider.jina.embedding",
                details={"api_key_env": self._config.api_key_env},
            )
        limits = BatchLimits(max_input_tokens=self._config.max_input_tokens)
        batches = batch_texts(request.texts, limits)
        task = (
            self._config.document_task
            if request.role is EmbeddingRequestRole.DOCUMENT
            else self._config.query_task
        )
        vectors: list[tuple[float, ...]] = []
        calls: list[ProviderCall] = []
        for batch in batches:
            try:
                response = self._http.request_json(
                    "POST",
                    "/embeddings",
                    payload={
                        "model": self._config.model,
                        "task": task,
                        "dimensions": self._config.dimension,
                        "normalized": self._config.normalization == "l2-v1",
                        "embedding_type": self._config.embedding_type,
                        "truncate": False,
                        "input": list(batch),
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    provider_id="jina",
                    operation="embedding",
                    model=self._config.model,
                    input_count=len(batch),
                    estimated_tokens=sum(
                        estimate_tokens(text) for text in batch
                    ),
                )
            except ProviderHttpError as failure:
                raise provider_error(
                    failure, stage="provider.jina.embedding"
                ) from None
            try:
                payload = _mapping(response.payload)
                observed_model = payload.get("model")
                if (
                    observed_model is not None
                    and observed_model != self._config.model
                ):
                    raise ValueError("Jina response model 不匹配。")
                batch_vectors = ordered_vectors(
                    payload.get("data"),
                    expected_count=len(batch),
                    dimension=self._config.dimension,
                    index_field="index",
                    vector_field="embedding",
                )
            except (TypeError, ValueError) as error:
                raise invalid_response_error(
                    type(error).__name__,
                    response.call,
                    stage="provider.jina.embedding",
                ) from None
            vectors.extend(batch_vectors)
            calls.append(response.call)
        if len(vectors) != len(request.texts):
            raise ProviderInvalidResponse(
                "Jina 跨批向量总数与输入不一致。",
                stage="provider.jina.embedding",
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
        """返回配置状态且默认不执行付费网络探测。

        Args:
            network: 当前实现不隐式执行模型请求。

        Returns:
            KEY 缺失时 DEGRADED_CONFIG，否则 UNKNOWN。

        """
        del network
        configured = bool(os.environ.get(self._config.api_key_env))
        return ProviderHealth(
            status=(
                ProviderHealthStatus.UNKNOWN
                if configured
                else ProviderHealthStatus.DEGRADED_CONFIG
            ),
            reason_code="NOT_PROBED" if configured else "JINA_KEY_MISSING",
        )

    def close(self) -> None:
        """幂等关闭 HTTP 连接池。

        Args:
            无参数；关闭当前 adapter。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self._http.close()

    def _check_egress(self, role: EmbeddingRequestRole) -> None:
        allowed = (
            self._config.document_egress_allowed
            if role is EmbeddingRequestRole.DOCUMENT
            else self._config.query_egress_allowed
        )
        if not allowed:
            raise PolicyDenied(
                "Jina Embedding 出网未授权。",
                stage="provider.jina.egress",
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


class JinaRerankerV35Adapter:
    """对完整候选集评分的 Jina v3.5 Reranker adapter。"""

    def __init__(
        self,
        config: JinaRerankerConfig,
        *,
        http_client: ProviderHttpClient | None = None,
    ) -> None:
        """保存模型、授权与长生命周期连接池。

        Args:
            config: 固定模型和安全限制。
            http_client: 可注入的同步 HTTP 客户端。

        Returns:
            无返回值。

        """
        if config.model != _JINA_RERANKER_MODEL:
            raise ValueError("Jina Reranker adapter 只接受 v3.5。")
        self._config = config
        self._http = http_client or ProviderHttpClient(_JINA_BASE_URL)
        self._closed = False
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.RERANKER,
            name="jina-reranker",
            version=config.model,
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
            ),
        )

    @property
    def config(self) -> JinaRerankerConfig:
        """返回不含凭据值的已解析配置。

        Args:
            无参数；读取构造时已验证的配置。

        Returns:
            仅含公开字段和环境变量名的 Jina Reranker 配置。

        """
        return self._config

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回 Jina Reranker 能力。

        Args:
            无参数；读取当前 adapter。

        Returns:
            组合阶段能力声明。

        """
        return self.descriptor.capabilities

    def rerank(self, request: RerankRequest) -> RerankResult:
        """要求 Jina 对传入候选集合完整评分。

        Args:
            request: 查询、候选 ID/文本和应用层 limit。

        Returns:
            按相关分数排序并截取应用层 limit 的结果。

        Raises:
            PolicyDenied: Jina Reranker 出网未授权。
            ProviderInputTooLarge: 本地总 Token 预算被超过。
            ProviderInvalidResponse: 返回缺失候选或分数无效。

        """
        if not self._config.egress_allowed:
            raise PolicyDenied(
                "Jina Reranker 出网未授权。",
                stage="provider.jina.reranker.egress",
            )
        if len(request.candidates) > self._config.max_candidates:
            raise ProviderInputTooLarge(
                "Jina Reranker 候选数超过本地上限。",
                stage="provider.jina.reranker",
                details={"candidate_count": len(request.candidates)},
            )
        api_key = os.environ.get(self._config.api_key_env)
        if not api_key:
            raise ProviderAuthenticationError(
                "Jina API Key 环境变量未配置。",
                stage="provider.jina.reranker",
                details={"api_key_env": self._config.api_key_env},
            )
        documents = tuple(text for _, text in request.candidates)
        estimated_tokens = estimate_tokens(request.query) + sum(
            estimate_tokens(document) for document in documents
        )
        if estimated_tokens > self._config.max_total_tokens:
            raise ProviderInputTooLarge(
                "Jina Reranker 输入超过本地总 Token 预算。",
                stage="provider.jina.reranker",
                details={"estimated_tokens": estimated_tokens},
            )
        try:
            response = self._http.request_json(
                "POST",
                "/rerank",
                payload={
                    "model": self._config.model,
                    "query": request.query,
                    "documents": list(documents),
                    "top_n": len(documents),
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                provider_id="jina",
                operation="reranking",
                model=self._config.model,
                input_count=len(documents),
                estimated_tokens=estimated_tokens,
            )
        except ProviderHttpError as failure:
            raise provider_error(
                failure, stage="provider.jina.reranker"
            ) from None
        try:
            items = _rerank_items(response.payload, request)
        except (TypeError, ValueError) as error:
            raise invalid_response_error(
                type(error).__name__,
                response.call,
                stage="provider.jina.reranker",
            ) from None
        return RerankResult(
            mode=RerankExecutionMode.PROVIDER,
            items=items,
            calls=(response.call,),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """只检查 Key 配置，不隐式消费 Token。

        Args:
            network: 当前实现不隐式执行模型请求。

        Returns:
            KEY 缺失时 DEGRADED_CONFIG，否则 UNKNOWN。

        """
        del network
        configured = bool(os.environ.get(self._config.api_key_env))
        return ProviderHealth(
            status=(
                ProviderHealthStatus.UNKNOWN
                if configured
                else ProviderHealthStatus.DEGRADED_CONFIG
            ),
            reason_code="NOT_PROBED" if configured else "JINA_KEY_MISSING",
        )

    def close(self) -> None:
        """幂等关闭 HTTP 连接池。

        Args:
            无参数；关闭当前 adapter。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self._http.close()


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("Provider response 必须是 object。")
    return value


def _rerank_items(
    payload: object, request: RerankRequest
) -> tuple[RerankItem, ...]:
    response = _mapping(payload)
    results = response.get("results")
    if not isinstance(results, list):
        raise ValueError("Jina rerank results 必须是 list。")
    scores: dict[int, float] = {}
    for result in results:
        item = _mapping(result)
        index = item.get("index")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index in scores
            or not 0 <= index < len(request.candidates)
        ):
            raise ValueError("Jina rerank index 重复或越界。")
        score_value = item.get("relevance_score", item.get("score"))
        scores[index] = finite_score(score_value)
        echoed = item.get("document")
        if isinstance(echoed, str) and echoed != request.candidates[index][1]:
            raise ValueError("Jina rerank document 回显与索引不一致。")
    if set(scores) != set(range(len(request.candidates))):
        raise ValueError("Jina rerank 没有完整返回全部候选。")
    ranked = [
        (
            index,
            RerankItem(
                candidate_id=request.candidates[index][0],
                score=scores[index],
            ),
        )
        for index in range(len(request.candidates))
    ]
    ranked.sort(key=lambda item: -item[1].score)
    return tuple(item for _, item in ranked[: request.limit])


__all__ = [
    "JinaEmbeddingConfig",
    "JinaRerankerConfig",
    "JinaRerankerV35Adapter",
    "JinaV5TextEmbeddingAdapter",
]
