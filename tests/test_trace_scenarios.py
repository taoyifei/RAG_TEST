from datetime import UTC, datetime
from pathlib import Path

from rag_app.tracing.models import (
    SpanKind,
    TraceIdentity,
    TraceMode,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.recorder import (
    TraceRecorder,
    TraceSpanSpec,
)
from rag_app.tracing.store import TraceStore


def _identity() -> TraceIdentity:
    return TraceIdentity(
        pipeline_fingerprint="sha256:" + "1" * 64,
        serving_fingerprint="sha256:" + "2" * 64,
        release_revision="synthetic-v1",
        active_collection="rag-synthetic-v1",
        index_manifest_sha256="3" * 64,
        payload_schema_version=2,
    )


def test_four_public_synthetic_trace_scenarios_round_trip(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    recorder = TraceRecorder(store)
    now = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)

    answered = recorder.begin_query(
        "1" * 32,
        TraceMode.FULL,
        now,
        _identity(),
    )
    for stage in ("rrf.fuse", "rerank", "evidence.assemble", "citation"):
        answered.decision(
            stage=stage,
            chunk_id="chunk-public-answer",
            selected=True,
            reason_code=DecisionCode.SELECTED,
            details={"rank": 1},
        )
    answered.artifact(
        "final",
        {"status": "answered", "claim_count": 1},
    )
    answered.finish(
        status=TraceStatus.ANSWERED,
        reason_code=DecisionCode.ANSWERED,
    )

    no_retrieval = recorder.begin_query(
        "2" * 32,
        TraceMode.DIAGNOSTIC,
        now,
        _identity(),
    )
    no_retrieval.completed_span(
        TraceSpanSpec(
            name="retrieve",
            kind=SpanKind.RETRIEVER,
            parent_span_id=no_retrieval.root.span_id,
            reason_code=DecisionCode.RETRIEVAL_EMPTY,
        )
    )
    no_retrieval.finish(
        status=TraceStatus.REFUSED,
        reason_code=DecisionCode.RETRIEVAL_EMPTY,
        refusal_code="NO_EVIDENCE",
    )

    budget_drop = recorder.begin_query(
        "3" * 32,
        TraceMode.DIAGNOSTIC,
        now,
        _identity(),
    )
    budget_drop.decision(
        stage="evidence.assemble",
        chunk_id="chunk-public-budget",
        selected=False,
        reason_code=DecisionCode.TOKEN_BUDGET,
        details={
            "estimated_total_tokens": 101,
            "actual_candidate_tokens": 25,
        },
    )
    budget_drop.finish(
        status=TraceStatus.REFUSED,
        reason_code=DecisionCode.EVIDENCE_BUDGET_DROP,
        refusal_code="NO_EVIDENCE",
    )

    repaired = recorder.begin_query(
        "4" * 32,
        TraceMode.FULL,
        now,
        _identity(),
    )
    repaired.completed_span(
        TraceSpanSpec(
            name="answer.validate",
            kind=SpanKind.GUARDRAIL,
            parent_span_id=repaired.root.span_id,
            reason_code=DecisionCode.INVALID_EVIDENCE_ID,
            attributes={"repair_triggered": True},
        )
    )
    repaired.completed_span(
        TraceSpanSpec(
            name="llm.repair",
            kind=SpanKind.LLM,
            parent_span_id=repaired.root.span_id,
            reason_code=DecisionCode.REPAIR_OK,
            attributes={"repair_validation_code": "VALIDATION_OK"},
        )
    )
    repaired.artifact(
        "validation",
        {
            "first_validation_code": "INVALID_EVIDENCE_ID",
            "repair_validation_code": "VALIDATION_OK",
        },
    )
    repaired.finish(
        status=TraceStatus.ANSWERED,
        reason_code=DecisionCode.ANSWERED,
    )
    recorder.flush()

    details = {
        trace_id: store.get_trace(trace_id)
        for trace_id in ("1" * 32, "2" * 32, "3" * 32, "4" * 32)
    }

    assert details["1" * 32].trace.status is TraceStatus.ANSWERED
    assert details["2" * 32].spans[-1].reason_code is (
        DecisionCode.RETRIEVAL_EMPTY
    )
    assert details["3" * 32].candidate_decisions[0].reason_code is (
        DecisionCode.TOKEN_BUDGET
    )
    assert any(
        span.reason_code is DecisionCode.REPAIR_OK
        for span in details["4" * 32].spans
    )
    assert len(details["1" * 32].artifacts) == 1
    assert len(details["4" * 32].artifacts) == 1
    recorder.close()
