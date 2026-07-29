from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import rag_app.tracing.recorder as recorder_module
import tests.test_query_trace_pipeline as synthetic_query
from rag_app.tracing.models import (
    SpanKind,
    SpanRecord,
    SpanStatus,
    TraceFinish,
    TraceMode,
    TraceRecord,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.recorder import (
    TraceRecorder,
    TraceRecorderConfig,
    TraceSession,
    TraceSpanFinish,
    TraceSpanHandle,
    TraceSpanSpec,
)
from rag_app.tracing.store import TraceStore


@dataclass(slots=True)
class _FakeClock:
    value: float = 10.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(slots=True)
class _RecorderProbe:
    spans: dict[str, SpanRecord] = field(default_factory=dict)
    trace_finish: TraceFinish | None = None

    def begin_trace(self, trace: TraceRecord) -> None:
        del trace

    def put_span(self, span: SpanRecord, *, strict: bool = False) -> None:
        del strict
        self.spans[span.span_id] = span

    def finish_trace(
        self,
        trace_id: str,
        finish: TraceFinish,
        *,
        strict: bool = False,
    ) -> None:
        del trace_id, strict
        self.trace_finish = finish


def _new_session(
    clock: _FakeClock,
) -> tuple[TraceSession, _RecorderProbe]:
    created_at = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    trace = TraceRecord(
        trace_id="a" * 32,
        schema_version="1",
        mode=TraceMode.SAFE,
        created_at=created_at,
        finished_at=None,
        duration_ms=None,
        pipeline_fingerprint="sha256:" + "1" * 64,
        serving_fingerprint="sha256:" + "2" * 64,
        release_revision="3" * 40,
        active_collection="rag-active-v1",
        index_manifest_sha256="4" * 64,
        payload_schema_version=2,
        status=TraceStatus.RUNNING,
        refusal_code=None,
        error_code=None,
        feedback_useful=None,
        capture_complete=True,
        expires_at=created_at + timedelta(days=30),
    )
    probe = _RecorderProbe()
    session = TraceSession(
        cast(TraceRecorder, probe),
        trace,
        clock=clock,
    )
    return session, probe


def _finish_ok(
    session: TraceSession,
    active: TraceSpanHandle,
) -> None:
    session.finish_span(
        active,
        TraceSpanFinish(
            status=SpanStatus.OK,
            reason_code=DecisionCode.ACCEPTED,
        ),
    )


def _finish_session(
    session: TraceSession,
    *,
    status: TraceStatus = TraceStatus.ANSWERED,
) -> None:
    session.finish(
        status=status,
        reason_code=(
            DecisionCode.ERROR
            if status is TraceStatus.FAILED
            else DecisionCode.ACCEPTED
        ),
        error_code="SYNTHETIC" if status is TraceStatus.FAILED else None,
    )


def _assert_complete_tree(
    spans: tuple[SpanRecord, ...] | list[SpanRecord],
) -> None:
    spans_by_id = {span.span_id: span for span in spans}
    for span in spans:
        assert span.finished_at is not None
        assert span.duration_ms is not None
        elapsed_ms = (
            span.finished_at - span.started_at
        ).total_seconds() * 1000
        assert 0 <= span.duration_ms - elapsed_ms <= 1
        if span.parent_span_id is None:
            continue
        parent = spans_by_id[span.parent_span_id]
        assert parent.finished_at is not None
        assert parent.started_at <= span.started_at
        assert span.finished_at <= parent.finished_at


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected_duration_ms"),
    ((0.0, 0), (0.0002, 1), (0.001, 1)),
)
def test_fake_monotonic_clock_quantizes_one_timeline(
    elapsed_seconds: float,
    expected_duration_ms: int,
) -> None:
    clock = _FakeClock()
    session, probe = _new_session(clock)
    child = session.start_span(
        "child",
        SpanKind.CHAIN,
        parent_span_id=session.root.span_id,
    )

    clock.advance(elapsed_seconds)
    _finish_ok(session, child)
    _finish_session(session)

    assert probe.spans[child.span_id].duration_ms == expected_duration_ms
    _assert_complete_tree(list(probe.spans.values()))


def test_wall_clock_jumps_are_not_read_after_session_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ForbiddenWallClock:
        @classmethod
        def now(cls, timezone: object) -> datetime:
            del timezone
            raise AssertionError("session 不得再次读取 wall clock")

    clock = _FakeClock()
    session, probe = _new_session(clock)
    monkeypatch.setattr(recorder_module, "datetime", _ForbiddenWallClock)
    parent = session.start_span(
        "parent",
        SpanKind.CHAIN,
        parent_span_id=session.root.span_id,
    )
    clock.advance(0.0003)
    _finish_ok(session, parent)
    _finish_session(session)

    _assert_complete_tree(list(probe.spans.values()))


def test_reported_duration_is_clamped_to_open_parent_interval() -> None:
    clock = _FakeClock()
    session, probe = _new_session(clock)
    parent = session.start_span(
        "parent",
        SpanKind.CHAIN,
        parent_span_id=session.root.span_id,
    )
    clock.advance(0.0002)

    child_id = session.completed_span(
        TraceSpanSpec(
            name="reported",
            kind=SpanKind.HTTP,
            parent_span_id=parent.span_id,
            reason_code=DecisionCode.ACCEPTED,
            duration_ms=25,
        )
    )
    _finish_ok(session, parent)
    _finish_session(session)

    child = probe.spans[child_id]
    assert child.duration_ms == 1
    assert child.attributes["reported_duration_ms"] == 25
    _assert_complete_tree(list(probe.spans.values()))


def test_multilevel_children_and_failure_finish_stay_nested() -> None:
    clock = _FakeClock()
    session, probe = _new_session(clock)
    parent = session.start_span(
        "parent",
        SpanKind.CHAIN,
        parent_span_id=session.root.span_id,
    )
    clock.advance(0.001)
    child = session.start_span(
        "child",
        SpanKind.RETRIEVER,
        parent_span_id=parent.span_id,
    )
    clock.advance(0.0002)
    session.completed_span(
        TraceSpanSpec(
            name="grandchild",
            kind=SpanKind.HTTP,
            parent_span_id=child.span_id,
            reason_code=DecisionCode.ACCEPTED,
            duration_ms=8,
        )
    )
    session.finish_span(
        child,
        TraceSpanFinish(
            status=SpanStatus.ERROR,
            reason_code=DecisionCode.ERROR,
        ),
    )
    _finish_ok(session, parent)
    _finish_session(session, status=TraceStatus.FAILED)

    assert probe.trace_finish is not None
    root = probe.spans[session.root.span_id]
    assert probe.trace_finish.finished_at >= root.finished_at
    _assert_complete_tree(list(probe.spans.values()))


def test_closed_parent_rejects_new_child() -> None:
    clock = _FakeClock()
    session, _ = _new_session(clock)
    parent = session.start_span(
        "parent",
        SpanKind.CHAIN,
        parent_span_id=session.root.span_id,
    )
    _finish_ok(session, parent)

    with pytest.raises(RuntimeError, match="已关闭"):
        session.start_span(
            "late-child",
            SpanKind.HTTP,
            parent_span_id=parent.span_id,
        )

    _finish_session(session)


def test_two_hundred_real_clock_queries_have_valid_tree_and_same_outcome(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(
        store,
        config=TraceRecorderConfig(
            queue_size=8192,
            wait_seconds=60,
        ),
    )
    traced = synthetic_query._service(
        tmp_path / "traced",
        recorder=recorder,
    )
    plain = synthetic_query._service(
        tmp_path / "plain",
        recorder=None,
    )
    now = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    plain_outcome = plain.ask(
        trace_id="f" * 32,
        conversation_id="plain",
        question="public synthetic question",
        now=now,
        emit=lambda _event: None,
    )
    trace_ids: list[str] = []
    try:
        for sequence in range(200):
            trace_id = f"{sequence:032x}"
            trace_ids.append(trace_id)
            outcome = traced.ask(
                trace_id=trace_id,
                conversation_id=f"conversation-{sequence}",
                question="public synthetic question",
                now=now,
                emit=lambda _event: None,
            )
            assert outcome.answer == plain_outcome.answer
            assert outcome.rewritten == plain_outcome.rewritten
            assert outcome.stage_count == plain_outcome.stage_count
            assert outcome.calls == plain_outcome.calls
        recorder.flush()
        for trace_id in trace_ids:
            detail = store.get_trace(trace_id)
            _assert_complete_tree(list(detail.spans))
    finally:
        recorder.close()


def test_original_safe_trace_scenario_is_stable_two_hundred_times(
    tmp_path: Path,
) -> None:
    for sequence in range(200):
        case_path = tmp_path / f"case-{sequence}"
        case_path.mkdir()
        synthetic_query.test_safe_trace_has_complete_tree_without_business_artifacts(
            case_path
        )
