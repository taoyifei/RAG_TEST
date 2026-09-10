import json
import sqlite3
import stat
import zlib
from dataclasses import replace
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
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    TraceArtifactLimitError,
    TraceNotFoundError,
    TraceStore,
)


def _trace(
    trace_id: str,
    *,
    mode: TraceMode = TraceMode.FULL,
    created_at: datetime | None = None,
) -> TraceRecord:
    created = created_at or datetime.now(UTC)
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


def _create_v1_database(path: Path, *, partial: bool = False) -> None:
    """创建不含 Product 扩展列的公开旧版 Trace schema。"""
    connection = sqlite3.connect(path)
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
        CREATE TABLE candidate_decisions (
            trace_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            stage TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            selected INTEGER NOT NULL,
            reason_code TEXT NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY (trace_id, sequence),
            FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
                ON DELETE CASCADE
        );
        """
    )
    if partial:
        connection.execute(
            "ALTER TABLE traces ADD COLUMN kind TEXT NOT NULL DEFAULT 'query'"
        )
    else:
        trace = _trace("9" * 32, mode=TraceMode.SAFE)
        connection.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            (
                trace.trace_id,
                trace.schema_version,
                trace.mode.value,
                trace.created_at.isoformat(),
                None,
                None,
                trace.pipeline_fingerprint,
                trace.serving_fingerprint,
                trace.release_revision,
                trace.active_collection,
                trace.index_manifest_sha256,
                trace.payload_schema_version,
                trace.status.value,
                None,
                None,
                None,
                1,
                trace.expires_at.isoformat(),
            ),
        )
    connection.commit()
    connection.close()
    path.chmod(0o600)


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


def test_prune_owns_manual_wal_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """周期 checkpoint 必须随受保护的 prune 维护执行。"""
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    checkpoints: list[sqlite3.Connection] = []
    original_checkpoint = store._checkpoint_wal

    def observe_checkpoint(connection: sqlite3.Connection) -> None:
        checkpoints.append(connection)
        original_checkpoint(connection)

    monkeypatch.setattr(store, "_checkpoint_wal", observe_checkpoint)

    assert store.prune(now=datetime.now(UTC)) == 0
    assert len(checkpoints) == 1
    store.close()


def test_store_raises_automatic_wal_checkpoint_to_bounded_64_mib(
    tmp_path: Path,
) -> None:
    """普通写避免 4 MiB 抖动，同时保留 64 MiB 自动回收后备。"""
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    connection = store._require_connection()
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    autocheckpoint = int(
        connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    )
    journal_size_limit = int(
        connection.execute("PRAGMA journal_size_limit").fetchone()[0]
    )

    assert autocheckpoint * page_size == 64 * 1024 * 1024
    assert journal_size_limit == 64 * 1024 * 1024
    store.close()
    store.close()


def test_completed_trace_batch_rolls_back_without_partial_root(
    tmp_path: Path,
) -> None:
    """批量 Trace 中任一非法成员都必须回滚根记录与既有成员。"""
    store = TraceStore(tmp_path / "traces.sqlite3")
    store.initialize()
    trace_id = "7" * 32
    trace = _trace(trace_id, mode=TraceMode.SAFE)
    invalid_span = replace(
        _span(trace_id),
        span_id="c" * 16,
        parent_span_id="a" * 16,
        sequence=2,
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.write_completed_trace(
            trace,
            (_span(trace_id), invalid_span),
            (),
            TraceFinish(
                status=TraceStatus.ANSWERED,
                finished_at=trace.created_at + timedelta(milliseconds=12),
            ),
        )

    with pytest.raises(TraceNotFoundError):
        store.get_trace(trace_id)
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


def test_corrupt_artifact_fails_integrity_check(tmp_path: Path) -> None:
    """持久载荷损坏不能以原 Artifact 正文返回。"""
    database = tmp_path / "traces.sqlite3"
    store = TraceStore(database)
    store.initialize()
    store.create_trace(_trace("a" * 32))
    artifact = store.add_artifact(
        "a" * 32,
        kind="debug.input",
        media_type="text/plain",
        payload=b"private payload",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE artifacts SET compressed_payload=? "
            "WHERE trace_id=? AND artifact_id=?",
            (b"not-zlib", "a" * 32, artifact.artifact_id),
        )

    with pytest.raises(ArtifactIntegrityError):
        store.get_artifact("a" * 32, artifact.artifact_id)
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
        replace(
            _trace(
                "c" * 32,
                mode=TraceMode.DIAGNOSTIC,
                created_at=now - timedelta(hours=1),
            ),
            document_id="doc_public",
            revision_id="irev_public",
        )
    )

    page = store.list_traces(TraceListFilter(page=1, page_size=2))
    linked = store.list_traces(
        TraceListFilter(
            document_id="doc_public",
            revision_id="irev_public",
        )
    )
    deleted = store.prune(now=now)
    remaining = store.list_traces(TraceListFilter(page=1, page_size=10))

    assert [item.trace_id for item in page.items] == [
        "c" * 32,
        "b" * 32,
    ]
    assert page.total == 3
    assert [item.trace_id for item in linked.items] == ["c" * 32]
    assert deleted == 1
    assert [item.trace_id for item in remaining.items] == [
        "c" * 32,
        "b" * 32,
    ]
    store.close()


def test_v1_schema_migrates_and_old_record_remains_readable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v1.sqlite3"
    _create_v1_database(database)

    store = TraceStore(database)
    store.initialize()
    detail = store.get_trace("9" * 32)

    assert detail.trace.kind == "query"
    assert detail.trace.project_id is None
    assert detail.trace.capture_complete is True
    store.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM trace_schema_metadata WHERE singleton=1"
        ).fetchone() == (2,)


def test_partial_product_migration_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "partial.sqlite3"
    _create_v1_database(database, partial=True)

    with pytest.raises(RuntimeError, match="部分迁移"):
        TraceStore(database).initialize()


def test_restart_recovers_running_root_and_span_as_interrupted(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart.sqlite3"
    store = TraceStore(database)
    store.initialize()
    trace = _trace("8" * 32, mode=TraceMode.SAFE)
    running_span = replace(
        _span(trace.trace_id),
        finished_at=None,
        duration_ms=None,
        status=SpanStatus.RUNNING,
        reason_code=DecisionCode.STARTED,
    )
    store.create_trace(trace)
    store.put_span(running_span)
    store.close()

    reopened = TraceStore(database)
    reopened.initialize()
    recovered_at = trace.created_at + timedelta(seconds=2)
    assert reopened.recover_running(now=recovered_at) == 1
    detail = reopened.get_trace(trace.trace_id)
    assert detail.trace.status is TraceStatus.INTERRUPTED
    assert detail.trace.error_code == "PROCESS_INTERRUPTED"
    assert detail.spans[0].status is SpanStatus.INTERRUPTED
    assert detail.spans[0].finished_at == recovered_at
    assert reopened.recover_running(now=recovered_at) == 0
    reopened.close()


def test_prune_respects_persistent_export_lease(tmp_path: Path) -> None:
    """在途导出 lease 释放前，prune 不删除已到期 Trace。"""
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    database = tmp_path / "lease.sqlite3"
    exporter = TraceStore(database)
    exporter.initialize()
    trace = _trace(
        "d" * 32,
        mode=TraceMode.SAFE,
        created_at=now - timedelta(days=31),
    )
    exporter.create_trace(trace)
    pruner = TraceStore(database)
    pruner.initialize()

    with exporter.export_guard((trace.trace_id,)):
        assert pruner.prune(now=now) == 0
        assert pruner.get_trace(trace.trace_id).trace.trace_id == trace.trace_id

    assert pruner.prune(now=now) == 1
    with pytest.raises(TraceNotFoundError):
        pruner.get_trace(trace.trace_id)
    pruner.close()
    exporter.close()
