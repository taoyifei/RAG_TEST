import json
import stat
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.tracing.models import (
    CandidateDecision,
    DecisionCode,
    SpanKind,
    SpanRecord,
    SpanStatus,
    TraceFinish,
    TraceListFilter,
    TraceMode,
    TraceRecord,
    TraceStatus,
)
from rag_app.tracing.store import (
    ArtifactNotFoundError,
    TraceArtifactLimitError,
    TraceStore,
)


def _trace(
    trace_id: str,
    *,
    mode: TraceMode = TraceMode.FULL,
    created_at: datetime | None = None,
) -> TraceRecord:
    created = created_at or datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    ttl = timedelta(hours=72) if mode is TraceMode.FULL else timedelta(days=30)
    return TraceRecord(
        trace_id=trace_id,
        schema_version="1",
        mode=mode,
        created_at=created,
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
        expires_at=created + ttl,
    )


def _span(trace_id: str) -> SpanRecord:
    started_at = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    return SpanRecord(
        trace_id=trace_id,
        span_id="b" * 16,
        parent_span_id=None,
        sequence=1,
        name="rag.query",
        kind=SpanKind.CHAIN,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=12),
        duration_ms=12,
        status=SpanStatus.OK,
        reason_code=DecisionCode.ANSWERED,
        attributes={"status": "answered"},
        input_artifact_id=None,
        output_artifact_id=None,
    )


def test_store_persists_trace_tree_decisions_and_compressed_artifact(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "trace" / "traces.sqlite3"
    database_path.parent.mkdir()
    store = TraceStore(database_path, artifact_limit_bytes=5 * 1024 * 1024)
    store.initialize()
    store.initialize()
    trace_id = "a" * 32
    trace = _trace(trace_id)
    store.create_trace(trace)
    store.put_span(_span(trace_id))
    store.add_candidate_decision(
        CandidateDecision(
            trace_id=trace_id,
            sequence=1,
            stage="rerank",
            chunk_id="chunk-1",
            selected=False,
            reason_code=DecisionCode.DROPPED_FINAL_LIMIT,
            details={"fused_rank": 2, "rerank_rank": 8},
        )
    )
    payload = json.dumps(
        {"question": "synthetic question"},
        separators=(",", ":"),
    ).encode()
    artifact = store.add_artifact(
        trace_id,
        kind="debug.input",
        media_type="application/json",
        payload=payload,
    )
    finished_at = trace.created_at + timedelta(milliseconds=12)
    store.finish_trace(
        trace_id,
        TraceFinish(
            status=TraceStatus.ANSWERED,
            finished_at=finished_at,
        ),
    )

    detail = store.get_trace(trace_id)
    loaded = store.get_artifact(trace_id, artifact.artifact_id)
    exported = json.loads(store.export_trace(trace_id))

    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    assert detail.trace.duration_ms == 12
    assert detail.trace.status is TraceStatus.ANSWERED
    assert detail.spans == (_span(trace_id),)
    assert detail.candidate_decisions[0].chunk_id == "chunk-1"
    assert artifact.original_bytes == len(payload)
    assert artifact.compressed_bytes == len(zlib.compress(payload, level=9))
    assert loaded.payload == payload
    assert exported["trace"]["trace_id"] == trace_id
    assert exported["artifacts"][0]["payload"] == {
        "question": "synthetic question"
    }
    store.close()
    store.close()


def test_artifact_is_bound_to_trace_and_limit_is_fail_closed(
    tmp_path: Path,
) -> None:
    store = TraceStore(
        tmp_path / "traces.sqlite3",
        artifact_limit_bytes=8,
    )
    store.initialize()
    store.create_trace(_trace("a" * 32))
    store.create_trace(_trace("b" * 32))
    artifact = store.add_artifact(
        "a" * 32,
        kind="debug.input",
        media_type="text/plain",
        payload=b"1234",
    )

    with pytest.raises(ArtifactNotFoundError):
        store.get_artifact("b" * 32, artifact.artifact_id)
    with pytest.raises(TraceArtifactLimitError):
        store.add_artifact(
            "a" * 32,
            kind="debug.output",
            media_type="text/plain",
            payload=b"56789",
        )

    assert store.get_trace("a" * 32).trace.capture_complete is False
    store.close()


def test_list_is_bounded_stable_and_prune_honors_mode_ttl(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    store.create_trace(
        _trace(
            "a" * 32,
            mode=TraceMode.FULL,
            created_at=now - timedelta(days=31),
        )
    )
    store.create_trace(
        _trace(
            "b" * 32,
            mode=TraceMode.SAFE,
            created_at=now - timedelta(days=29),
        )
    )
    store.create_trace(
        _trace(
            "c" * 32,
            mode=TraceMode.DIAGNOSTIC,
            created_at=now - timedelta(hours=1),
        )
    )

    page = store.list_traces(TraceListFilter(page=1, page_size=2))
    deleted = store.prune(now=now)
    remaining = store.list_traces(TraceListFilter(page=1, page_size=10))

    assert [item.trace_id for item in page.items] == [
        "c" * 32,
        "b" * 32,
    ]
    assert page.total == 3
    assert deleted == 1
    assert [item.trace_id for item in remaining.items] == [
        "c" * 32,
        "b" * 32,
    ]
    store.close()
