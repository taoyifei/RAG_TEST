"""Product singleflight 等价键、并发、取消与独立 Trace 回归。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import cast

import pytest

from rag_app.application.retrieval import RetrievalService
from rag_app.application.retrieval.service import RetrievalExecutionIdentity
from rag_app.clients.resilience import StreamCancellation
from rag_app.composition.product_runtime import (
    ProductProfileResolver,
    _RetrievalLeaseProxy,
)
from rag_app.core.errors import QueryCancelled
from rag_app.core.models import (
    BaseResultCacheKey,
    ConfidenceDecision,
    ConfidenceStatus,
    KnowledgeBaseScope,
    ProviderCallCount,
    QueryKind,
    RetrievalDiagnostics,
    RetrievalDiagnosticsSummary,
    SearchAnswerResult,
    SearchRequest,
    StageTiming,
)
from rag_app.core.ports import CancellationPort
from rag_app.product.singleflight import ProductQuerySingleflight

_PROJECT_ID = f"prj_{'1' * 32}"
_KNOWLEDGE_BASE_ID = f"kb_{'2' * 32}"
_REVISION_ID = f"irev_{'3' * 32}"
_INDEX_FINGERPRINT = f"sha256:{'4' * 64}"
_SERVING_FINGERPRINT = f"sha256:{'5' * 64}"
_KEY_HASH = f"sha256:{'6' * 64}"


def _result(trace_id: str) -> SearchAnswerResult:
    diagnostics = RetrievalDiagnostics(
        provider_calls=(
            ProviderCallCount(
                operation="generation",
                call_count=1,
                retry_count=0,
            ),
        ),
        stage_timings=(StageTiming(stage="generation", elapsed_ms=3.0),),
    )
    return SearchAnswerResult(
        trace_id=trace_id,
        status=ConfidenceStatus.ANSWERABLE,
        reason_code="ANSWERABLE",
        answer="合成回答",
        confidence=ConfidenceDecision(
            status=ConfidenceStatus.ANSWERABLE,
            score=1.0,
        ),
        query_kind=QueryKind.SIMPLE_FACT,
        active_index_revision_id=_REVISION_ID,
        index_fingerprint=_INDEX_FINGERPRINT,
        serving_fingerprint=_SERVING_FINGERPRINT,
        route_reason_code="LEXICAL_ONLY",
        rerank_execution_mode="bypass",
        generation_mode="llm",
        cache_key=_KEY_HASH,
        generation_called_this_request=True,
        diagnostics=diagnostics,
        diagnostics_summary=RetrievalDiagnosticsSummary(
            channel_count=1,
            fused_count=1,
            reranked_count=1,
            evidence_count=1,
            provider_call_count=1,
            provider_retry_count=0,
            cache_hit=False,
        ),
    )


@pytest.mark.parametrize("concurrency", [2, 8, 32])
def test_same_key_concurrency_runs_one_bottom_computation(
    concurrency: int,
) -> None:
    coordinator = ProductQuerySingleflight()
    start = threading.Barrier(concurrency + 1)
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def invoke(index: int) -> SearchAnswerResult:
        trace_id = f"trace_{index + 1:032x}"
        cancellation = StreamCancellation()
        start.wait(timeout=5)

        def compute(_group: CancellationPort) -> SearchAnswerResult:
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return _result(trace_id)

        return coordinator.execute(
            _KEY_HASH,
            request_trace_id=trace_id,
            cancellation=cancellation,
            compute=compute,
        )

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(invoke, index) for index in range(concurrency)
        ]
        start.wait(timeout=5)
        assert entered.wait(timeout=5)
        _wait_for_followers(coordinator, concurrency - 1)
        release.set()
        results = tuple(future.result(timeout=5) for future in futures)

    assert calls == 1
    assert len({result.trace_id for result in results}) == concurrency
    assert sum(result.singleflight_role == "leader" for result in results) == 1
    assert (
        sum(result.singleflight_role == "follower" for result in results)
        == concurrency - 1
    )
    for result in results:
        if result.singleflight_role == "leader":
            assert result.generation_called_this_request
            continue
        assert result.result_origin == "singleflight"
        assert not result.generation_called_this_request
        assert result.diagnostics_summary is not None
        assert result.diagnostics_summary.provider_call_count == 0
        assert result.diagnostics is not None
        assert result.diagnostics.stage_timings[0].stage == "singleflight_wait"


def test_equivalence_key_separates_answer_affecting_identity() -> None:
    base = BaseResultCacheKey(
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
        active_revision_id=_REVISION_ID,
        index_fingerprint=_INDEX_FINGERPRINT,
        serving_fingerprint=_SERVING_FINGERPRINT,
        retrieval_profile_identity="rpr-active-a",
        query_sha256="7" * 64,
        owner_identity_hash="8" * 64,
        metadata_filter_hash=f"sha256:{'9' * 64}",
        access_filter_hash=f"sha256:{'a' * 64}",
        conversation_identity=f"sha256:{'b' * 64}",
        rewrite_policy_identity=f"sha256:{'c' * 64}",
        query_semantics_identity=f"sha256:{'d' * 64}",
        cache_schema=1,
    )
    variants = (
        {"owner_identity_hash": "e" * 64},
        {"knowledge_base_id": f"kb_{'f' * 32}"},
        {"active_revision_id": f"irev_{'0' * 32}"},
        {"serving_fingerprint": f"sha256:{'e' * 64}"},
        {"retrieval_profile_identity": "rpr-active-b"},
        {"conversation_identity": f"sha256:{'1' * 64}"},
        {"access_filter_hash": f"sha256:{'2' * 64}"},
        {"limit": 11},
        {"generation_behavior": "grounded"},
    )

    assert (
        len(
            {
                base.persistent_key,
                *(
                    base.model_copy(update=update).persistent_key
                    for update in variants
                ),
            }
        )
        == len(variants) + 1
    )


def test_follower_cancel_does_not_cancel_active_leader() -> None:
    coordinator = ProductQuerySingleflight()
    leader_cancel = StreamCancellation()
    follower_cancel = StreamCancellation()
    entered = threading.Event()
    release = threading.Event()
    upstream_closed = threading.Event()

    def compute(group: CancellationPort) -> SearchAnswerResult:
        registration = group.register(upstream_closed.set)
        entered.set()
        try:
            assert release.wait(timeout=5)
            return _result(f"trace_{'1' * 32}")
        finally:
            group.unregister(registration)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(
            coordinator.execute,
            _KEY_HASH,
            request_trace_id=f"trace_{'1' * 32}",
            cancellation=leader_cancel,
            compute=compute,
        )
        assert entered.wait(timeout=5)
        follower = executor.submit(
            coordinator.execute,
            _KEY_HASH,
            request_trace_id=f"trace_{'2' * 32}",
            cancellation=follower_cancel,
            compute=compute,
        )
        _wait_for_followers(coordinator, 1)
        follower_cancel.cancel()
        with pytest.raises(QueryCancelled):
            follower.result(timeout=5)
        assert not upstream_closed.is_set()
        release.set()
        assert leader.result(timeout=5).singleflight_role == "leader"


def test_cancelled_leader_keeps_computing_for_follower() -> None:
    coordinator = ProductQuerySingleflight()
    leader_cancel = StreamCancellation()
    follower_cancel = StreamCancellation()
    entered = threading.Event()
    release = threading.Event()
    upstream_closed = threading.Event()

    def compute(group: CancellationPort) -> SearchAnswerResult:
        registration = group.register(upstream_closed.set)
        entered.set()
        try:
            assert release.wait(timeout=5)
            return _result(f"trace_{'3' * 32}")
        finally:
            group.unregister(registration)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(
            coordinator.execute,
            _KEY_HASH,
            request_trace_id=f"trace_{'3' * 32}",
            cancellation=leader_cancel,
            compute=compute,
        )
        assert entered.wait(timeout=5)
        follower = executor.submit(
            coordinator.execute,
            _KEY_HASH,
            request_trace_id=f"trace_{'4' * 32}",
            cancellation=follower_cancel,
            compute=compute,
        )
        _wait_for_followers(coordinator, 1)
        leader_cancel.cancel()
        assert not upstream_closed.is_set()
        release.set()
        with pytest.raises(QueryCancelled):
            leader.result(timeout=5)
        shared = follower.result(timeout=5)

    assert shared.singleflight_role == "follower"
    assert shared.trace_id == f"trace_{'4' * 32}"


def test_all_subscribers_cancel_closes_upstream_once() -> None:
    coordinator = ProductQuerySingleflight()
    leader_cancel = StreamCancellation()
    follower_cancel = StreamCancellation()
    entered = threading.Event()
    upstream_closed = threading.Event()
    close_calls = 0
    close_lock = threading.Lock()

    def close_upstream() -> None:
        nonlocal close_calls
        with close_lock:
            close_calls += 1
        upstream_closed.set()

    def compute(group: CancellationPort) -> SearchAnswerResult:
        registration = group.register(close_upstream)
        entered.set()
        try:
            assert upstream_closed.wait(timeout=5)
            if group.is_cancelled():
                raise QueryCancelled("QUERY_CANCELLED")
            raise AssertionError("全部订阅者取消后聚合令牌必须取消。")
        finally:
            group.unregister(registration)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(
            coordinator.execute,
            _KEY_HASH,
            request_trace_id=f"trace_{'5' * 32}",
            cancellation=leader_cancel,
            compute=compute,
        )
        assert entered.wait(timeout=5)
        follower = executor.submit(
            coordinator.execute,
            _KEY_HASH,
            request_trace_id=f"trace_{'6' * 32}",
            cancellation=follower_cancel,
            compute=compute,
        )
        _wait_for_followers(coordinator, 1)
        leader_cancel.cancel()
        follower_cancel.cancel()
        with pytest.raises(QueryCancelled):
            leader.result(timeout=5)
        with pytest.raises(QueryCancelled):
            follower.result(timeout=5)

    assert close_calls == 1
    assert coordinator.metrics().upstream_cancellations == 1


class _ProxyService:
    """为组合代理验证底层调用次数与每请求观测。"""

    def __init__(
        self,
        *,
        key_hash: str = _KEY_HASH,
        serving_fingerprint: str = _SERVING_FINGERPRINT,
    ) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.validated: list[str] = []
        self.observed: list[str] = []
        self.key_hash = key_hash
        self.serving_fingerprint = serving_fingerprint
        self._lock = threading.Lock()

    def execution_identity(
        self, request: SearchRequest
    ) -> RetrievalExecutionIdentity:
        del request
        return RetrievalExecutionIdentity(
            key_hash=self.key_hash,
            active_revision_id=_REVISION_ID,
            serving_fingerprint=self.serving_fingerprint,
        )

    def search_and_answer(
        self,
        request: SearchRequest,
        **kwargs: object,
    ) -> SearchAnswerResult:
        del kwargs
        with self._lock:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5)
        assert request.trace_id is not None
        return _result(request.trace_id)

    def validate_shared_result(
        self,
        result: SearchAnswerResult,
        request: SearchRequest,
        identity: RetrievalExecutionIdentity,
    ) -> None:
        del request, identity
        self.validated.append(result.trace_id)

    def record_singleflight_observation(
        self,
        result: SearchAnswerResult,
        identity: RetrievalExecutionIdentity,
    ) -> None:
        del identity
        self.observed.append(result.trace_id)


class _ProxyResolver:
    """只暴露代理需要的 lease 与 singleflight。"""

    def __init__(self, service: _ProxyService) -> None:
        self.singleflight = ProductQuerySingleflight()
        self.service = service

    @contextmanager
    def retrieval_service_lease(
        self,
        knowledge_base_id: str,
        fallback: RetrievalService,
    ) -> object:
        del knowledge_base_id, fallback
        yield cast(RetrievalService, self.service)


def test_retrieval_proxy_coalesces_non_stream_and_preserves_trace_ids() -> None:
    service = _ProxyService()
    resolver = _ProxyResolver(service)
    proxy = _RetrievalLeaseProxy(
        cast(ProductProfileResolver, resolver),
        _KNOWLEDGE_BASE_ID,
        cast(RetrievalService, object()),
    )
    scope = KnowledgeBaseScope(
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
    )

    def invoke(index: int) -> SearchAnswerResult:
        return proxy.search_and_answer(
            SearchRequest(
                scope=scope,
                text="相同问题",
                owner_identity="owner-a",
                trace_id=f"trace_{index + 10:032x}",
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(invoke, index) for index in range(8)]
        assert service.entered.wait(timeout=5)
        _wait_for_followers(resolver.singleflight, 7)
        service.release.set()
        results = tuple(future.result(timeout=5) for future in futures)

    assert service.calls == 1
    assert len({result.trace_id for result in results}) == 8
    assert len(service.validated) == 7
    assert sorted(service.observed) == sorted(
        result.trace_id for result in results
    )


def test_retrieval_proxy_does_not_join_retired_resource_generation() -> None:
    """旧代际 leader 在途时，新代际相同请求必须独立执行。"""
    service_a = _ProxyService(
        key_hash=f"sha256:{'a' * 64}",
        serving_fingerprint=f"sha256:{'1' * 64}",
    )
    service_b = _ProxyService(
        key_hash=f"sha256:{'b' * 64}",
        serving_fingerprint=f"sha256:{'2' * 64}",
    )
    service_b.release.set()
    resolver = _ProxyResolver(service_a)
    proxy = _RetrievalLeaseProxy(
        cast(ProductProfileResolver, resolver),
        _KNOWLEDGE_BASE_ID,
        cast(RetrievalService, object()),
    )
    scope = KnowledgeBaseScope(
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
    )

    def invoke(trace_suffix: int) -> SearchAnswerResult:
        return proxy.search_and_answer(
            SearchRequest(
                scope=scope,
                text="轮换期间的相同问题",
                owner_identity="owner-a",
                trace_id=f"trace_{trace_suffix:032x}",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        old_generation = executor.submit(invoke, 100)
        assert service_a.entered.wait(timeout=5)
        resolver.service = service_b
        new_generation = executor.submit(invoke, 101)
        try:
            assert (
                new_generation.result(timeout=2).singleflight_role == "leader"
            )
        finally:
            service_a.release.set()
        assert old_generation.result(timeout=5).singleflight_role == "leader"

    assert service_a.calls == 1
    assert service_b.calls == 1
    assert resolver.singleflight.metrics().followers == 0


def _wait_for_followers(
    coordinator: ProductQuerySingleflight,
    expected: int,
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if coordinator.metrics().followers >= expected:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"singleflight follower 未达到预期：{coordinator.metrics()}"
    )
