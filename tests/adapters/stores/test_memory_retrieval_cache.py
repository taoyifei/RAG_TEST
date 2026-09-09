"""进程内最终结果缓存的容量、TTL 与并发关闭回归。"""

from __future__ import annotations

from threading import Event, Thread

import pytest

from rag_app.adapters.stores import memory_retrieval_cache
from rag_app.adapters.stores.memory_retrieval_cache import (
    InMemoryRetrievalCache,
)
from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    QueryKind,
    SearchAnswerResult,
)


def _result(identity: int, *, answer: str = "合成回答") -> SearchAnswerResult:
    cache_key = f"sha256:{identity:064x}"
    return SearchAnswerResult(
        trace_id=f"trace_{identity:032x}",
        status=ConfidenceStatus.ANSWERABLE,
        reason_code="ANSWERABLE",
        answer=answer,
        confidence=ConfidenceDecision(
            status=ConfidenceStatus.ANSWERABLE,
            score=1.0,
        ),
        query_kind=QueryKind.SIMPLE_FACT,
        active_index_revision_id=f"irev_{identity:032x}",
        index_fingerprint=f"sha256:{identity:064x}",
        serving_fingerprint=f"sha256:{identity + 1:064x}",
        route_reason_code="LEXICAL_ONLY",
        rerank_execution_mode="bypass",
        generation_mode="extractive",
        cache_key=cache_key,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_entries": 0}, "max_entries"),
        ({"max_approx_bytes": 0}, "max_approx_bytes"),
        ({"prune_every_operations": 0}, "prune_every_operations"),
        ({"prune_batch_size": 0}, "prune_batch_size"),
    ],
)
def test_cache_rejects_non_positive_bounds(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        InMemoryRetrievalCache(**kwargs)


def test_cache_evicts_least_recently_used_entry() -> None:
    cache = InMemoryRetrievalCache(
        max_entries=2,
        max_approx_bytes=None,
        prune_every_operations=100,
    )
    first = _result(1)
    second = _result(2)
    third = _result(3)

    cache.put(first.cache_key, first)
    cache.put(second.cache_key, second)
    assert cache.get(first.cache_key) is first
    cache.put(third.cache_key, third)

    assert cache.get(second.cache_key) is None
    assert cache.get(first.cache_key) is first
    assert cache.get(third.cache_key) is third
    metrics = cache.metrics()
    assert metrics.entries == 2
    assert metrics.evictions == 1
    assert metrics.hits == 3
    assert metrics.misses == 1


def test_cache_byte_limit_counts_serialized_result_body() -> None:
    small = _result(1, answer="短回答")
    large = _result(2, answer="证据正文" * 2_000)
    probe = InMemoryRetrievalCache(max_approx_bytes=None)
    probe.put(large.cache_key, large)
    large_bytes = probe.metrics().approx_bytes

    cache = InMemoryRetrievalCache(
        max_entries=10,
        max_approx_bytes=large_bytes,
        prune_every_operations=100,
    )
    cache.put(small.cache_key, small)
    cache.put(large.cache_key, large)

    metrics = cache.metrics()
    assert metrics.entries == 1
    assert metrics.approx_bytes == large_bytes
    assert metrics.evictions == 1
    assert cache.get(small.cache_key) is None
    assert cache.get(large.cache_key) is large


def test_cache_prune_obeys_batch_limit_and_ttl_order() -> None:
    clock = {"now": 0.0}
    cache = InMemoryRetrievalCache(
        max_entries=10,
        max_approx_bytes=None,
        prune_every_operations=100,
        prune_batch_size=2,
        clock=lambda: clock["now"],
    )
    for identity in range(1, 4):
        result = _result(identity)
        cache.put(result.cache_key, result, ttl_seconds=1)
    clock["now"] = 2.0

    assert cache.prune() == 2
    assert cache.metrics().entries == 1
    assert cache.prune() == 1
    metrics = cache.metrics()
    assert metrics.entries == 0
    assert metrics.approx_bytes == 0
    assert metrics.expired == 3


def test_cache_low_frequency_prune_runs_during_put() -> None:
    clock = {"now": 0.0}
    cache = InMemoryRetrievalCache(
        max_entries=10,
        max_approx_bytes=None,
        prune_every_operations=2,
        prune_batch_size=1,
        clock=lambda: clock["now"],
    )
    expired = _result(1)
    current = _result(2)
    cache.put(expired.cache_key, expired, ttl_seconds=1)
    clock["now"] = 2.0

    cache.put(current.cache_key, current, ttl_seconds=10)

    metrics = cache.metrics()
    assert metrics.entries == 1
    assert metrics.expired == 1
    assert cache.get(current.cache_key) is current


def test_close_wins_race_with_in_progress_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = InMemoryRetrievalCache()
    result = _result(1)
    estimating = Event()
    resume = Event()
    failures: list[BaseException] = []

    def _blocked_estimate(cache_key: str, candidate: SearchAnswerResult) -> int:
        del cache_key, candidate
        estimating.set()
        assert resume.wait(timeout=2)
        return 1

    def _put() -> None:
        try:
            cache.put(result.cache_key, result)
        except BaseException as error:  # 测试线程需把异常传回主线程。
            failures.append(error)

    monkeypatch.setattr(
        memory_retrieval_cache,
        "_entry_approx_bytes",
        _blocked_estimate,
    )
    thread = Thread(target=_put)
    thread.start()
    assert estimating.wait(timeout=2)
    cache.close()
    resume.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert cache.metrics().entries == 0
    with pytest.raises(RuntimeError, match="已关闭"):
        cache.get(result.cache_key)
    with pytest.raises(RuntimeError, match="已关闭"):
        cache.put(result.cache_key, result)
