"""control/local 索引任务租约 heartbeat 测试。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.state import JobKind, StateStore
from rag_app.state.lease import LeaseHeartbeat, LeaseLostError

_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64
_THREAD_PREFIX = "rag-lease-heartbeat-"


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _claimed_job(store: StateStore, worker_id: str = "worker-a") -> str:
    job = store.create_job(
        idempotency_key=f"heartbeat:{worker_id}",
        kind=JobKind.INCREMENTAL,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    claimed = store.claim_next_job(
        worker_id=worker_id,
        now=datetime.now(UTC),
        lease_seconds=1,
    )
    assert claimed is not None
    assert claimed.job_id == job.job_id
    return job.job_id


def _heartbeat_threads() -> set[int | None]:
    return {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith(_THREAD_PREFIX)
    }


def test_heartbeat_renews_past_lease_then_stops_for_reclaim(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job_id = _claimed_job(store)
    before_threads = _heartbeat_threads()
    heartbeat = LeaseHeartbeat.for_job(
        store=store,
        job_id=job_id,
        worker_id="worker-a",
        lease_seconds=1,
    )
    assert 0.1 <= heartbeat.interval_seconds <= 1 / 3

    with heartbeat:
        assert heartbeat.thread_is_daemon is False
        assert threading.Event().wait(1.2) is False
        assert (
            store.claim_next_job(
                worker_id="worker-b",
                now=datetime.now(UTC),
                lease_seconds=1,
            )
            is None
        )

    heartbeat.close()
    reclaimed = store.claim_next_job(
        worker_id="worker-b",
        now=datetime.now(UTC) + timedelta(seconds=2),
        lease_seconds=1,
    )
    assert reclaimed is not None
    assert reclaimed.job_id == job_id
    assert reclaimed.lease_owner == "worker-b"
    assert _heartbeat_threads() == before_threads


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("test-only database failure"),
        LookupError("test-only owner replacement"),
    ],
)
def test_thread_failure_is_visible_without_leaking_thread(
    error: Exception,
) -> None:
    attempted = threading.Event()
    calls = 0

    def fail_after_initial_renewal(now: datetime) -> None:
        nonlocal calls
        assert now.tzinfo is UTC
        calls += 1
        if calls > 1:
            attempted.set()
            raise error

    before_threads = _heartbeat_threads()
    heartbeat = LeaseHeartbeat(
        renew=fail_after_initial_renewal,
        lease_seconds=1,
    )

    with pytest.raises(LeaseLostError, match="LEASE_LOST"), heartbeat:
        assert attempted.wait(2)
        heartbeat.raise_if_failed()

    heartbeat.close()
    assert _heartbeat_threads() == before_threads


def test_interval_has_fixed_upper_bound() -> None:
    heartbeat = LeaseHeartbeat(
        renew=lambda _: None,
        lease_seconds=300,
    )

    assert heartbeat.interval_seconds == 30


def test_actual_owner_replacement_is_reported(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    job_id = _claimed_job(store)
    heartbeat = LeaseHeartbeat.for_job(
        store=store,
        job_id=job_id,
        worker_id="worker-a",
        lease_seconds=1,
    )

    with pytest.raises(LeaseLostError, match="LEASE_LOST"), heartbeat:
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "UPDATE jobs SET lease_owner = ? WHERE job_id = ?",
                ("worker-b", job_id),
            )
        assert threading.Event().wait(0.6) is False
        heartbeat.raise_if_failed()
