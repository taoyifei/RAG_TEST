"""旧 TEI Embedding 与内部 Reranker 的 Core 端口桥接。"""

from __future__ import annotations

from rag_app.clients.model_services import RerankerClient, TeiEmbeddingClient
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
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


class LegacyTeiEmbeddingAdapter:
    """把旧 TEI 客户端投影为显式 slot 的 EmbeddingPort。"""

    def __init__(  # noqa: PLR0913
        self,
        client: TeiEmbeddingClient,
        *,
        slot_id: str,
        model: str,
        dimension: int,
        request_policy_identity: str,
        query_instruction: str = "",
    ) -> None:
        """保存旧客户端和不可混用的 slot 身份。

        Args:
            client: 已有同步 TEI 客户端。
            slot_id: 该实例唯一允许的 slot。
            model: 旧模型身份。
            dimension: 严格向量维度。
            request_policy_identity: 进入指纹的策略身份。
            query_instruction: 旧查询文本前缀策略。

        Returns:
            无返回值。

        """
        self._client = client
        self._slot_id = slot_id
        self._dimension = dimension
        self._policy_identity = request_policy_identity
        self._query_instruction = query_instruction
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name="legacy-tei",
            version=model,
            mode=ProviderMode.LEGACY,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
                dimensions=(dimension,),
                roles=("document", "query"),
            ),
        )

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回旧 TEI 能力。

        Args:
            无参数；读取当前 adapter。

        Returns:
            组合阶段能力声明。

        """
        return self.descriptor.capabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """调用旧客户端并保留显式 slot。

        Args:
            request: Core Embedding 请求。

        Returns:
            Core Embedding 结果。

        """
        if request.slot_id != self._slot_id:
            raise ValueError("Legacy TEI slot 不匹配。")
        instruction = (
            self._query_instruction
            if request.role is EmbeddingRequestRole.QUERY
            else ""
        )
        result = self._client.embed(request.texts, instruction=instruction)
        calls = tuple(
            ProviderCall(
                provider_id="legacy-tei",
                operation="embedding",
                call_count=1,
                retry_count=audit.retry_count,
                elapsed_ms=round(audit.elapsed_seconds * 1000),
                model=self.descriptor.version,
                endpoint=audit.endpoint,
                attempt_count=audit.retry_count + 1,
                status_category="SUCCESS",
                input_count=len(request.texts),
            )
            for audit in result.calls
        )
        return EmbeddingResult(
            slot_id=request.slot_id,
            role=request.role,
            vectors=result.vectors,
            observed_dimension=self._dimension,
            request_policy_identity=self._policy_identity,
            calls=calls,
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """返回旧服务的未探测状态。

        Args:
            network: 不触发隐式探测。

        Returns:
            UNKNOWN 健康状态。

        """
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.UNKNOWN,
            reason_code="LEGACY_NOT_PROBED",
        )


class LegacyInternalRerankerAdapter:
    """把旧内部 Reranker 客户端投影为 RerankerPort。"""

    def __init__(self, client: RerankerClient, *, model: str) -> None:
        """保存旧客户端与模型身份。

        Args:
            client: 已有同步内部 Reranker 客户端。
            model: 可审计模型身份。

        Returns:
            无返回值。

        """
        self._client = client
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.RERANKER,
            name="legacy-internal-reranker",
            version=model,
            mode=ProviderMode.LEGACY,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
            ),
        )

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回旧 Reranker 能力。

        Args:
            无参数；读取当前 adapter。

        Returns:
            组合阶段能力声明。

        """
        return self.descriptor.capabilities

    def rerank(self, request: RerankRequest) -> RerankResult:
        """调用旧客户端并映射候选 ID。

        Args:
            request: Core Rerank 请求。

        Returns:
            Core Rerank 结果。

        """
        documents = tuple(text for _, text in request.candidates)
        result = self._client.rerank(request.query, documents)
        items = tuple(
            RerankItem(
                candidate_id=request.candidates[item.index][0],
                score=item.score,
            )
            for item in result.items
        )
        call = ProviderCall(
            provider_id="legacy-internal-reranker",
            operation="reranking",
            call_count=1,
            retry_count=result.call.retry_count,
            elapsed_ms=round(result.call.elapsed_seconds * 1000),
            model=self.descriptor.version,
            endpoint=result.call.endpoint,
            attempt_count=result.call.retry_count + 1,
            status_category="SUCCESS",
            input_count=len(documents),
        )
        return RerankResult(
            mode=RerankExecutionMode.PROVIDER,
            items=items,
            calls=(call,),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """返回旧服务的未探测状态。

        Args:
            network: 不触发隐式探测。

        Returns:
            UNKNOWN 健康状态。

        """
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.UNKNOWN,
            reason_code="LEGACY_NOT_PROBED",
        )


__all__ = [
    "LegacyInternalRerankerAdapter",
    "LegacyTeiEmbeddingAdapter",
]
