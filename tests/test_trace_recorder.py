import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.tracing.exporter import TraceExporter
from rag_app.tracing.models import (
    DecisionCode,
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
    TraceUnavailableError,
)
from rag_app.tracing.store import TraceStore


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
    assert session.artifact(
        "context",
        {"question": "must-not-persist"},
    ) is None
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


def test_bounded_writer_queue_audits_full_without_raising(
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
    recorder.mark_capture_incomplete(trace.trace_id)
    recorder.mark_capture_incomplete(trace.trace_id)
    release_writer.set()
    recorder.flush()

    assert DecisionCode.TRACE_QUEUE_FULL in failures
    recorder.close()


def test_full_artifact_limit_marks_incomplete_without_failing_query(
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

    assert session.artifact("oversized", {"content": "x" * 64}) is None
    session.finish(
        status=TraceStatus.REFUSED,
        reason_code=DecisionCode.REFUSED,
        refusal_code="NO_EVIDENCE",
    )
    recorder.flush()

    detail = store.get_trace(trace_id)
    assert detail.trace.capture_complete is False
    assert detail.artifacts == ()
    assert failures == [DecisionCode.TRACE_ARTIFACT_LIMIT]
    recorder.close()
