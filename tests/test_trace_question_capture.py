import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.tracing.models import (
    TraceIdentity,
    TraceMode,
    TraceQuestionCapture,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.recorder import TraceRecorder, TraceRecorderConfig
from rag_app.tracing.store import (
    TraceNotFoundError,
    TraceStore,
    TraceStoreClosedError,
)


def _identity() -> TraceIdentity:
    return TraceIdentity(
        pipeline_fingerprint="sha256:" + "1" * 64,
        serving_fingerprint="sha256:" + "2" * 64,
        release_revision="release-1",
        active_collection="rag-active-v1",
        index_manifest_sha256="3" * 64,
        payload_schema_version=2,
    )


def _record_question(
    tmp_path: Path,
    capture: TraceQuestionCapture,
    question: str,
) -> tuple[TraceStore, str]:
    store = TraceStore(tmp_path / f"{capture.value}.sqlite3")
    store.initialize()
    recorder = TraceRecorder(
        store,
        config=TraceRecorderConfig(question_capture=capture),
    )
    trace_id = "a" * 32
    session = recorder.begin_query(
        trace_id,
        TraceMode.SAFE,
        datetime.now(UTC),
        _identity(),
        question=question,
    )
    session.finish(
        status=TraceStatus.ANSWERED,
        reason_code=DecisionCode.ANSWERED,
    )
    recorder.flush()
    recorder.close()
    store.initialize()
    return store, trace_id


def test_hash_only_keeps_digest_without_plaintext(tmp_path: Path) -> None:
    question = "内部项目下一步怎么推进？"
    store, trace_id = _record_question(
        tmp_path,
        TraceQuestionCapture.HASH_ONLY,
        question,
    )

    trace = store.get_trace(trace_id).trace
    exported = store.export_trace(trace_id)

    assert trace.schema_version == "2"
    assert trace.question_text is None
    assert trace.question_sha256 == hashlib.sha256(
        question.encode("utf-8")
    ).hexdigest()
    assert question.encode("utf-8") not in exported
    store.close()


def test_plaintext_keeps_exact_question_and_digest(tmp_path: Path) -> None:
    question = "内部项目\n下一步怎么推进？"
    store, trace_id = _record_question(
        tmp_path,
        TraceQuestionCapture.PLAINTEXT,
        question,
    )

    trace = store.get_trace(trace_id).trace
    exported = json.loads(store.export_trace(trace_id))

    assert trace.question_text == question
    assert trace.question_sha256 == hashlib.sha256(
        question.encode("utf-8")
    ).hexdigest()
    assert exported["trace"]["question_text"] == question
    assert exported["trace"]["question_sha256"] == trace.question_sha256
    store.close()


def test_plaintext_retention_clears_only_question_text(
    tmp_path: Path,
) -> None:
    question = "七天后只保留问题摘要"
    store, trace_id = _record_question(
        tmp_path,
        TraceQuestionCapture.PLAINTEXT,
        question,
    )
    before = store.get_trace(trace_id).trace

    deleted = store.prune(
        now=before.created_at + timedelta(days=7),
        question_retention_seconds=604_800,
    )
    after = store.get_trace(trace_id).trace

    assert deleted == 0
    assert after.question_text is None
    assert after.question_sha256 == before.question_sha256
    assert after.status == before.status
    assert after.expires_at == before.expires_at
    store.close()


def test_trace_ttl_shorter_than_question_retention_removes_trace_first(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "short-trace.sqlite3")
    store.initialize()
    recorder = TraceRecorder(
        store,
        config=TraceRecorderConfig(
            question_capture=TraceQuestionCapture.PLAINTEXT,
            question_retention_seconds=604_800,
        ),
    )
    created = datetime(2026, 8, 1, tzinfo=UTC)
    trace_id = "c" * 32
    session = recorder.begin_query(
        trace_id,
        TraceMode.FULL,
        created,
        _identity(),
        question="Trace TTL 只有三天",
    )
    session.finish(
        status=TraceStatus.ANSWERED,
        reason_code=DecisionCode.ANSWERED,
    )
    recorder.flush()
    recorder.close()
    store.initialize()

    deleted = store.prune(
        now=created + timedelta(days=4),
        question_retention_seconds=604_800,
    )

    assert deleted == 1
    with pytest.raises(TraceNotFoundError):
        store.get_trace(trace_id)
    store.close()


def _create_legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE traces (
                trace_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                pipeline_fingerprint TEXT NOT NULL,
                serving_fingerprint TEXT NOT NULL,
                release_revision TEXT NOT NULL,
                active_collection TEXT NOT NULL,
                index_manifest_sha256 TEXT NOT NULL,
                payload_schema_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                refusal_code TEXT,
                error_code TEXT,
                feedback_useful INTEGER,
                capture_complete INTEGER NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE artifacts (
                trace_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                media_type TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                original_bytes INTEGER NOT NULL,
                compressed_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                compressed_payload BLOB NOT NULL,
                PRIMARY KEY (trace_id, artifact_id)
            );
            CREATE TABLE spans (
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL,
                parent_span_id TEXT,
                sequence INTEGER NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                status TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                attributes_json TEXT NOT NULL,
                input_artifact_id TEXT,
                output_artifact_id TEXT,
                PRIMARY KEY (trace_id, span_id)
            );
            CREATE TABLE candidate_decisions (
                trace_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                stage TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                selected INTEGER NOT NULL,
                reason_code TEXT NOT NULL,
                details_json TEXT NOT NULL,
                PRIMARY KEY (trace_id, sequence)
            );
            PRAGMA user_version=1;
            """
        )
        connection.execute(
            """
            INSERT INTO traces VALUES (
                ?, '1', 'SAFE', ?, NULL, NULL, ?, ?, ?, ?, ?, 2,
                'RUNNING', NULL, NULL, NULL, 1, ?
            )
            """,
            (
                "b" * 32,
                "2026-08-01T00:00:00+00:00",
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                "release-1",
                "rag-active-v1",
                "3" * 64,
                "2026-09-01T00:00:00+00:00",
            ),
        )
    path.chmod(0o600)


def test_v1_database_migrates_to_v2_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_database(path)

    first = TraceStore(path)
    first.initialize()
    trace = first.get_trace("b" * 32).trace
    first.close()
    second = TraceStore(path)
    second.initialize()
    second.close()

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(traces)")
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert trace.question_text is None
    assert trace.question_sha256 is None
    assert {"question_text", "question_sha256"} <= columns
    assert "question_preview" not in columns
    assert version == 2


def test_partial_schema_migration_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "partial.sqlite3"
    _create_legacy_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE traces ADD COLUMN question_text TEXT")

    store = TraceStore(path)
    with pytest.raises(ValueError, match="Trace schema"):
        store.initialize()
    with pytest.raises(TraceStoreClosedError):
        store.healthcheck()
