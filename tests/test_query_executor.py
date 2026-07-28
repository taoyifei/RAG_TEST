from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest

from rag_app.query_executor import (
    QueryCapacityError,
    QueryExecutor,
    QueryExecutorClosedError,
    QueryQueueTimeoutError,
)


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等待执行器状态超时。")
        time.sleep(0.005)


def test_fifth_query_waits_while_only_four_queries_run() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    release = threading.Event()
    completed = 0
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal active, completed, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        release.wait()
        with lock:
            active -= 1
            completed += 1

    with ThreadPoolExecutor(max_workers=5) as callers:
        first_four = tuple(
            callers.submit(executor.submit, work) for _ in range(4)
        )
        for admitted in first_four:
            admitted.result(timeout=1)
        fifth = callers.submit(executor.submit, work)
        _wait_for(lambda: executor.in_flight == 5)

        assert active == 4
        assert not fifth.done()

        release.set()
        fifth.result(timeout=1)
        _wait_for(lambda: completed == 5)

    executor.close()

    assert maximum_active == 4
    assert executor.max_workers == 4
    assert executor.max_queue == 8


def test_twelve_queries_are_accepted_and_thirteenth_is_immediate_429() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    release = threading.Event()

    def work() -> None:
        release.wait()

    with ThreadPoolExecutor(max_workers=12) as callers:
        admissions = tuple(
            callers.submit(executor.submit, work) for _ in range(12)
        )
        _wait_for(lambda: executor.in_flight == 12)

        started = time.monotonic()
        with pytest.raises(QueryCapacityError):
            executor.submit(lambda: None)
        elapsed = time.monotonic() - started

        assert elapsed < 0.1
        assert executor.retry_after_seconds == 5

        release.set()
        for admitted in admissions:
            admitted.result(timeout=1)

    _wait_for(lambda: executor.in_flight == 0)
    executor.close()


def test_queue_timeout_cancels_work_and_restores_capacity() -> None:
    executor = QueryExecutor(queue_wait_seconds=0.05)
    release = threading.Event()
    queued_ran = threading.Event()

    def work() -> None:
        release.wait()

    with ThreadPoolExecutor(max_workers=4) as callers:
        admissions = tuple(
            callers.submit(executor.submit, work) for _ in range(4)
        )
        for admitted in admissions:
            admitted.result(timeout=1)

        with pytest.raises(QueryQueueTimeoutError):
            executor.submit(queued_ran.set)

        assert executor.in_flight == 4
        release.set()
        for admitted in admissions:
            admitted.result(timeout=1)

    _wait_for(lambda: executor.in_flight == 0)
    assert not queued_ran.is_set()

    executor.submit(queued_ran.set)
    _wait_for(queued_ran.is_set)
    executor.close()


def test_query_exception_restores_capacity() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)

    def fail() -> None:
        raise RuntimeError("synthetic query failure")

    executor.submit(fail)
    _wait_for(lambda: executor.in_flight == 0)

    recovered = threading.Event()
    executor.submit(recovered.set)
    _wait_for(recovered.is_set)
    executor.close()


def test_close_waits_for_active_query_and_rejects_new_work() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    release = threading.Event()

    def work() -> None:
        release.wait()

    executor.submit(work)

    with ThreadPoolExecutor(max_workers=1) as caller:
        closing = caller.submit(executor.close)
        time.sleep(0.05)
        assert not closing.done()
        release.set()
        closing.result(timeout=1)

    executor.close()
    with pytest.raises(QueryExecutorClosedError):
        executor.submit(lambda: None)


def test_worker_threads_are_fixed_and_joined_on_close() -> None:
    baseline = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("rag-query-worker")
    }
    executor = QueryExecutor(queue_wait_seconds=1.0)
    release = threading.Event()

    def work() -> None:
        release.wait()

    with ThreadPoolExecutor(max_workers=4) as callers:
        admissions = tuple(
            callers.submit(executor.submit, work) for _ in range(4)
        )
        for admitted in admissions:
            admitted.result(timeout=1)
        current = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("rag-query-worker")
        }
        assert len(current - baseline) == 4
        release.set()

    executor.close()
    _wait_for(
        lambda: not {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("rag-query-worker")
        }
        - baseline
    )
