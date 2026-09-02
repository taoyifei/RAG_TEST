"""双槽文档调度与请求内粘性的 Embedding 自动切换。"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass

from rag_app.application.provider_health import (
    CircuitKey,
    CircuitSnapshot,
    EgressGuard,
    LocalUsageBudget,
    ProviderCircuitBreaker,
)
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    DenseUnavailable,
    IndexCompatibilityError,
    PolicyDenied,
    ProviderAuthenticationError,
    ProviderInputTooLarge,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    RagError,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    EmbeddingCoverage,
    EmbeddingRequest,
    EmbeddingRequestRole,
    EmbeddingResult,
    EmbeddingSlotIdentity,
    EmbeddingTopology,
    ProviderCall,
    ProviderFailureCategory,
)
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import EmbeddingPort
from rag_app.core.tokenization import estimate_tokens


@dataclass(frozen=True, slots=True)
class QueryEmbeddingRequest:
    """一次只允许选择一个 Dense slot 的查询。"""

    text: str

    def __post_init__(self) -> None:
        """拒绝空白 query。"""
        if not self.text.strip():
            raise ValueError("Query Embedding 文本不能为空。")


@dataclass(frozen=True, slots=True)
class ActiveRevisionEmbeddingState:
    """Router 所需的 active revision 双槽证据。"""

    topology: EmbeddingTopology
    coverages: tuple[EmbeddingCoverage, ...]


@dataclass(frozen=True, slots=True)
class RoutedEmbeddingResult:
    """绑定单一 slot/vector name 的查询向量。"""

    vector: tuple[float, ...]
    selected_slot_id: str
    vector_name: str
    attempted_slot_ids: tuple[str, ...]
    fallback_reason: str
    provider_calls: tuple[ProviderCall, ...]
    circuit_before: tuple[CircuitSnapshot, ...]
    circuit_after: tuple[CircuitSnapshot, ...]


@dataclass(frozen=True, slots=True)
class DocumentEmbeddingInput:
    """P02 Fake E2E 使用的稳定 Chunk 身份和 embedding_text。"""

    chunk_id: str
    text: str


@dataclass(frozen=True, slots=True)
class SlotEmbeddingOutcome:
    """一个 slot 独立成功或失败的文档向量结果。"""

    slot_id: str
    chunk_ids: tuple[str, ...]
    vectors: tuple[tuple[float, ...], ...]
    calls: tuple[ProviderCall, ...]
    retryable: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class SlotEmbeddingBatchResult:
    """不混合 slot 的双 Provider 文档调度结果。"""

    outcomes: tuple[SlotEmbeddingOutcome, ...]

    def outcome(self, slot_id: str) -> SlotEmbeddingOutcome:
        """按 slot ID 返回独立结果。

        Args:
            slot_id: 目标 slot。

        Returns:
            匹配的结果。

        Raises:
            KeyError: slot 不在结果中。

        """
        for outcome in self.outcomes:
            if outcome.slot_id == slot_id:
                return outcome
        raise KeyError(slot_id)


class QueryEmbeddingRouter:
    """Jina primary 优先且失败时只切换到 Qwen standby。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.EMBEDDING_ROUTER,
        name="query-embedding-router-hot-standby",
        version="1",
        mode=ProviderMode.LOCAL,
    )

    def __init__(
        self,
        primary: EmbeddingPort,
        standby: EmbeddingPort,
        *,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        usage_budget: LocalUsageBudget | None = None,
    ) -> None:
        """保存双 Provider 和进程内健康状态。

        Args:
            primary: Jina Embedding 端口。
            standby: 阿里 Qwen3.7 Embedding 端口。
            circuit_breaker: 可注入时钟的 circuit。
            usage_budget: UTC 日预算计数器。

        Returns:
            无返回值。

        """
        self._primary = primary
        self._standby = standby
        self._circuit = circuit_breaker or ProviderCircuitBreaker()
        self._budget = usage_budget or LocalUsageBudget()

    def embed_query_with_failover(
        self,
        request: QueryEmbeddingRequest,
        revision: ActiveRevisionEmbeddingState,
        egress: EgressPolicy,
    ) -> RoutedEmbeddingResult:
        """调用 primary，必要时在同一请求内粘性切到 standby。

        Args:
            request: 单条 query 文本。
            revision: active revision 的 topology 与覆盖证据。
            egress: 请求作用域默认拒绝策略。

        Returns:
            单一 slot/vector name 的查询向量。

        Raises:
            IndexCompatibilityError: slot/schema/revision 不匹配。
            PolicyDenied: 出网或预算拒绝，禁止备用掩盖。
            ProviderInputTooLarge: 调用方输入无效，禁止切换。
            DenseUnavailable: 允许切换的失败耗尽两个 slot。

        """
        primary_slot, standby_slot = _slots(revision.topology)
        _require_complete_coverage(primary_slot, revision.coverages)
        EgressGuard.require_query_embedding(egress, "jina")
        primary_key = _circuit_key(primary_slot)
        standby_key = _circuit_key(standby_slot)
        before = (
            self._circuit.snapshot(primary_key),
            self._circuit.snapshot(standby_key),
        )
        attempted: list[str] = []
        calls: list[ProviderCall] = []
        if self._circuit.allow_call(primary_key):
            attempted.append(primary_slot.slot_id)
            try:
                result = self._embed_slot(
                    self._primary, primary_slot, request.text
                )
            except RagError as error:
                calls.extend(_error_calls(error))
                category = failure_category(error)
                self._circuit.record_failure(primary_key, category)
                if category in {
                    ProviderFailureCategory.INPUT_INVALID,
                    ProviderFailureCategory.POLICY_DENIED,
                    ProviderFailureCategory.STORE_INCOMPATIBLE,
                }:
                    raise
                fallback_reason = _fallback_reason(category)
            else:
                self._circuit.record_success(primary_key)
                calls.extend(result.calls)
                return self._routed(
                    result,
                    primary_slot,
                    tuple(attempted),
                    "PRIMARY_SELECTED",
                    tuple(calls),
                    before,
                    primary_key,
                    standby_key,
                )
        else:
            fallback_reason = "PRIMARY_CIRCUIT_OPEN"
        _require_complete_coverage(standby_slot, revision.coverages)
        EgressGuard.require_query_embedding(egress, "aliyun-qwen37")
        if not self._circuit.allow_call(standby_key):
            raise _dense_unavailable(
                tuple(attempted), tuple(calls), "STANDBY_CIRCUIT_OPEN"
            )
        estimated_tokens = estimate_tokens(request.text)
        self._budget.reserve(
            "aliyun-qwen37",
            "embedding",
            estimated_tokens,
            daily_request_limit=egress.aliyun_daily_request_budget,
            daily_estimated_token_limit=egress.aliyun_daily_token_budget,
        )
        attempted.append(standby_slot.slot_id)
        try:
            result = self._embed_slot(self._standby, standby_slot, request.text)
        except RagError as error:
            calls.extend(_error_calls(error))
            self._circuit.record_failure(
                standby_key, failure_category(error)
            )
            raise _dense_unavailable(
                tuple(attempted), tuple(calls), "BOTH_UNAVAILABLE"
            ) from None
        self._circuit.record_success(standby_key)
        calls.extend(result.calls)
        return self._routed(
            result,
            standby_slot,
            tuple(attempted),
            fallback_reason,
            tuple(calls),
            before,
            primary_key,
            standby_key,
        )

    def _embed_slot(
        self,
        provider: EmbeddingPort,
        slot: EmbeddingSlotIdentity,
        text: str,
    ) -> EmbeddingResult:
        result = provider.embed(
            EmbeddingRequest(
                slot_id=slot.slot_id,
                role=EmbeddingRequestRole.QUERY,
                texts=(text,),
            )
        )
        if (
            result.slot_id != slot.slot_id
            or result.observed_dimension != slot.dimension
            or len(result.vectors) != 1
            or result.request_policy_identity
            != _role_policy_identity(slot, EmbeddingRequestRole.QUERY)
        ):
            raise IndexCompatibilityError(
                "Provider 结果与选定 slot 身份不匹配。",
                stage="embedding_router.result",
                details={"slot_id": slot.slot_id},
            )
        return result

    def _routed(  # noqa: PLR0913, PLR0917
        self,
        result: EmbeddingResult,
        slot: EmbeddingSlotIdentity,
        attempted: tuple[str, ...],
        reason: str,
        calls: tuple[ProviderCall, ...],
        before: tuple[CircuitSnapshot, ...],
        primary_key: CircuitKey,
        standby_key: CircuitKey,
    ) -> RoutedEmbeddingResult:
        return RoutedEmbeddingResult(
            vector=result.vectors[0],
            selected_slot_id=slot.slot_id,
            vector_name=slot.vector_name,
            attempted_slot_ids=attempted,
            fallback_reason=reason,
            provider_calls=calls,
            circuit_before=before,
            circuit_after=(
                self._circuit.snapshot(primary_key),
                self._circuit.snapshot(standby_key),
            ),
        )


# P02-P05 类名兼容；新代码使用能表达真实调用职责的名称。
EmbeddingFailoverRouter = QueryEmbeddingRouter


class DualEmbeddingCoordinator:
    """顺序执行两个独立 slot 的文档 Embedding 工作流。"""

    def __init__(
        self,
        providers: Mapping[str, EmbeddingPort],
        *,
        cache: dict[str, tuple[float, ...]] | None = None,
    ) -> None:
        """保存显式 slot→Provider 映射和可选进程内缓存。

        Args:
            providers: 每个 required slot 的独立 Provider。
            cache: 由完整安全 key 隔离的向量缓存。

        Returns:
            无返回值。

        """
        self._providers = dict(providers)
        self._cache = cache if cache is not None else {}

    def embed_documents_for_slots(
        self,
        chunks: tuple[DocumentEmbeddingInput, ...],
        topology: EmbeddingTopology,
    ) -> SlotEmbeddingBatchResult:
        """为每个 slot 独立生成文档向量且不激活 revision。

        Args:
            chunks: 稳定 chunk ID 与 embedding_text。
            topology: required slot 和向量空间身份。

        Returns:
            每个 slot 独立成功、失败和可重试状态。

        """
        if not chunks or any(not chunk.text.strip() for chunk in chunks):
            raise ValueError("双槽文档 Embedding 输入不能为空。")
        outcomes: list[SlotEmbeddingOutcome] = []
        for slot in topology.slots:
            provider = self._providers.get(slot.slot_id)
            if provider is None:
                outcomes.append(
                    SlotEmbeddingOutcome(
                        slot_id=slot.slot_id,
                        chunk_ids=(),
                        vectors=(),
                        calls=(),
                        retryable=False,
                        reason_code="PROVIDER_NOT_CONFIGURED",
                    )
                )
                continue
            keys = tuple(
                embedding_cache_key(
                    slot,
                    EmbeddingRequestRole.DOCUMENT,
                    chunk.text,
                )
                for chunk in chunks
            )
            cached = tuple(self._cache.get(key) for key in keys)
            if all(vector is not None for vector in cached):
                outcomes.append(
                    SlotEmbeddingOutcome(
                        slot_id=slot.slot_id,
                        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
                        vectors=tuple(
                            vector for vector in cached if vector is not None
                        ),
                        calls=(),
                        retryable=False,
                        reason_code="CACHE_HIT",
                    )
                )
                continue
            missing_positions = tuple(
                index
                for index, vector in enumerate(cached)
                if vector is None
            )
            try:
                result = provider.embed(
                    EmbeddingRequest(
                        slot_id=slot.slot_id,
                        role=EmbeddingRequestRole.DOCUMENT,
                        texts=tuple(
                            chunks[index].text for index in missing_positions
                        ),
                    )
                )
            except RagError as error:
                outcomes.append(
                    SlotEmbeddingOutcome(
                        slot_id=slot.slot_id,
                        chunk_ids=(),
                        vectors=(),
                        calls=_error_calls(error),
                        retryable=error.retryable,
                        reason_code=error.code,
                    )
                )
                continue
            if not _document_result_matches_slot(
                result, slot, expected_count=len(missing_positions)
            ):
                outcomes.append(
                    SlotEmbeddingOutcome(
                        slot_id=slot.slot_id,
                        chunk_ids=(),
                        vectors=(),
                        calls=result.calls,
                        retryable=False,
                        reason_code="PROVIDER_RESULT_INCOMPATIBLE",
                    )
                )
                continue
            for position, vector in zip(
                missing_positions,
                result.vectors,
                strict=True,
            ):
                self._cache[keys[position]] = vector
            merged_vectors = tuple(self._cache[key] for key in keys)
            outcomes.append(
                SlotEmbeddingOutcome(
                    slot_id=slot.slot_id,
                    chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
                    vectors=merged_vectors,
                    calls=result.calls,
                    retryable=False,
                    reason_code=(
                        "PARTIAL_CACHE_FILLED"
                        if len(missing_positions) < len(chunks)
                        else "COMPLETE"
                    ),
                )
            )
        return SlotEmbeddingBatchResult(outcomes=tuple(outcomes))


def validate_required_slot_coverage(
    result: SlotEmbeddingBatchResult,
    topology: EmbeddingTopology,
    *,
    chunk_count: int,
) -> bool:
    """模拟 ``all_required_slots_complete`` 激活门。

    Args:
        result: 双槽调度结果。
        topology: required slot 身份。
        chunk_count: revision chunk 总数。

    Returns:
        所有 slot 数量、维度、有限性均完整时为 ``True``。

    """
    if chunk_count <= 0:
        return False
    for slot in topology.slots:
        outcome = result.outcome(slot.slot_id)
        if outcome.reason_code not in {
            "COMPLETE",
            "CACHE_HIT",
            "PARTIAL_CACHE_FILLED",
        }:
            return False
        if len(outcome.vectors) != chunk_count:
            return False
        if any(
            len(vector) != slot.dimension
            or any(not math.isfinite(value) for value in vector)
            or not any(value != 0.0 for value in vector)
            for vector in outcome.vectors
        ):
            return False
    return True


def _document_result_matches_slot(
    result: EmbeddingResult,
    slot: EmbeddingSlotIdentity,
    *,
    expected_count: int,
) -> bool:
    return (
        result.slot_id == slot.slot_id
        and result.role is EmbeddingRequestRole.DOCUMENT
        and result.observed_dimension == slot.dimension
        and len(result.vectors) == expected_count
        and result.request_policy_identity
        == _role_policy_identity(slot, EmbeddingRequestRole.DOCUMENT)
    )


def _role_policy_identity(
    slot: EmbeddingSlotIdentity,
    role: EmbeddingRequestRole,
) -> str:
    policy = (
        slot.document_request_policy
        if role is EmbeddingRequestRole.DOCUMENT
        else slot.query_request_policy
    )
    return canonical_sha256(policy)


def embedding_cache_key(
    slot: EmbeddingSlotIdentity,
    role: EmbeddingRequestRole,
    text: str,
) -> str:
    """生成含 slot、Provider、角色和正文摘要的缓存键。

    Args:
        slot: 不可混用的向量空间身份。
        role: document 或 query。
        text: 只进入 SHA-256 的原文。

    Returns:
        规范化 SHA-256 缓存键。

    """
    return canonical_sha256(
        {
            "slot_id": slot.slot_id,
            "provider": slot.provider_id,
            "model": slot.model,
            "role": role.value,
            "dimension": slot.dimension,
            "normalization": slot.normalization,
            "request_policy_identity": canonical_sha256(
                slot.document_request_policy
                if role is EmbeddingRequestRole.DOCUMENT
                else slot.query_request_policy
            ),
            "adapter_revision": slot.adapter_revision,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    )


def search_cache_key(  # noqa: PLR0913
    *,
    project_id: str,
    knowledge_base_id: str,
    active_index_revision_id: str,
    serving_fingerprint: str,
    selected_embedding_slot: str,
    rerank_mode: str,
    query: str,
    metadata_filters: object = (),
    access_filters: object = (),
    conversation_identity: str | None = None,
    rewrite_identity: str | None = None,
) -> str:
    """生成绑定实际 slot 和重排模式的搜索缓存键。

    Args:
        project_id: 当前项目边界。
        knowledge_base_id: 当前知识库边界。
        active_index_revision_id: 当前 active revision。
        serving_fingerprint: 当前 serving 指纹。
        selected_embedding_slot: 请求实际选择的 slot。
        rerank_mode: Provider 或显式旁路模式。
        query: 只进入 SHA-256 的查询原文。
        metadata_filters: 已规范化的元数据过滤条件。
        access_filters: 已规范化的访问控制过滤条件。
        conversation_identity: 适用时的会话身份。
        rewrite_identity: 适用时的 query rewrite 身份。

    Returns:
        规范化 SHA-256 缓存键。

    """
    return canonical_sha256(
        {
            "project_id": project_id,
            "knowledge_base_id": knowledge_base_id,
            "active_index_revision_id": active_index_revision_id,
            "serving_fingerprint": serving_fingerprint,
            "selected_embedding_slot": selected_embedding_slot,
            "rerank_mode": rerank_mode,
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "metadata_filters": metadata_filters,
            "access_filters": access_filters,
            "conversation_identity": conversation_identity,
            "rewrite_identity": rewrite_identity,
        }
    )


def failure_category(error: RagError) -> ProviderFailureCategory:
    """把稳定 Core 错误映射为 Router 失败类别。

    Args:
        error: Provider 或策略错误。

    Returns:
        Circuit 与 failover 使用的稳定类别。

    """
    mappings = (
        (
            (ProviderRateLimited, ProviderUnavailable),
            ProviderFailureCategory.TRANSIENT,
        ),
        ((ProviderInvalidResponse,), ProviderFailureCategory.RESPONSE_CONTRACT),
        ((ProviderAuthenticationError,), ProviderFailureCategory.AUTH_OR_MODEL),
        ((ProviderInputTooLarge,), ProviderFailureCategory.INPUT_INVALID),
        ((PolicyDenied,), ProviderFailureCategory.POLICY_DENIED),
        (
            (IndexCompatibilityError,),
            ProviderFailureCategory.STORE_INCOMPATIBLE,
        ),
    )
    for error_types, category in mappings:
        if isinstance(error, error_types):
            return category
    return ProviderFailureCategory.INPUT_INVALID


def _slots(
    topology: EmbeddingTopology,
) -> tuple[EmbeddingSlotIdentity, EmbeddingSlotIdentity]:
    if topology.mode != "hot_standby" or topology.standby_slot_id is None:
        raise IndexCompatibilityError(
            "自动切换要求 hot_standby topology。",
            stage="embedding_router.revision",
        )
    return (
        topology.slot(topology.primary_slot_id),
        topology.slot(topology.standby_slot_id),
    )


def _require_complete_coverage(
    slot: EmbeddingSlotIdentity,
    coverages: tuple[EmbeddingCoverage, ...],
) -> None:
    matches = tuple(item for item in coverages if item.slot_id == slot.slot_id)
    if len(matches) != 1:
        raise IndexCompatibilityError(
            "active revision 缺少唯一 slot coverage。",
            stage="embedding_router.revision",
            details={"slot_id": slot.slot_id},
        )
    coverage = matches[0]
    if (
        coverage.vector_name != slot.vector_name
        or coverage.observed_dimension != slot.dimension
    ):
        raise IndexCompatibilityError(
            "coverage 与 slot/vector schema 不匹配。",
            stage="embedding_router.revision",
            details={"slot_id": slot.slot_id},
        )
    if coverage.ratio != 1.0:
        raise DenseUnavailable(
            "active revision 的目标 slot 覆盖不完整。",
            stage="embedding_router.revision",
            details={"slot_id": slot.slot_id},
        )


def _circuit_key(slot: EmbeddingSlotIdentity) -> CircuitKey:
    return CircuitKey(slot.provider_id, "embedding", slot.model)


def _fallback_reason(category: ProviderFailureCategory) -> str:
    return {
        ProviderFailureCategory.TRANSIENT: "PRIMARY_TRANSIENT_FAILURE",
        ProviderFailureCategory.RESPONSE_CONTRACT: "PRIMARY_RESPONSE_CONTRACT",
        ProviderFailureCategory.AUTH_OR_MODEL: "PRIMARY_AUTH_OR_MODEL",
    }.get(category, "PRIMARY_UNAVAILABLE")


def _error_calls(error: RagError) -> tuple[ProviderCall, ...]:
    call = getattr(error, "provider_call", None)
    return (call,) if isinstance(call, ProviderCall) else ()


def _dense_unavailable(
    attempted: tuple[str, ...],
    calls: tuple[ProviderCall, ...],
    reason_code: str,
) -> DenseUnavailable:
    error = DenseUnavailable(
        "Dense Provider 均不可用；检索层应继续 Exact 与 FTS5。",
        stage="embedding_router.failover",
        details={
            "attempted_slots": list(attempted),
            "reason_code": reason_code,
        },
    )
    error.provider_calls = calls
    return error


__all__ = [
    "ActiveRevisionEmbeddingState",
    "DocumentEmbeddingInput",
    "DualEmbeddingCoordinator",
    "EmbeddingFailoverRouter",
    "QueryEmbeddingRequest",
    "QueryEmbeddingRouter",
    "RoutedEmbeddingResult",
    "SlotEmbeddingBatchResult",
    "SlotEmbeddingOutcome",
    "embedding_cache_key",
    "failure_category",
    "search_cache_key",
    "validate_required_slot_coverage",
]
