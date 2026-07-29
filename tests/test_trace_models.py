from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from rag_app.tracing.models import (
    CandidateDecision,
    DecisionCode,
    SpanKind,
    SpanRecord,
    SpanStatus,
    TraceMode,
    TraceRecord,
    TraceStatus,
)


def _trace() -> TraceRecord:
    created_at = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    return TraceRecord(
        trace_id="a" * 32,
        schema_version="1",
        mode=TraceMode.FULL,
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
        expires_at=created_at + timedelta(hours=72),
    )


def test_trace_contract_validates_ids_times_and_terminal_state() -> None:
    trace = _trace()

    assert trace.trace_id == "a" * 32
    assert trace.mode is TraceMode.FULL
    assert trace.status is TraceStatus.RUNNING

    with pytest.raises(ValueError, match="trace_id"):
        replace(trace, trace_id="NOT-HEX")


def test_span_contract_requires_parent_and_exact_duration() -> None:
    started_at = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    span = SpanRecord(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id="c" * 16,
        sequence=3,
        name="embedding.query",
        kind=SpanKind.EMBEDDING,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=15),
        duration_ms=15,
        status=SpanStatus.OK,
        reason_code=DecisionCode.RETRIEVAL_OK,
        attributes={"query_variant_index": 0, "returned_count": 3},
        input_artifact_id=None,
        output_artifact_id=None,
    )

    assert span.duration_ms == 15
    assert span.parent_span_id == "c" * 16

    with pytest.raises(ValueError, match="duration_ms"):
        replace(span, duration_ms=14)


def test_candidate_decision_uses_stable_code_not_free_text() -> None:
    decision = CandidateDecision(
        trace_id="a" * 32,
        sequence=1,
        stage="evidence.assemble",
        chunk_id="chunk-1",
        selected=False,
        reason_code=DecisionCode.TOKEN_BUDGET,
        details={"estimated_tokens": 21, "actual_tokens": 19},
    )

    assert decision.reason_code is DecisionCode.TOKEN_BUDGET
    assert decision.details["actual_tokens"] == 19
