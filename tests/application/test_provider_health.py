from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from rag_app.application.provider_health import (
    CircuitKey,
    LocalUsageBudget,
    ProviderCircuitBreaker,
    rerank_or_bypass,
)
from rag_app.core.errors import PolicyDenied, ProviderUnavailable
from rag_app.core.models import (
    CircuitState,
    ProviderFailureCategory,
    RerankRequest,
)
from rag_app.core.policies import CircuitBreakerPolicy


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _key() -> CircuitKey:
    return CircuitKey("jina", "embedding", "model")


def test_half_open_allows_one_concurrent_probe() -> None:
    clock = _Clock()
    breaker = ProviderCircuitBreaker(
        CircuitBreakerPolicy(
            failure_threshold=1,
            open_cooldown_seconds=1,
            recovery_success_threshold=3,
        ),
        clock=clock,
    )
    key = _key()
    assert breaker.allow_call(key)
    breaker.record_failure(key, ProviderFailureCategory.TRANSIENT)
    clock.value = 2.0

    barrier = threading.Barrier(3)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=2)
        allowed = breaker.allow_call(key)
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(results) == [False, True]
    assert breaker.snapshot(key).state is CircuitState.HALF_OPEN


def test_three_half_open_successes_restore_closed() -> None:
    clock = _Clock()
    breaker = ProviderCircuitBreaker(
        CircuitBreakerPolicy(
            failure_threshold=1,
            open_cooldown_seconds=1,
            recovery_success_threshold=3,
        ),
        clock=clock,
    )
    key = _key()
    assert breaker.allow_call(key)
    breaker.record_failure(key, ProviderFailureCategory.TRANSIENT)
    clock.value = 2.0
    for expected in (1, 2, 0):
        assert breaker.allow_call(key)
        breaker.record_success(key)
        snapshot = breaker.snapshot(key)
        assert snapshot.recovery_successes == expected
    assert breaker.snapshot(key).state is CircuitState.CLOSED


def test_contract_failure_quarantines_until_explicit_reset() -> None:
    breaker = ProviderCircuitBreaker()
    key = _key()
    assert breaker.allow_call(key)
    breaker.record_failure(key, ProviderFailureCategory.RESPONSE_CONTRACT)
    assert breaker.snapshot(key).state is CircuitState.QUARANTINED
    assert not breaker.allow_call(key)
    breaker.reset(key)
    assert breaker.allow_call(key)


def test_usage_budget_is_utc_daily_and_atomic() -> None:
    current = [datetime(2026, 9, 2, 23, 59, tzinfo=UTC)]
    budget = LocalUsageBudget(now=lambda: current[0])
    budget.reserve(
        "aliyun",
        "embedding",
        5,
        daily_request_limit=1,
        daily_estimated_token_limit=5,
    )
    with pytest.raises(PolicyDenied):
        budget.reserve(
            "aliyun",
            "embedding",
            1,
            daily_request_limit=1,
            daily_estimated_token_limit=5,
        )
    current[0] = datetime(2026, 9, 3, tzinfo=UTC)
    budget.reserve(
        "aliyun",
        "embedding",
        5,
        daily_request_limit=1,
        daily_estimated_token_limit=5,
    )


class _UnavailableReranker:
    def rerank(self, request: RerankRequest) -> object:
        del request
        raise ProviderUnavailable("down", stage="test.reranker")


def test_rerank_failure_bypasses_without_zero_scores_or_qwen() -> None:
    result = rerank_or_bypass(
        _UnavailableReranker(),  # type: ignore[arg-type]
        RerankRequest(
            query="query",
            candidates=(("a", "first"), ("b", "second")),
            limit=2,
        ),
    )
    assert result.mode.value == "bypass_keep_rrf"
    assert result.items == ()
    assert result.reason_code == "RERANK_BYPASSED_PROVIDER_UNAVAILABLE"
