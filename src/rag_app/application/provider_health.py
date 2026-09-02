"""Provider circuit、出网授权、本地预算与重排降级。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from rag_app.core.errors import (
    PolicyDenied,
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)
from rag_app.core.models import (
    CircuitState,
    ProviderFailureCategory,
    RerankExecutionMode,
    RerankRequest,
    RerankResult,
)
from rag_app.core.policies import CircuitBreakerPolicy, EgressPolicy
from rag_app.core.ports import RerankerPort


@dataclass(frozen=True, slots=True)
class CircuitKey:
    """Provider、操作与模型组成的 circuit 唯一键。"""

    provider_id: str
    operation: str
    model: str


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    """不含 secret 的 circuit 状态快照。"""

    key: CircuitKey
    state: CircuitState
    consecutive_failures: int
    recovery_successes: int
    reason_code: str


@dataclass(slots=True)
class _CircuitRecord:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    recovery_successes: int = 0
    open_until: float = 0.0
    half_open_lease: bool = False
    reason_code: str = "READY"


class ProviderCircuitBreaker:
    """并发安全且不主动消费 Token 的进程内 circuit。"""

    def __init__(
        self,
        policy: CircuitBreakerPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """保存策略并创建空状态表。

        Args:
            policy: 阈值、冷却和恢复成功次数。
            clock: 可注入的单调时钟。

        Returns:
            无返回值。

        """
        self._policy = policy or CircuitBreakerPolicy()
        self._clock = clock
        self._records: dict[CircuitKey, _CircuitRecord] = {}
        self._lock = threading.Lock()

    def allow_call(self, key: CircuitKey) -> bool:
        """原子判断调用并为 HALF_OPEN 获取唯一 lease。

        Args:
            key: Provider、操作与模型键。

        Returns:
            当前请求允许探测或正常调用时为 ``True``。

        """
        with self._lock:
            record = self._records.setdefault(key, _CircuitRecord())
            if record.state is CircuitState.QUARANTINED:
                return False
            if record.state is CircuitState.OPEN:
                if self._clock() < record.open_until:
                    return False
                record.state = CircuitState.HALF_OPEN
                record.recovery_successes = 0
                record.reason_code = "HALF_OPEN_PROBE"
            if record.state is CircuitState.HALF_OPEN:
                if record.half_open_lease:
                    return False
                record.half_open_lease = True
            return True

    def record_failure(
        self, key: CircuitKey, category: ProviderFailureCategory
    ) -> None:
        """按分类打开或隔离 circuit 并释放 HALF_OPEN lease。

        Args:
            key: Provider circuit 键。
            category: Router 分类后的失败类别。

        Returns:
            无返回值。

        """
        with self._lock:
            record = self._records.setdefault(key, _CircuitRecord())
            record.half_open_lease = False
            record.recovery_successes = 0
            record.reason_code = category.value.upper()
            if category is ProviderFailureCategory.RESPONSE_CONTRACT:
                record.state = CircuitState.QUARANTINED
                return
            record.consecutive_failures += 1
            threshold_reached = (
                record.consecutive_failures >= self._policy.failure_threshold
                or record.state is CircuitState.HALF_OPEN
                or category is ProviderFailureCategory.AUTH_OR_MODEL
            )
            if threshold_reached:
                record.state = CircuitState.OPEN
                record.open_until = (
                    self._clock() + self._policy.open_cooldown_seconds
                )

    def record_success(self, key: CircuitKey) -> None:
        """记录成功并在连续恢复阈值后关闭 circuit。

        Args:
            key: Provider circuit 键。

        Returns:
            无返回值。

        """
        with self._lock:
            record = self._records.setdefault(key, _CircuitRecord())
            record.half_open_lease = False
            if record.state is CircuitState.HALF_OPEN:
                record.recovery_successes += 1
                record.reason_code = "RECOVERY_PROBE_OK"
                if (
                    record.recovery_successes
                    < self._policy.recovery_success_threshold
                ):
                    return
            record.state = CircuitState.CLOSED
            record.consecutive_failures = 0
            record.recovery_successes = 0
            record.open_until = 0.0
            record.reason_code = "READY"

    def snapshot(self, key: CircuitKey) -> CircuitSnapshot:
        """返回单个 circuit 的一致快照。

        Args:
            key: Provider circuit 键。

        Returns:
            不含 secret 的状态与计数。

        """
        with self._lock:
            record = self._records.setdefault(key, _CircuitRecord())
            return CircuitSnapshot(
                key=key,
                state=record.state,
                consecutive_failures=record.consecutive_failures,
                recovery_successes=record.recovery_successes,
                reason_code=record.reason_code,
            )

    def reset(self, key: CircuitKey) -> None:
        """在显式 health check 或配置变化后重置状态。

        Args:
            key: 需要重置的 Provider circuit 键。

        Returns:
            无返回值。

        """
        with self._lock:
            self._records[key] = _CircuitRecord(reason_code="EXPLICIT_RESET")


@dataclass(slots=True)
class _UsageRecord:
    day: date
    requests: int = 0
    estimated_tokens: int = 0


class LocalUsageBudget:
    """按 UTC 日窗口限制本应用的远程调用。"""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """创建空预算计数器。

        Args:
            now: 返回带时区时间的可测试时钟。

        Returns:
            无返回值。

        """
        self._now = now
        self._records: dict[tuple[str, str], _UsageRecord] = {}
        self._lock = threading.Lock()

    def reserve(
        self,
        provider_id: str,
        operation: str,
        estimated_tokens: int,
        *,
        daily_request_limit: int,
        daily_estimated_token_limit: int,
    ) -> None:
        """原子预留一次请求和估算 Token。

        Args:
            provider_id: Provider ID。
            operation: 操作名。
            estimated_tokens: 本次保守估算量。
            daily_request_limit: UTC 日请求上限。
            daily_estimated_token_limit: UTC 日估算 Token 上限。

        Returns:
            无返回值。

        Raises:
            PolicyDenied: 限制非正或预留将超过预算。

        """
        if (
            estimated_tokens < 0
            or daily_request_limit <= 0
            or daily_estimated_token_limit <= 0
        ):
            raise PolicyDenied(
                "本地 Provider 预算未配置正数限制。",
                stage="provider.budget",
            )
        today = self._now().astimezone(UTC).date()
        key = (provider_id, operation)
        with self._lock:
            record = self._records.get(key)
            if record is None or record.day != today:
                record = _UsageRecord(day=today)
                self._records[key] = record
            if (
                record.requests + 1 > daily_request_limit
                or record.estimated_tokens + estimated_tokens
                > daily_estimated_token_limit
            ):
                raise PolicyDenied(
                    "本地 Provider 日预算已耗尽。",
                    stage="provider.budget",
                    details={
                        "provider_id": provider_id,
                        "operation": operation,
                    },
                )
            record.requests += 1
            record.estimated_tokens += estimated_tokens


class EgressGuard:
    """在读取或发送正文前执行目的地级授权。"""

    @staticmethod
    def require_query_embedding(
        policy: EgressPolicy, provider_id: str
    ) -> None:
        """检查 query embedding 总授权和厂商授权。

        Args:
            policy: 默认拒绝的项目策略。
            provider_id: ``jina`` 或 ``aliyun-qwen37``。

        Returns:
            无返回值。

        Raises:
            PolicyDenied: 任一必需授权缺失。

        """
        allowed = policy.remote_query_embedding
        if provider_id == "jina":
            allowed = allowed and policy.remote_query_embedding_jina
        elif provider_id == "aliyun-qwen37":
            allowed = (
                allowed
                and policy.remote_query_embedding_aliyun
                and policy.allow_aliyun_embedding_failover
            )
        else:
            allowed = False
        if not allowed:
            raise PolicyDenied(
                "Query Embedding 出网未授权。",
                stage="provider.egress",
                details={"provider_id": provider_id},
            )

    @staticmethod
    def require_document_embedding(
        policy: EgressPolicy, provider_id: str
    ) -> None:
        """检查 document embedding 总授权和厂商授权。

        Args:
            policy: 默认拒绝的项目策略。
            provider_id: ``jina`` 或 ``aliyun-qwen37``。

        Returns:
            无返回值。

        Raises:
            PolicyDenied: 任一必需授权缺失。

        """
        allowed = policy.remote_document_embedding
        if provider_id == "jina":
            allowed = allowed and policy.remote_document_embedding_jina
        elif provider_id == "aliyun-qwen37":
            allowed = allowed and policy.remote_document_embedding_aliyun
        else:
            allowed = False
        if not allowed:
            raise PolicyDenied(
                "Document Embedding 出网未授权。",
                stage="provider.egress",
                details={"provider_id": provider_id},
            )

    @staticmethod
    def require_reranking(policy: EgressPolicy) -> None:
        """检查 Jina Reranker 的双重授权。

        Args:
            policy: 默认拒绝的项目策略。

        Returns:
            无返回值。

        Raises:
            PolicyDenied: 总授权或 Jina 授权缺失。

        """
        if not (policy.remote_reranking and policy.remote_reranking_jina):
            raise PolicyDenied(
                "Jina Reranker 出网未授权。",
                stage="provider.egress",
            )


def rerank_or_bypass(
    reranker: RerankerPort, request: RerankRequest
) -> RerankResult:
    """显式保留 Provider 不可用时的应用层 RRF 顺序。

    Args:
        reranker: Jina 或其它真实 Reranker。
        request: 已按 RRF 排序的候选集合。

    Returns:
        Provider 结果，或不补零且 items 为空的显式旁路结果。

    """
    try:
        return reranker.rerank(request)
    except (
        ProviderAuthenticationError,
        ProviderInvalidResponse,
        ProviderRateLimited,
        ProviderUnavailable,
    ):
        return RerankResult(
            mode=RerankExecutionMode.BYPASS_KEEP_RRF,
            items=(),
            reason_code="RERANK_BYPASSED_PROVIDER_UNAVAILABLE",
        )


__all__ = [
    "CircuitKey",
    "CircuitSnapshot",
    "EgressGuard",
    "LocalUsageBudget",
    "ProviderCircuitBreaker",
    "rerank_or_bypass",
]
