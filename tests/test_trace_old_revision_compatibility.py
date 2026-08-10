from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from rag_app.tracing.store import TraceStore

_ROOT = Path(__file__).parents[1]
_OLD_REVISION = "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"
_TRACE_ID = "a" * 32


def _old_source(tmp_path: Path) -> Path:
    archive = tmp_path / "old-source.tar"
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required")
    subprocess.run(  # noqa: S603
        [
            git,
            "-C",
            str(_ROOT),
            "archive",
            "--format=tar",
            f"--output={archive}",
            _OLD_REVISION,
            "src/rag_app",
        ],
        check=True,
    )
    source = tmp_path / "old-source"
    source.mkdir()
    with tarfile.open(archive, mode="r:") as payload:
        payload.extractall(source, filter="data")
    return source / "src"


def _run_old(source: Path, database: Path, action: str) -> dict[str, object]:
    program = r"""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from rag_app.tracing.store import TraceStore

database = Path(sys.argv[1])
trace_id = "a" * 32
store = TraceStore(database)
store.initialize()
if sys.argv[2] == "create":
    created = datetime(2026, 8, 10, tzinfo=UTC)
    store.create_trace(TraceRecord(
        trace_id=trace_id,
        schema_version="1",
        mode=TraceMode.SAFE,
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
        expires_at=created + timedelta(days=30),
    ))
    store.put_span(SpanRecord(
        trace_id=trace_id,
        span_id="b" * 16,
        parent_span_id=None,
        sequence=1,
        name="rag.query",
        kind=SpanKind.CHAIN,
        started_at=created,
        finished_at=created + timedelta(milliseconds=1),
        duration_ms=1,
        status=SpanStatus.OK,
        reason_code=DecisionCode.ANSWERED,
        attributes={"safe": True},
        input_artifact_id=None,
        output_artifact_id=None,
    ))
    store.add_candidate_decision(CandidateDecision(
        trace_id=trace_id,
        sequence=1,
        stage="rerank",
        chunk_id="chunk-1",
        selected=True,
        reason_code=DecisionCode.SELECTED,
        details={"rank": 1},
    ))
    store.add_artifact(
        trace_id,
        kind="debug.input",
        media_type="application/json",
        payload=b'{"safe":true}',
    )
    store.set_feedback(trace_id, useful=True)
detail = store.get_trace(trace_id)
print(json.dumps({
    "artifact_count": len(detail.artifacts),
    "decision_count": len(detail.candidate_decisions),
    "feedback_useful": detail.trace.feedback_useful,
    "span_count": len(detail.spans),
    "trace_id": detail.trace.trace_id,
}, separators=(",", ":"), sort_keys=True))
store.close()
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(source),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", program, str(database), action],
        check=True,
        capture_output=True,
        text=True,
        cwd=source.parent,
        env=environment,
    )
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def test_real_old_revision_can_read_trace_database_after_v2_migration(
    tmp_path: Path,
) -> None:
    source = _old_source(tmp_path)
    database = tmp_path / "traces.sqlite3"
    before = _run_old(source, database, "create")

    current = TraceStore(database)
    current.initialize()
    migrated = current.get_trace(_TRACE_ID)
    current.close()

    after = _run_old(source, database, "read")

    assert (
        before
        == after
        == {
            "artifact_count": 1,
            "decision_count": 1,
            "feedback_useful": True,
            "span_count": 1,
            "trace_id": _TRACE_ID,
        }
    )
    assert migrated.trace.question_text is None
    assert migrated.trace.question_sha256 is None
    assert len(migrated.spans) == 1
    assert len(migrated.artifacts) == 1
    assert len(migrated.candidate_decisions) == 1
