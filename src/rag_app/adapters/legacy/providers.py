"""P01 离线 Provider 与真实 Provider 的无网络声明边界。"""

from __future__ import annotations

import hashlib
import math

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import ProviderUnavailable
from rag_app.core.models import (
    AnswerDraft,
    EmbeddingRequest,
    EmbeddingRequestRole,
    EmbeddingResult,
    EmbeddingRouteDecision,
    ProviderHealth,
    ProviderHealthStatus,
    RerankExecutionMode,
    RerankItem,
    RerankRequest,
    RerankResult,
)
from rag_app.core.ports import EmbeddingRouteRequest, GenerationRequest


class EmbeddingAdapterConfig(BaseModel):
    """Embedding factory 的严格非敏感配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str
    provider_id: str
    model: str
    dimension: StrictInt = Field(gt=0)
    request_policy_identity: str
    document_request_policy_identity: str
    query_request_policy_identity: str
    document_egress_allowed: bool = False
    query_egress_allowed: bool = False


class RerankerAdapterConfig(BaseModel):
    """Reranker factory 的严格非敏感配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str


class DeterministicEmbeddingProvider:
    """用 SHA-256 生成可复现非语义向量的离线 Provider。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        slot_id: str,
        dimension: int,
        model: str = "deterministic-sha256-v1",
        request_policy_identity: str = "deterministic-v1",
        document_request_policy_identity: str | None = None,
        query_request_policy_identity: str | None = None,
    ) -> None:
        """冻结 slot、维度和策略身份。

        Args:
            slot_id: 只允许处理的 slot。
            dimension: 输出向量维度。
            model: 可审计模型身份。
            request_policy_identity: 文档/查询策略身份。
            document_request_policy_identity: 文档角色策略身份。
            query_request_policy_identity: 查询角色策略身份。

        Returns:
            无返回值。

        """
        self._slot_id = slot_id
        self._dimension = dimension
        self._policy_identity = request_policy_identity
        self._document_policy_identity = (
            document_request_policy_identity or request_policy_identity
        )
        self._query_policy_identity = (
            query_request_policy_identity or request_policy_identity
        )
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name="deterministic",
            version=model,
            mode=ProviderMode.DETERMINISTIC,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                dimensions=(dimension,),
                roles=("document", "query"),
            ),
        )

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回离线批量能力。

        Args:
            无参数；读取当前 Provider。

        Returns:
            维度和角色能力。

        """
        return self.descriptor.capabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """为每条文本生成归一化确定性向量。

        Args:
            request: 显式 slot、角色和文本批次。

        Returns:
            与输入顺序一致的离线向量。

        Raises:
            ValueError: 请求 slot 与 Provider 不匹配。

        """
        if request.slot_id != self._slot_id:
            raise ValueError("deterministic Provider slot 不匹配。")
        vectors = tuple(
            _deterministic_vector(text, self._dimension)
            for text in request.texts
        )
        return EmbeddingResult(
            slot_id=request.slot_id,
            role=request.role,
            vectors=vectors,
            observed_dimension=self._dimension,
            request_policy_identity=(
                self._document_policy_identity
                if request.role is EmbeddingRequestRole.DOCUMENT
                else self._query_policy_identity
            ),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """返回无需网络的健康状态。

        Args:
            network: 被忽略；实现永不访问网络。

        Returns:
            HEALTHY 离线状态。

        """
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.HEALTHY,
            reason_code="DETERMINISTIC_READY",
        )


class DeclaredRemoteEmbeddingProvider:
    """P02 前只验证身份、绝不发 HTTP 的远程 Provider 边界。"""

    def __init__(self, config: EmbeddingAdapterConfig) -> None:
        """保存非敏感 Provider 配置。

        Args:
            config: slot、模型、维度和策略身份。

        Returns:
            无返回值。

        """
        self._config = config
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
    def capabilities(self) -> ComponentCapabilities:
        """返回声明的远程能力。

        Args:
            无参数；读取当前 Provider。

        Returns:
            维度、角色和网络能力。

        """
        return self.descriptor.capabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """失败关闭，避免 P01 意外发送正文。

        Args:
            request: 未发送到网络的请求。

        Returns:
            此实现不会返回。

        Raises:
            ProviderUnavailable: 真实 HTTP adapter 属于 P02。

        """
        del request
        raise ProviderUnavailable(
            "真实 Embedding adapter 尚未在 P01 启用。",
            stage="provider.embedding",
            retryable=False,
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """默认只报告声明状态，不做付费探测。

        Args:
            network: 即使为真，P01 也禁止网络探测。

        Returns:
            UNKNOWN 且明确未检查网络。

        """
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.UNKNOWN,
            reason_code="P02_ADAPTER_NOT_INSTALLED",
        )


class LexicalOverlapReranker:
    """按 query/token 重叠率执行确定性离线重排。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.RERANKER,
        name="lexical-overlap",
        version="1",
        mode=ProviderMode.DETERMINISTIC,
        capabilities=ComponentCapabilities(supports_batch=True),
    )

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回离线批量能力。

        Args:
            无参数；读取当前 Provider。

        Returns:
            重排能力声明。

        """
        return self.descriptor.capabilities

    def rerank(self, request: RerankRequest) -> RerankResult:
        """按字符 token 集合重叠稳定重排。

        Args:
            request: 查询、候选和条数上限。

        Returns:
            分数降序且 ID 稳定打破并列的结果。

        """
        query_tokens = _tokens(request.query)
        scored = [
            RerankItem(
                candidate_id=candidate_id,
                score=float(len(query_tokens & _tokens(text))),
            )
            for candidate_id, text in request.candidates
        ]
        scored.sort(key=lambda item: (-item.score, item.candidate_id))
        return RerankResult(
            mode=RerankExecutionMode.LEXICAL_OVERLAP,
            items=tuple(scored[: request.limit]),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """返回无需网络的健康状态。

        Args:
            network: 被忽略；实现永不访问网络。

        Returns:
            HEALTHY 离线状态。

        """
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.HEALTHY,
            reason_code="LEXICAL_RERANKER_READY",
        )


class DeclaredRemoteReranker:
    """P02 前绝不发 HTTP 的 Jina reranker 声明。"""

    def __init__(self, *, model: str) -> None:
        """保存模型身份。

        Args:
            model: 固定 Provider 模型名。

        Returns:
            无返回值。

        """
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.RERANKER,
            name="jina-reranker",
            version=model,
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
            ),
        )

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回声明的远程能力。

        Args:
            无参数；读取当前 Provider。

        Returns:
            网络与批量能力。

        """
        return self.descriptor.capabilities

    def rerank(self, request: RerankRequest) -> RerankResult:
        """失败关闭，避免 P01 意外调用 Jina。

        Args:
            request: 未发送到网络的候选。

        Returns:
            此实现不会返回。

        Raises:
            ProviderUnavailable: 真实 HTTP adapter 属于 P02。

        """
        del request
        raise ProviderUnavailable(
            "真实 Jina Reranker adapter 尚未在 P01 启用。",
            stage="provider.reranker",
            retryable=False,
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """返回未联网的声明状态。

        Args:
            network: 即使为真，P01 也禁止网络探测。

        Returns:
            UNKNOWN 状态。

        """
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.UNKNOWN,
            reason_code="P02_ADAPTER_NOT_INSTALLED",
        )


class SingleSlotRouter:
    """只选择覆盖完整 primary 的离线 Router。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.EMBEDDING_ROUTER,
        name="embedding-router-single",
        version="1",
        mode=ProviderMode.LOCAL,
    )

    def route(self, request: EmbeddingRouteRequest) -> EmbeddingRouteDecision:
        """选择第一个覆盖完整的 slot。

        Args:
            request: revision 覆盖和出网策略视图。

        Returns:
            primary 或 DENSE_UNAVAILABLE 决策。

        """
        if request.coverages and request.coverages[0].ratio == 1.0:
            slot_id = request.coverages[0].slot_id
            return EmbeddingRouteDecision(
                selected_slot_id=slot_id,
                attempted_slot_ids=(slot_id,),
                reason_code="PRIMARY_SELECTED",
                dense_available=True,
                revision_coverages=((slot_id, 1.0),),
            )
        return EmbeddingRouteDecision(
            selected_slot_id=None,
            attempted_slot_ids=(),
            reason_code="DENSE_UNAVAILABLE",
            dense_available=False,
        )


class HotStandbyRouter:
    """保持 slot 隔离的 primary 优先 P01 Router 骨架。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.EMBEDDING_ROUTER,
        name="embedding-router-hot-standby",
        version="1",
        mode=ProviderMode.LOCAL,
    )

    def route(self, request: EmbeddingRouteRequest) -> EmbeddingRouteDecision:
        """只在两槽覆盖完整时允许备用候选。

        Args:
            request: revision 覆盖和独立出网授权。

        Returns:
            primary、standby 或词法降级决策。

        """
        coverage = {item.slot_id: item.ratio for item in request.coverages}
        if coverage.get("primary") == 1.0:
            return EmbeddingRouteDecision(
                selected_slot_id="primary",
                attempted_slot_ids=("primary",),
                reason_code="PRIMARY_SELECTED",
                dense_available=True,
                revision_coverages=tuple(sorted(coverage.items())),
            )
        allow_standby = (
            coverage.get("standby") == 1.0
            and request.egress_policy.allow_aliyun_embedding_failover
            and request.egress_policy.remote_query_embedding_aliyun
        )
        if allow_standby:
            return EmbeddingRouteDecision(
                selected_slot_id="standby",
                attempted_slot_ids=("primary", "standby"),
                reason_code="PRIMARY_TRANSIENT_FAILURE",
                dense_available=True,
                revision_coverages=tuple(sorted(coverage.items())),
            )
        return EmbeddingRouteDecision(
            selected_slot_id=None,
            attempted_slot_ids=("primary",),
            reason_code="DENSE_UNAVAILABLE",
            dense_available=False,
            revision_coverages=tuple(sorted(coverage.items())),
        )


class ExtractiveGenerator:
    """只从显式证据提取文本的离线 Generator。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.GENERATOR,
        name="extractive",
        version="1",
        mode=ProviderMode.DETERMINISTIC,
    )

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回无网络生成能力。

        Args:
            无参数；读取当前 Generator。

        Returns:
            离线能力声明。

        """
        return self.descriptor.capabilities

    def generate(self, request: GenerationRequest) -> AnswerDraft:
        """按证据顺序生成带显式引用的草稿。

        Args:
            request: 查询、证据和引用协议。

        Returns:
            未经过发布门的提取式草稿。

        """
        text = "\n".join(item.citation_text for item in request.evidence)
        if not text:
            text = "没有可用证据。"
        return AnswerDraft(
            text=text,
            cited_evidence_ids=tuple(
                item.evidence_id for item in request.evidence
            ),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """返回无需网络的健康状态。

        Args:
            network: 被忽略；实现永不访问网络。

        Returns:
            HEALTHY 离线状态。

        """
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.HEALTHY,
            reason_code="EXTRACTIVE_READY",
        )


def _deterministic_vector(text: str, dimension: int) -> tuple[float, ...]:
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(f"{counter}\x00{text}".encode()).digest()
        values.extend((byte - 127.5) / 127.5 for byte in digest)
        counter += 1
    selected = values[:dimension]
    norm = math.sqrt(sum(value * value for value in selected))
    return tuple(value / norm for value in selected)


def _tokens(value: str) -> set[str]:
    return {
        character.casefold() for character in value if not character.isspace()
    }
