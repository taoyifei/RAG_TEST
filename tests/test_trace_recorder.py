import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.tracing.exporter import TraceExporter
from rag_app.tracing.models import (
    DecisionCode,
    SpanKind,
    SpanRecord,
    SpanStatus,
    TraceDetail,
    TraceFinish,
    TraceIdentity,
    TraceMode,
    TraceRecord,
    TraceStatus,
)
from rag_app.tracing.recorder import (
    TraceRecorder,
    TraceRecorderConfig,
    TraceSpanSpec,
    TraceUnavailableError,
)
from rag_app.tracing.store import TraceNotFoundError, TraceStore


def _trace(trace_id: str, mode: TraceMode) -> TraceRecord:
    created_at = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    return TraceRecord(
        trace_id=trace_id,
        schema_version="1",
        mode=mode,
        created_at=created_at,
        finished_at=None,
        duration_ms=None,
        pipeline_fingerprint="sha256:" + "1" * 64,
        serving_fingerprint="sha256:" + "2" * 64,
        release_revision="release-1",
        active_collection="rag-active-v1",
        index_manifest_sha256="3" * 64,
        payload_schema_version=2,
        status=TraceStatus.RUNNING,
        refusal_code=None,
        error_code=None,
        feedback_useful=None,
        capture_complete=True,
        expires_at=created_at + timedelta(days=30),
    )


class _FailingExporter(TraceExporter):
    def export_trace(self, trace: TraceDetail) -> None:
        del trace
        raise RuntimeError("synthetic exporter failure")


def test_normal_capture_failure_is_audited_without_raising(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    store.close()
    failures: list[tuple[str, DecisionCode]] = []
    recorder = TraceRecorder(
        store,
        audit_failure=lambda trace_id, code: failures.append((trace_id, code)),
    )

    recorder.begin_trace(_trace("a" * 32, TraceMode.SAFE))
    recorder.flush()
    recorder.close()

    assert failures == [("a" * 32, DecisionCode.TRACE_CAPTURE_FAILED)]
    assert recorder.writer_alive is False


def test_full_capture_rejects_unavailable_store_before_query(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    store.close()
    recorder = TraceRecorder(store)

    with pytest.raises(TraceUnavailableError):
        recorder.require_full_capacity()

    recorder.close()


def test_exporter_failure_does_not_lose_persisted_trace(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    failures: list[tuple[str, DecisionCode]] = []
    recorder = TraceRecorder(
        store,
        exporter=_FailingExporter(),
        audit_failure=lambda trace_id, code: failures.append((trace_id, code)),
    )
    trace_id = "a" * 32
    trace = _trace(trace_id, TraceMode.DIAGNOSTIC)

    recorder.begin_trace(trace)
    recorder.finish_trace(
        trace_id,
        TraceFinish(
            status=TraceStatus.REFUSED,
            finished_at=trace.created_at + timedelta(milliseconds=3),
            refusal_code="NO_EVIDENCE",
        ),
    )
    recorder.flush()

    assert store.get_trace(trace_id).trace.status is TraceStatus.REFUSED
    assert failures == [(trace_id, DecisionCode.TRACE_EXPORT_FAILED)]
    recorder.close()
    recorder.close()


def test_diagnostic_keeps_candidate_scores_without_full_artifact(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
    trace_id = "d" * 32
    session = recorder.begin_query(
        trace_id,
        TraceMode.DIAGNOSTIC,
        datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
        TraceIdentity(
            pipeline_fingerprint="sha256:" + "1" * 64,
            serving_fingerprint="sha256:" + "2" * 64,
            release_revision="release-1",
            active_collection="rag-active-v1",
            index_manifest_sha256="3" * 64,
            payload_schema_version=2,
        ),
    )
    session.decision(
        stage="retrieve.q0:dense",
        chunk_id="chunk-1",
        selected=True,
        reason_code=DecisionCode.RETRIEVAL_OK,
        details={
            "rank": 1,
            "raw_score": 0.75,
            "rrf_contribution": 1 / 61,
        },
    )
    assert (
        session.artifact(
            "context",
            {"question": "must-not-persist"},
        )
        is None
    )
    session.finish(
        status=TraceStatus.REFUSED,
        reason_code=DecisionCode.REFUSED,
        refusal_code="NO_EVIDENCE",
    )
    recorder.flush()

    detail = store.get_trace(trace_id)

    assert detail.trace.mode is TraceMode.DIAGNOSTIC
    assert detail.artifacts == ()
    assert detail.candidate_decisions[0].details["raw_score"] == 0.75
    assert b"must-not-persist" not in store.export_trace(trace_id)
    recorder.close()


def test_buffered_trace_is_committed_once_with_original_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 FULL 请求结束前不可见，结束后由 writer 一次提交完整快照。"""
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    writes: list[tuple[str, str]] = []
    original_write = store.write_completed_trace

    def observe_write(*args: object, **kwargs: object) -> None:
        trace = args[0]
        assert isinstance(trace, TraceRecord)
        writes.append((trace.trace_id, threading.current_thread().name))
        original_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "write_completed_trace", observe_write)
    recorder = TraceRecorder(store)
    trace_id = "c" * 32
    session = recorder.begin_query(
        trace_id,
        TraceMode.DIAGNOSTIC,
        datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
        TraceIdentity(
            pipeline_fingerprint="sha256:" + "1" * 64,
            serving_fingerprint="sha256:" + "2" * 64,
            release_revision="release-1",
            active_collection="pending",
            index_manifest_sha256="3" * 64,
            payload_schema_version=2,
        ),
        buffer_writes=True,
        completed_elapsed_ms=37,
    )
    session.completed_span(
        TraceSpanSpec(
            name="retrieval.snapshot",
            kind=SpanKind.RETRIEVER,
            parent_span_id=session.root.span_id,
            reason_code=DecisionCode.RETRIEVAL_OK,
            duration_ms=5,
        )
    )
    session.decision(
        stage="retrieve.q0:fts",
        chunk_id="chunk-1",
        selected=True,
        reason_code=DecisionCode.RETRIEVAL_OK,
        details={"rank": 1},
    )
    session.update_identity(
        revision_id="irev-current",
        index_fingerprint="sha256:" + "4" * 64,
        active_collection="irev-current",
    )

    with pytest.raises(TraceNotFoundError):
        store.get_trace(trace_id)

    session.finish(
        status=TraceStatus.ANSWERED,
        reason_code=DecisionCode.ANSWERED,
    )
    recorder.flush()
    detail = store.get_trace(trace_id)

    assert writes == [(trace_id, "rag-trace-writer")]
    assert detail.trace.status is TraceStatus.ANSWERED
    assert detail.trace.duration_ms == 37
    assert detail.trace.revision_id == "irev-current"
    assert detail.trace.index_fingerprint == "sha256:" + "4" * 64
    assert [span.name for span in detail.spans] == [
        "rag.query",
        "retrieval.snapshot",
    ]
    assert len(detail.candidate_decisions) == 1
    recorder.close()


def test_prune_waits_until_query_window_and_idle_grace_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台清理不得与活跃查询重叠，并应在静默窗后恢复。"""
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    initial_prune_finished = threading.Event()
    recurring_prune_started = threading.Event()
    prune_count = 0
    original_prune = store.prune

    def observe_prune(*args: object, **kwargs: object) -> int:
        nonlocal prune_count
        result = original_prune(*args, **kwargs)  # type: ignore[arg-type]
        prune_count += 1
        if prune_count == 1:
            initial_prune_finished.set()
        else:
            recurring_prune_started.set()
        return result

    monkeypatch.setattr(store, "prune", observe_prune)
    recorder = TraceRecorder(
        store,
        config=TraceRecorderConfig(
            prune_interval_seconds=0.01,
            maintenance_idle_grace_seconds=0.05,
        ),
    )
    assert initial_prune_finished.wait(timeout=0.5)
    trace_id = "1" * 32
    recorder.begin_query_window(trace_id)

    assert recurring_prune_started.wait(timeout=0.12) is False

    recorder.end_query_window(trace_id)
    assert recurring_prune_started.wait(timeout=0.5) is True
    recorder.close()


def test_query_waits_for_prune_that_already_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理已经取得互斥权时，新查询必须等其完成后再进入。"""
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    prune_started = threading.Event()
    prune_finished = threading.Event()
    release_prune = threading.Event()
    original_prune = store.prune

    def blocking_prune(*args: object, **kwargs: object) -> int:
        prune_started.set()
        assert release_prune.wait(timeout=1)
        result = original_prune(*args, **kwargs)  # type: ignore[arg-type]
        prune_finished.set()
        return result

    monkeypatch.setattr(store, "prune", blocking_prune)
    recorder = TraceRecorder(
        store,
        config=TraceRecorderConfig(
            prune_interval_seconds=0.01,
            maintenance_idle_grace_seconds=0.01,
        ),
    )
    assert prune_started.wait(timeout=0.5)
    query_entered = threading.Event()
    trace_id = "2" * 32

    def begin_query() -> None:
        recorder.begin_query_window(trace_id)
        query_entered.set()

    query_thread = threading.Thread(target=begin_query)
    query_thread.start()
    assert query_entered.wait(timeout=0.05) is False

    release_prune.set()
    assert prune_finished.wait(timeout=0.5)
    assert query_entered.wait(timeout=0.5)
    recorder.end_query_window(trace_id)
    query_thread.join(timeout=0.5)
    assert query_thread.is_alive() is False
    recorder.close()


def test_writer_flush_restarts_maintenance_idle_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flush 返回后不得让已到期清理抢在下一波查询之前运行。"""
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    initial_prune_finished = threading.Event()
    recurring_prune_started = threading.Event()
    prune_count = 0
    original_prune = store.prune

    def observe_prune(*args: object, **kwargs: object) -> int:
        nonlocal prune_count
        result = original_prune(*args, **kwargs)  # type: ignore[arg-type]
        prune_count += 1
        if prune_count == 1:
            initial_prune_finished.set()
        else:
            recurring_prune_started.set()
        return result

    monkeypatch.setattr(store, "prune", observe_prune)
    recorder = TraceRecorder(
        store,
        config=TraceRecorderConfig(
            prune_interval_seconds=0.01,
            maintenance_idle_grace_seconds=0.08,
        ),
    )
    try:
        assert initial_prune_finished.wait(timeout=0.5)

        recorder.flush()
        # 只观察 flush 返回后的维护窗口；此前已经完成的周期不属于抢跑。
        recurring_prune_started.clear()
        assert recurring_prune_started.wait(timeout=0.03) is False
        trace_id = "3" * 32
        recorder.begin_query_window(trace_id)
        assert recurring_prune_started.wait(timeout=0.1) is False

        recorder.end_query_window(trace_id)
        assert recurring_prune_started.wait(timeout=0.5)
    finally:
        recorder.close()


def test_bounded_writer_queue_records_drop_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    original_create = store.create_trace
    writer_started = threading.Event()
    release_writer = threading.Event()

    def blocking_create(trace: TraceRecord) -> None:
        writer_started.set()
        release_writer.wait(timeout=2)
        original_create(trace)

    monkeypatch.setattr(store, "create_trace", blocking_create)
    failures: list[DecisionCode] = []
    recorder = TraceRecorder(
        store,
        audit_failure=lambda _trace_id, code: failures.append(code),
        config=TraceRecorderConfig(queue_size=1),
    )
    trace = _trace("e" * 32, TraceMode.SAFE)

    recorder.begin_trace(trace)
    assert writer_started.wait(timeout=1)
    span = SpanRecord(
        trace_id=trace.trace_id,
        span_id="1" * 16,
        parent_span_id=None,
        sequence=1,
        name="queue-test",
        kind=SpanKind.CHAIN,
        started_at=trace.created_at,
        finished_at=None,
        duration_ms=None,
        status=SpanStatus.RUNNING,
        reason_code=DecisionCode.STARTED,
        attributes={},
        input_artifact_id=None,
        output_artifact_id=None,
    )
    recorder.put_span(span)
    recorder.put_span(span)
    release_writer.set()
    recorder.flush()

    assert DecisionCode.TRACE_QUEUE_FULL in failures
    root = store.get_trace(trace.trace_id).trace
    assert root.capture_complete is False
    assert root.capture_incomplete_reason == DecisionCode.TRACE_QUEUE_FULL.value
    assert root.dropped_span_count == 1
    assert root.writer_queue_high_water == 1
    recorder.close()


def test_full_artifact_limit_fails_request_without_truncation(
    tmp_path: Path,
) -> None:
    store = TraceStore(
        tmp_path / "traces.sqlite3",
        artifact_limit_bytes=32,
    )
    store.initialize()
    failures: list[DecisionCode] = []
    recorder = TraceRecorder(
        store,
        audit_failure=lambda _trace_id, code: failures.append(code),
        config=TraceRecorderConfig(full_artifact_reservation_bytes=16),
    )
    trace_id = "f" * 32
    session = recorder.begin_query(
        trace_id,
        TraceMode.FULL,
        datetime.now(UTC),
        TraceIdentity(
            pipeline_fingerprint="sha256:" + "1" * 64,
            serving_fingerprint="sha256:" + "2" * 64,
            release_revision="release-1",
            active_collection="rag-active-v1",
            index_manifest_sha256="3" * 64,
            payload_schema_version=2,
        ),
    )

    with pytest.raises(TraceUnavailableError):
        session.artifact("oversized", {"content": "x" * 64})
    session.finish(
        status=TraceStatus.FAILED,
        reason_code=DecisionCode.ERROR,
        error_code="TRACE_ARTIFACT_LIMIT",
    )
    recorder.flush()

    detail = store.get_trace(trace_id)
    assert detail.trace.capture_complete is False
    assert detail.artifacts == ()
    assert failures == [DecisionCode.TRACE_ARTIFACT_LIMIT]
    recorder.close()
