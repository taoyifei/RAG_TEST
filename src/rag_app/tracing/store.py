"""独立 SQLite Query Trace Store。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import uuid
import zlib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from rag_app.tracing.models import (
    ArtifactContent,
    ArtifactMetadata,
    CandidateDecision,
    DecisionCode,
    SpanKind,
    SpanRecord,
    SpanStatus,
    TraceDetail,
    TraceFinish,
    TraceListFilter,
    TraceMode,
    TracePage,
    TraceRecord,
    TraceStatus,
)

__all__ = [
    "ArtifactExpiredError",
    "ArtifactNotFoundError",
    "TraceArtifactLimitError",
    "TraceNotFoundError",
    "TraceStore",
    "TraceStoreClosedError",
]

_DEFAULT_ARTIFACT_LIMIT = 5 * 1024 * 1024
_DEFAULT_ARTIFACT_ITEM_LIMIT = 2 * 1024 * 1024
_DEFAULT_GLOBAL_ARTIFACT_LIMIT = 64 * 1024 * 1024
_DEFAULT_EXPORT_LIMIT = 16 * 1024 * 1024
_DEFAULT_SPAN_LIMIT = 512
_DEFAULT_DECISION_LIMIT = 4096
_DEFAULT_STAGE_DECISION_LIMIT = 512
_LATEST_SCHEMA_VERSION = 2
_PRODUCT_TRACE_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'query'",
    "project_id": "TEXT",
    "knowledge_base_id": "TEXT",
    "owner_sha256": "TEXT",
    "request_id": "TEXT",
    "job_id": "TEXT",
    "document_id": "TEXT",
    "revision_id": "TEXT",
    "profile_id": "TEXT",
    "index_fingerprint": "TEXT",
    "source_revision": "TEXT",
    "capture_incomplete_reason": "TEXT",
    "dropped_span_count": "INTEGER NOT NULL DEFAULT 0",
    "dropped_decision_count": "INTEGER NOT NULL DEFAULT 0",
    "writer_queue_high_water": "INTEGER NOT NULL DEFAULT 0",
}
_PRODUCT_DECISION_COLUMNS = {
    "candidate_id": "TEXT",
    "evidence_id": "TEXT",
    "channel": "TEXT",
    "rank": "INTEGER",
    "score_type": "TEXT",
    "score": "REAL",
    "contribution": "REAL",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS traces (
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
    expires_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'query',
    project_id TEXT,
    knowledge_base_id TEXT,
    owner_sha256 TEXT,
    request_id TEXT,
    job_id TEXT,
    document_id TEXT,
    revision_id TEXT,
    profile_id TEXT,
    index_fingerprint TEXT,
    source_revision TEXT,
    capture_incomplete_reason TEXT,
    dropped_span_count INTEGER NOT NULL DEFAULT 0,
    dropped_decision_count INTEGER NOT NULL DEFAULT 0,
    writer_queue_high_water INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS traces_created_idx
ON traces(created_at DESC, trace_id DESC);
CREATE INDEX IF NOT EXISTS traces_expires_idx ON traces(expires_at);
CREATE TABLE IF NOT EXISTS artifacts (
    trace_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    original_bytes INTEGER NOT NULL,
    compressed_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    compressed_payload BLOB NOT NULL,
    PRIMARY KEY (trace_id, artifact_id),
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS spans (
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
    PRIMARY KEY (trace_id, span_id),
    UNIQUE (trace_id, sequence),
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE,
    FOREIGN KEY (trace_id, parent_span_id)
        REFERENCES spans(trace_id, span_id),
    FOREIGN KEY (trace_id, input_artifact_id)
        REFERENCES artifacts(trace_id, artifact_id),
    FOREIGN KEY (trace_id, output_artifact_id)
        REFERENCES artifacts(trace_id, artifact_id)
);

CREATE TABLE IF NOT EXISTS candidate_decisions (
    trace_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    stage TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    selected INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    details_json TEXT NOT NULL,
    candidate_id TEXT,
    evidence_id TEXT,
    channel TEXT,
    rank INTEGER,
    score_type TEXT,
    score REAL,
    contribution REAL,
    PRIMARY KEY (trace_id, sequence),
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
);
"""


class TraceStoreClosedError(RuntimeError):
    """Trace Store 已关闭或尚未初始化。"""


class TraceNotFoundError(LookupError):
    """指定 Trace 不存在。"""


class ArtifactNotFoundError(LookupError):
    """artifact 不存在或不属于指定 Trace。"""


class ArtifactExpiredError(LookupError):
    """artifact 所属 Trace 已到期。"""


class TraceArtifactLimitError(ValueError):
    """FULL Trace 的原始 artifact 总量超过硬上限。"""


class TraceStore:
    """以单连接和锁提供有界、严格的 Trace 持久化。"""

    def __init__(  # noqa: PLR0913
        self,
        database_path: Path,
        *,
        artifact_limit_bytes: int = _DEFAULT_ARTIFACT_LIMIT,
        artifact_item_limit_bytes: int = _DEFAULT_ARTIFACT_ITEM_LIMIT,
        global_artifact_limit_bytes: int = _DEFAULT_GLOBAL_ARTIFACT_LIMIT,
        export_limit_bytes: int = _DEFAULT_EXPORT_LIMIT,
        span_limit: int = _DEFAULT_SPAN_LIMIT,
        decision_limit: int = _DEFAULT_DECISION_LIMIT,
        stage_decision_limit: int = _DEFAULT_STAGE_DECISION_LIMIT,
        minimum_free_disk_bytes: int = 1024 * 1024,
        busy_timeout_ms: int = 5000,
    ) -> None:
        """保存独立数据库路径和硬容量。

        Args:
            database_path: 不与状态库共用的 SQLite 文件。
            artifact_limit_bytes: 单条 Trace 的原始 artifact 字节上限。
            artifact_item_limit_bytes: 单项 artifact 原始字节上限。
            global_artifact_limit_bytes: Store 内 artifact 原始总字节上限。
            export_limit_bytes: 单条 canonical JSON 导出字节上限。
            span_limit: 单条 Trace 的 span 数量上限。
            decision_limit: 单条 Trace 的候选决策数量上限。
            stage_decision_limit: 单阶段候选决策数量上限。
            minimum_free_disk_bytes: FULL 准入必须保留的磁盘空间。
            busy_timeout_ms: SQLite 锁等待上限。

        Raises:
            ValueError: 容量、超时或路径无效。

        """
        limits = (
            artifact_limit_bytes,
            artifact_item_limit_bytes,
            global_artifact_limit_bytes,
            export_limit_bytes,
            span_limit,
            decision_limit,
            stage_decision_limit,
            minimum_free_disk_bytes,
            busy_timeout_ms,
        )
        if any(value <= 0 for value in limits):
            raise ValueError("Trace 容量和 busy timeout 必须为正数。")
        if stage_decision_limit > decision_limit:
            raise ValueError("单阶段候选上限不能超过单 Trace 上限。")
        self._path = _canonical_database_path(database_path)
        self._artifact_limit_bytes = artifact_limit_bytes
        self._artifact_item_limit_bytes = min(
            artifact_item_limit_bytes,
            artifact_limit_bytes,
        )
        self._global_artifact_limit_bytes = global_artifact_limit_bytes
        self._export_limit_bytes = export_limit_bytes
        self._span_limit = span_limit
        self._decision_limit = decision_limit
        self._stage_decision_limit = stage_decision_limit
        self._minimum_free_disk_bytes = minimum_free_disk_bytes
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._active_exports: Counter[str] = Counter()

    @property
    def database_path(self) -> Path:
        """返回 canonical Trace 数据库路径。

        Args:
            无参数；读取当前 Store 配置。

        Returns:
            已完成父目录 realpath 校验的绝对路径。

        """
        return self._path

    def initialize(self) -> None:
        """安全创建文件并幂等初始化四类持久表。

        Args:
            无参数；使用构造时的数据库路径。

        Returns:
            无返回值。

        """
        with self._lock:
            if self._connection is not None:
                return
            _secure_create_database(self._path)
            connection = sqlite3.connect(
                self._path,
                timeout=self._busy_timeout_ms / 1000,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)
            self._migrate_product_schema(connection)
            connection.commit()
            self._connection = connection

    def _migrate_product_schema(self, connection: sqlite3.Connection) -> None:
        """把旧 Query Trace v1 原子扩展为 Product Operational Trace v2。"""
        trace_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(traces)")
        }
        decision_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(candidate_decisions)"
            )
        }
        missing_trace = set(_PRODUCT_TRACE_COLUMNS) - trace_columns
        missing_decision = set(_PRODUCT_DECISION_COLUMNS) - decision_columns
        present_new = (set(_PRODUCT_TRACE_COLUMNS) & trace_columns) | (
            set(_PRODUCT_DECISION_COLUMNS) & decision_columns
        )
        version_row = connection.execute(
            "SELECT version FROM trace_schema_metadata WHERE singleton=1"
        ).fetchone()
        version = None if version_row is None else int(version_row[0])
        if (missing_trace or missing_decision) and present_new:
            raise RuntimeError("Trace schema 处于部分迁移状态，拒绝启动。")
        if version not in (None, 1, _LATEST_SCHEMA_VERSION):
            raise RuntimeError("Trace schema 版本不受支持。")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for name in sorted(missing_trace):
                definition = _PRODUCT_TRACE_COLUMNS[name]
                connection.execute(
                    f"ALTER TABLE traces ADD COLUMN {name} {definition}"
                )
            for name in sorted(missing_decision):
                definition = _PRODUCT_DECISION_COLUMNS[name]
                connection.execute(
                    "ALTER TABLE candidate_decisions "
                    f"ADD COLUMN {name} {definition}"
                )
            connection.execute(
                "INSERT INTO trace_schema_metadata(singleton, version) "
                "VALUES (1, ?) ON CONFLICT(singleton) DO UPDATE "
                "SET version=excluded.version",
                (_LATEST_SCHEMA_VERSION,),
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS traces_product_scope_idx "
                "ON traces(project_id, knowledge_base_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS traces_job_idx ON traces(job_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS traces_request_idx "
                "ON traces(request_id)"
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def recover_running(self, *, now: datetime | None = None) -> int:
        """把重启前遗留的根与 span 关闭为 INTERRUPTED。

        Args:
            now: 可选的确定性恢复时点；省略时使用当前 UTC 时间。

        Returns:
            被恢复为终态的根 Trace 数量。

        """
        recovered_at = now or datetime.now(UTC)
        with self._lock:
            connection = self._require_connection()
            rows = connection.execute(
                "SELECT trace_id, created_at FROM traces WHERE status='RUNNING'"
            ).fetchall()
            for row in rows:
                trace_id = str(row["trace_id"])
                created_at = _parse_timestamp(row["created_at"])
                duration_ms = max(
                    0,
                    round((recovered_at - created_at).total_seconds() * 1000),
                )
                running_spans = connection.execute(
                    "SELECT span_id, started_at FROM spans "
                    "WHERE trace_id=? AND status='RUNNING'",
                    (trace_id,),
                ).fetchall()
                for span_row in running_spans:
                    started_at = _parse_timestamp(span_row["started_at"])
                    span_duration_ms = max(
                        0,
                        round(
                            (recovered_at - started_at).total_seconds() * 1000
                        ),
                    )
                    connection.execute(
                        "UPDATE spans SET finished_at=?, duration_ms=?, "
                        "status=?, reason_code=? WHERE trace_id=? AND "
                        "span_id=? AND status='RUNNING'",
                        (
                            _timestamp(recovered_at),
                            span_duration_ms,
                            SpanStatus.INTERRUPTED.value,
                            DecisionCode.INTERRUPTED.value,
                            trace_id,
                            span_row["span_id"],
                        ),
                    )
                connection.execute(
                    "UPDATE traces SET finished_at=?, duration_ms=?, status=?, "
                    "error_code=COALESCE(error_code, 'PROCESS_INTERRUPTED') "
                    "WHERE trace_id=? AND status='RUNNING'",
                    (
                        _timestamp(recovered_at),
                        duration_ms,
                        TraceStatus.INTERRUPTED.value,
                        trace_id,
                    ),
                )
            connection.commit()
            return len(rows)

    def preflight_full(self, *, reserved_bytes: int) -> None:
        """在业务执行前检查 FULL 捕获、磁盘与导出容量。

        Args:
            reserved_bytes: 本次 FULL 查询要求预留的原始 Artifact 字节。

        Returns:
            无返回值；所有容量和权限检查通过即返回。

        """
        if not 0 < reserved_bytes <= self._artifact_limit_bytes:
            raise TraceArtifactLimitError("FULL artifact 预留量越界。")
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                "SELECT COALESCE(SUM(original_bytes), 0) FROM artifacts"
            ).fetchone()
            used = 0 if row is None else int(row[0])
            if used + reserved_bytes > self._global_artifact_limit_bytes:
                raise TraceArtifactLimitError("FULL artifact 全局容量不足。")
            if reserved_bytes > self._export_limit_bytes:
                raise TraceArtifactLimitError("FULL 导出预算不足。")
            info = os.statvfs(self._path.parent)
            free = info.f_bavail * info.f_frsize
            if free < self._minimum_free_disk_bytes + reserved_bytes:
                raise TraceArtifactLimitError("FULL Trace 磁盘可用空间不足。")
            if not os.access(self._path.parent, os.R_OK | os.W_OK | os.X_OK):
                raise PermissionError("FULL Trace 数据目录权限不足。")

    def healthcheck(self) -> None:
        """确认数据库仍可执行只读查询。

        Args:
            无参数；检查当前连接。

        Returns:
            无返回值。

        Raises:
            TraceStoreClosedError: Store 未初始化或已关闭。
            sqlite3.Error: SQLite 不可用。

        """
        with self._lock:
            self._require_connection().execute("SELECT 1").fetchone()

    def create_trace(self, trace: TraceRecord) -> None:
        """插入一条新的 RUNNING Trace。

        Args:
            trace: 已通过契约校验的根记录。

        Returns:
            无返回值。

        """
        if trace.status is not TraceStatus.RUNNING:
            raise ValueError("新 Trace 必须处于 RUNNING。")
        with self._lock:
            connection = self._require_connection()
            connection.execute(
                """
                INSERT INTO traces (
                    trace_id, schema_version, mode, created_at,
                    finished_at, duration_ms, pipeline_fingerprint,
                    serving_fingerprint, release_revision,
                    active_collection, index_manifest_sha256,
                    payload_schema_version, status, refusal_code,
                    error_code, feedback_useful, capture_complete,
                    expires_at, kind, project_id, knowledge_base_id,
                    owner_sha256, request_id, job_id, document_id,
                    revision_id, profile_id, index_fingerprint,
                    source_revision, capture_incomplete_reason,
                    dropped_span_count, dropped_decision_count,
                    writer_queue_high_water
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.trace_id,
                    trace.schema_version,
                    trace.mode.value,
                    _timestamp(trace.created_at),
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
                    int(trace.capture_complete),
                    _timestamp(trace.expires_at),
                    trace.kind,
                    trace.project_id,
                    trace.knowledge_base_id,
                    trace.owner_sha256,
                    trace.request_id,
                    trace.job_id,
                    trace.document_id,
                    trace.revision_id,
                    trace.profile_id,
                    trace.index_fingerprint,
                    trace.source_revision,
                    trace.capture_incomplete_reason,
                    trace.dropped_span_count,
                    trace.dropped_decision_count,
                    trace.writer_queue_high_water,
                ),
            )
            connection.commit()

    def finish_trace(
        self,
        trace_id: str,
        finish: TraceFinish,
    ) -> None:
        """原子关闭 Trace 并计算总耗时。

        Args:
            trace_id: 待关闭的稳定 Trace ID。
            finish: 已校验的 Trace 终态字段。

        Returns:
            无返回值。

        """
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                "SELECT created_at, status FROM traces WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            if row is None:
                raise TraceNotFoundError(trace_id)
            if str(row["status"]) != TraceStatus.RUNNING.value:
                return
            created_at = _parse_timestamp(row["created_at"])
            duration_ms = max(
                0,
                round((finish.finished_at - created_at).total_seconds() * 1000),
            )
            connection.execute(
                """
                UPDATE traces
                SET finished_at=?, duration_ms=?, status=?,
                    refusal_code=?, error_code=?
                WHERE trace_id=? AND status='RUNNING'
                """,
                (
                    _timestamp(finish.finished_at),
                    duration_ms,
                    finish.status.value,
                    finish.refusal_code,
                    finish.error_code,
                    trace_id,
                ),
            )
            connection.commit()

    def update_trace_identity(
        self,
        trace_id: str,
        *,
        revision_id: str | None = None,
        index_fingerprint: str | None = None,
        serving_fingerprint: str | None = None,
        active_collection: str | None = None,
    ) -> None:
        """在 snapshot 固定后补齐根 Trace 的活动身份。

        Args:
            trace_id: 待更新根 Trace ID。
            revision_id: 可选的活动 Revision ID。
            index_fingerprint: 可选的活动索引指纹。
            serving_fingerprint: 可选的查询服务指纹。
            active_collection: 可选的活动集合身份。

        Returns:
            无返回值。

        """
        if not any(
            (
                revision_id,
                index_fingerprint,
                serving_fingerprint,
                active_collection,
            )
        ):
            return
        with self._lock:
            connection = self._require_connection()
            cursor = connection.execute(
                """
                UPDATE traces
                SET revision_id=COALESCE(?, revision_id),
                    index_fingerprint=COALESCE(?, index_fingerprint),
                    serving_fingerprint=COALESCE(?, serving_fingerprint),
                    active_collection=COALESCE(?, active_collection)
                WHERE trace_id=? AND status='RUNNING'
                """,
                (
                    revision_id,
                    index_fingerprint,
                    serving_fingerprint,
                    active_collection,
                    trace_id,
                ),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    "SELECT 1 FROM traces WHERE trace_id=?", (trace_id,)
                ).fetchone()
                if existing is None:
                    raise TraceNotFoundError(trace_id)
            connection.commit()

    def put_span(self, span: SpanRecord) -> None:
        """插入或关闭同一身份的 span。

        Args:
            span: RUNNING 或终态 span 记录。

        Returns:
            无返回值。

        """
        with self._lock:
            connection = self._require_connection()
            existing = connection.execute(
                "SELECT 1 FROM spans WHERE trace_id=? AND span_id=?",
                (span.trace_id, span.span_id),
            ).fetchone()
            if existing is None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM spans WHERE trace_id=?",
                    (span.trace_id,),
                ).fetchone()
                if count is not None and int(count[0]) >= self._span_limit:
                    self._mark_capture_incomplete(
                        connection,
                        span.trace_id,
                        reason="SPAN_LIMIT",
                        dropped_spans=1,
                    )
                    connection.commit()
                    raise TraceArtifactLimitError("Trace span 数超过硬上限。")
            connection.execute(
                """
                INSERT INTO spans (
                    trace_id, span_id, parent_span_id, sequence, name,
                    kind, started_at, finished_at, duration_ms, status,
                    reason_code, attributes_json, input_artifact_id,
                    output_artifact_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id, span_id) DO UPDATE SET
                    finished_at=excluded.finished_at,
                    duration_ms=excluded.duration_ms,
                    status=excluded.status,
                    reason_code=excluded.reason_code,
                    attributes_json=excluded.attributes_json,
                    input_artifact_id=excluded.input_artifact_id,
                    output_artifact_id=excluded.output_artifact_id
                """,
                (
                    span.trace_id,
                    span.span_id,
                    span.parent_span_id,
                    span.sequence,
                    span.name,
                    span.kind.value,
                    _timestamp(span.started_at),
                    (
                        None
                        if span.finished_at is None
                        else _timestamp(span.finished_at)
                    ),
                    span.duration_ms,
                    span.status.value,
                    span.reason_code.value,
                    _json(span.attributes),
                    span.input_artifact_id,
                    span.output_artifact_id,
                ),
            )
            connection.commit()

    def add_candidate_decision(
        self,
        decision: CandidateDecision,
    ) -> None:
        """保存候选漏斗中的一个确定性决策。

        Args:
            decision: 带稳定 sequence 和 reason code 的候选记录。

        Returns:
            无返回值。

        """
        with self._lock:
            connection = self._require_connection()
            counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN stage=? THEN 1 ELSE 0 END) AS stage_count "
                "FROM candidate_decisions WHERE trace_id=?",
                (decision.stage, decision.trace_id),
            ).fetchone()
            if counts is not None and (
                int(counts["total"]) >= self._decision_limit
                or int(counts["stage_count"] or 0) >= self._stage_decision_limit
            ):
                self._mark_capture_incomplete(
                    connection,
                    decision.trace_id,
                    reason="DECISION_LIMIT",
                    dropped_decisions=1,
                )
                connection.commit()
                raise TraceArtifactLimitError(
                    "Trace candidate decision 数超过硬上限。"
                )
            connection.execute(
                """
                INSERT INTO candidate_decisions (
                    trace_id, sequence, stage, chunk_id, selected,
                    reason_code, details_json, candidate_id, evidence_id,
                    channel, rank, score_type, score, contribution
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.trace_id,
                    decision.sequence,
                    decision.stage,
                    decision.chunk_id,
                    int(decision.selected),
                    decision.reason_code.value,
                    _json(decision.details),
                    decision.candidate_id,
                    decision.evidence_id,
                    decision.channel,
                    decision.rank,
                    decision.score_type,
                    decision.score,
                    decision.contribution,
                ),
            )
            connection.commit()

    def add_artifact(
        self,
        trace_id: str,
        *,
        kind: str,
        media_type: str,
        payload: bytes,
    ) -> ArtifactMetadata:
        """压缩并保存一份完整 artifact。

        Args:
            trace_id: artifact 所属 Trace。
            kind: 稳定 artifact 类别。
            media_type: 原始 payload 媒体类型。
            payload: 未截断的完整原始字节。

        Returns:
            包含摘要和压缩大小的 artifact 元数据。

        Raises:
            TraceArtifactLimitError: 原始总量超过单 Trace 硬上限。

        """
        if not payload:
            raise ValueError("artifact payload 不能为空。")
        if len(payload) > self._artifact_item_limit_bytes:
            self.mark_capture_incomplete(
                trace_id,
                reason="ARTIFACT_ITEM_LIMIT",
            )
            raise TraceArtifactLimitError("单项 Trace artifact 超过硬上限。")
        compressed = zlib.compress(payload, level=9)
        metadata = ArtifactMetadata(
            artifact_id=uuid.uuid4().hex,
            trace_id=trace_id,
            kind=kind,
            media_type=media_type,
            sha256=hashlib.sha256(payload).hexdigest(),
            original_bytes=len(payload),
            compressed_bytes=len(compressed),
            created_at=datetime.now(UTC),
        )
        with self._lock:
            connection = self._require_connection()
            row = connection.execute(
                """
                SELECT COALESCE(SUM(original_bytes), 0) AS used
                FROM artifacts WHERE trace_id=?
                """,
                (trace_id,),
            ).fetchone()
            if row is None:
                raise TraceNotFoundError(trace_id)
            used = int(row["used"])
            global_row = connection.execute(
                "SELECT COALESCE(SUM(original_bytes), 0) AS used FROM artifacts"
            ).fetchone()
            global_used = 0 if global_row is None else int(global_row["used"])
            if (
                used + len(payload) > self._artifact_limit_bytes
                or global_used + len(payload)
                > self._global_artifact_limit_bytes
            ):
                self._mark_capture_incomplete(
                    connection,
                    trace_id,
                    reason="ARTIFACT_CAPACITY_LIMIT",
                )
                connection.commit()
                raise TraceArtifactLimitError(
                    "Trace artifact 原始字节总量超过硬上限。"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        trace_id, artifact_id, kind, media_type, sha256,
                        original_bytes, compressed_bytes, created_at,
                        compressed_payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trace_id,
                        metadata.artifact_id,
                        kind,
                        media_type,
                        metadata.sha256,
                        metadata.original_bytes,
                        metadata.compressed_bytes,
                        _timestamp(metadata.created_at),
                        compressed,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise TraceNotFoundError(trace_id) from error
            connection.commit()
        return metadata

    def mark_capture_incomplete(
        self,
        trace_id: str,
        *,
        reason: str = "TRACE_CAPTURE_FAILED",
        dropped_spans: int = 0,
        dropped_decisions: int = 0,
        queue_high_water: int = 0,
    ) -> None:
        """把 Trace 标记为未完整捕获。

        Args:
            trace_id: 待标记的 Trace ID。
            reason: 稳定的不完整捕获原因码。
            dropped_spans: 本次未写入的 span 数。
            dropped_decisions: 本次未写入的候选决策数。
            queue_high_water: writer 队列达到的最高深度。

        Returns:
            无返回值。

        """
        with self._lock:
            connection = self._require_connection()
            self._mark_capture_incomplete(
                connection,
                trace_id,
                reason=reason,
                dropped_spans=dropped_spans,
                dropped_decisions=dropped_decisions,
                queue_high_water=queue_high_water,
            )
            connection.commit()

    def get_trace(self, trace_id: str) -> TraceDetail:
        """读取稳定排序的根、span、候选和 artifact 元数据。

        Args:
            trace_id: 待读取的 Trace ID。

        Returns:
            完整 Trace 详情。

        Raises:
            TraceNotFoundError: Trace 不存在。

        """
        with self._lock:
            connection = self._require_connection()
            trace_row = connection.execute(
                "SELECT * FROM traces WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            if trace_row is None:
                raise TraceNotFoundError(trace_id)
            span_rows = connection.execute(
                """
                SELECT * FROM spans WHERE trace_id=?
                ORDER BY sequence ASC, span_id ASC
                """,
                (trace_id,),
            ).fetchall()
            decision_rows = connection.execute(
                """
                SELECT * FROM candidate_decisions WHERE trace_id=?
                ORDER BY sequence ASC
                """,
                (trace_id,),
            ).fetchall()
            artifact_rows = connection.execute(
                """
                SELECT * FROM artifacts WHERE trace_id=?
                ORDER BY created_at ASC, artifact_id ASC
                """,
                (trace_id,),
            ).fetchall()
        return TraceDetail(
            trace=_trace_from_row(trace_row),
            spans=tuple(_span_from_row(row) for row in span_rows),
            candidate_decisions=tuple(
                _decision_from_row(row) for row in decision_rows
            ),
            artifacts=tuple(
                _artifact_metadata_from_row(row) for row in artifact_rows
            ),
        )

    def get_artifact(
        self,
        trace_id: str,
        artifact_id: str,
        *,
        now: datetime | None = None,
    ) -> ArtifactContent:
        """读取属于指定 Trace 且未到期的完整 artifact。

        Args:
            trace_id: 所属 Trace ID。
            artifact_id: 待读取的 artifact ID。
            now: 可注入的到期判断时点。

        Returns:
            已解压并复核 SHA256 的完整 payload。

        Raises:
            ArtifactExpiredError: Trace 已到期。
            ArtifactNotFoundError: artifact 不存在或跨 Trace。

        """
        check_time = now or datetime.now(UTC)
        with self._lock:
            connection = self._require_connection()
            trace_row = connection.execute(
                "SELECT expires_at FROM traces WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            if trace_row is None:
                raise ArtifactNotFoundError(artifact_id)
            if _parse_timestamp(trace_row["expires_at"]) <= check_time:
                raise ArtifactExpiredError(artifact_id)
            row = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE trace_id=? AND artifact_id=?
                """,
                (trace_id, artifact_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFoundError(artifact_id)
            compressed = bytes(row["compressed_payload"])
        try:
            payload = zlib.decompress(compressed)
        except zlib.error as error:
            raise ValueError("Trace artifact 压缩内容损坏。") from error
        metadata = _artifact_metadata_from_row(row)
        if (
            len(payload) != metadata.original_bytes
            or hashlib.sha256(payload).hexdigest() != metadata.sha256
        ):
            raise ValueError("Trace artifact 完整性校验失败。")
        return ArtifactContent(metadata=metadata, payload=payload)

    def list_traces(self, filters: TraceListFilter) -> TracePage:
        """按稳定倒序返回有上限的 Trace 列表。

        Args:
            filters: 已校验的分页和可选过滤条件。

        Returns:
            稳定排序列表页和过滤后总数。

        """
        status = None if filters.status is None else filters.status.value
        created_from = (
            None
            if filters.created_from is None
            else _timestamp(filters.created_from)
        )
        created_to = (
            None
            if filters.created_to is None
            else _timestamp(filters.created_to)
        )
        feedback = (
            None
            if filters.feedback_useful is None
            else int(filters.feedback_useful)
        )
        capture_mode = (
            None if filters.capture_mode is None else filters.capture_mode.value
        )
        capture_complete = (
            None
            if filters.capture_complete is None
            else int(filters.capture_complete)
        )
        values = (
            filters.trace_id,
            filters.trace_id,
            created_from,
            created_from,
            created_to,
            created_to,
            status,
            status,
            filters.refusal_code,
            filters.refusal_code,
            filters.error_code,
            filters.error_code,
            feedback,
            feedback,
            filters.kind,
            filters.kind,
            filters.project_id,
            filters.project_id,
            filters.knowledge_base_id,
            filters.knowledge_base_id,
            filters.request_id,
            filters.request_id,
            filters.job_id,
            filters.job_id,
            filters.document_id,
            filters.document_id,
            filters.revision_id,
            filters.revision_id,
            capture_mode,
            capture_mode,
            capture_complete,
            capture_complete,
        )
        with self._lock:
            connection = self._require_connection()
            count_row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM traces
                WHERE (? IS NULL OR trace_id=?)
                  AND (? IS NULL OR created_at>=?)
                  AND (? IS NULL OR created_at<=?)
                  AND (? IS NULL OR status=?)
                  AND (? IS NULL OR refusal_code=?)
                  AND (? IS NULL OR error_code=?)
                  AND (? IS NULL OR feedback_useful=?)
                  AND (? IS NULL OR kind=?)
                  AND (? IS NULL OR project_id=?)
                  AND (? IS NULL OR knowledge_base_id=?)
                  AND (? IS NULL OR request_id=?)
                  AND (? IS NULL OR job_id=?)
                  AND (? IS NULL OR document_id=?)
                  AND (? IS NULL OR revision_id=?)
                  AND (? IS NULL OR mode=?)
                  AND (? IS NULL OR capture_complete=?)
                """,
                values,
            ).fetchone()
            rows = connection.execute(
                """
                SELECT * FROM traces
                WHERE (? IS NULL OR trace_id=?)
                  AND (? IS NULL OR created_at>=?)
                  AND (? IS NULL OR created_at<=?)
                  AND (? IS NULL OR status=?)
                  AND (? IS NULL OR refusal_code=?)
                  AND (? IS NULL OR error_code=?)
                  AND (? IS NULL OR feedback_useful=?)
                  AND (? IS NULL OR kind=?)
                  AND (? IS NULL OR project_id=?)
                  AND (? IS NULL OR knowledge_base_id=?)
                  AND (? IS NULL OR request_id=?)
                  AND (? IS NULL OR job_id=?)
                  AND (? IS NULL OR document_id=?)
                  AND (? IS NULL OR revision_id=?)
                  AND (? IS NULL OR mode=?)
                  AND (? IS NULL OR capture_complete=?)
                ORDER BY created_at DESC, trace_id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    *values,
                    filters.page_size,
                    (filters.page - 1) * filters.page_size,
                ),
            ).fetchall()
        total = 0 if count_row is None else int(count_row["count"])
        return TracePage(
            items=tuple(_trace_from_row(row) for row in rows),
            page=filters.page,
            page_size=filters.page_size,
            total=total,
        )

    def set_feedback(self, trace_id: str, *, useful: bool) -> None:
        """把非敏感反馈关联到 Trace。

        Args:
            trace_id: 已存在的 Trace ID。
            useful: 用户是否认为回答有用。

        Returns:
            无返回值。

        """
        with self._lock:
            connection = self._require_connection()
            cursor = connection.execute(
                "UPDATE traces SET feedback_useful=? WHERE trace_id=?",
                (int(useful), trace_id),
            )
            if cursor.rowcount != 1:
                raise TraceNotFoundError(trace_id)
            connection.commit()

    def prune(self, *, now: datetime) -> int:
        """删除已超过各自 mode 到期时点的 Trace。

        Args:
            now: 带时区的固定清理时点。

        Returns:
            级联删除的根 Trace 数量。

        """
        with self._lock:
            connection = self._require_connection()
            protected = frozenset(self._active_exports)
            expired = connection.execute(
                "SELECT trace_id FROM traces WHERE expires_at<=?",
                (_timestamp(now),),
            ).fetchall()
            removable = [
                (str(row["trace_id"]),)
                for row in expired
                if str(row["trace_id"]) not in protected
            ]
            if not removable:
                return 0
            cursor = connection.executemany(
                "DELETE FROM traces WHERE trace_id=?",
                removable,
            )
            connection.commit()
            return cursor.rowcount

    def export_trace(self, trace_id: str) -> bytes:
        """导出单条 Trace 的 canonical JSON。

        Args:
            trace_id: 待导出的 Trace ID。

        Returns:
            只含该 Trace 的 UTF-8 canonical JSON。

        """
        with self._export_guard(trace_id):
            detail = self.get_trace(trace_id)
            artifacts: list[dict[str, object]] = []
            for metadata in detail.artifacts:
                content = self.get_artifact(
                    trace_id,
                    metadata.artifact_id,
                )
                artifacts.append(
                    {
                        **_artifact_json(metadata),
                        "payload": _decode_artifact(content),
                    }
                )
            payload = {
                "trace": _trace_json(detail.trace),
                "spans": [_span_json(span) for span in detail.spans],
                "candidate_decisions": [
                    _decision_json(decision)
                    for decision in detail.candidate_decisions
                ],
                "artifacts": artifacts,
            }
            encoded = _json(payload).encode()
            if len(encoded) > self._export_limit_bytes:
                raise TraceArtifactLimitError("Trace 导出超过总字节上限。")
            return encoded

    @contextmanager
    def _export_guard(self, trace_id: str) -> Iterator[None]:
        """导出期间阻止 prune 删除同一 Trace。"""
        with self._lock:
            self._active_exports[trace_id] += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_exports[trace_id] -= 1
                if self._active_exports[trace_id] <= 0:
                    del self._active_exports[trace_id]

    def close(self) -> None:
        """幂等关闭数据库连接。

        Args:
            无参数；关闭当前 Store。

        Returns:
            无返回值。

        """
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise TraceStoreClosedError("Trace Store 未初始化或已关闭。")
        return connection

    @staticmethod
    def _mark_capture_incomplete(  # noqa: PLR0913
        connection: sqlite3.Connection,
        trace_id: str,
        *,
        reason: str,
        dropped_spans: int = 0,
        dropped_decisions: int = 0,
        queue_high_water: int = 0,
    ) -> None:
        cursor = connection.execute(
            "UPDATE traces SET capture_complete=0, "
            "capture_incomplete_reason=COALESCE(capture_incomplete_reason, ?), "
            "dropped_span_count=dropped_span_count+?, "
            "dropped_decision_count=dropped_decision_count+?, "
            "writer_queue_high_water=MAX(writer_queue_high_water, ?) "
            "WHERE trace_id=?",
            (
                reason,
                max(0, dropped_spans),
                max(0, dropped_decisions),
                max(0, queue_high_water),
                trace_id,
            ),
        )
        if cursor.rowcount != 1:
            raise TraceNotFoundError(trace_id)


def _canonical_database_path(path: Path) -> Path:
    """校验数据库路径并拒绝符号链接父目录。

    Args:
        path: 配置提供的 Trace 数据库文件路径。

    Returns:
        使用已解析真实父目录组成的绝对数据库路径。

    Raises:
        ValueError: 路径不是绝对路径、文件名无效、父目录不存在，
            或父目录解析结果发生变化。

    """
    if not path.is_absolute():
        raise ValueError("RAG_TRACE_DATABASE 必须是绝对路径。")
    if path.name in {"", ".", ".."}:
        raise ValueError("RAG_TRACE_DATABASE 文件名无效。")
    try:
        canonical_parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise ValueError("Trace 数据库父目录不存在。") from error
    if canonical_parent != path.parent.absolute():
        raise ValueError("Trace 数据库父目录不能经过符号链接。")
    return canonical_parent / path.name


def _secure_create_database(path: Path) -> None:
    """以私有权限创建数据库文件或验证现有文件权限。

    Args:
        path: 已通过父目录规范化检查的数据库路径。

    Returns:
        无返回值。

    Raises:
        ValueError: 目标是符号链接，或现有文件向组或其他用户开放。
        OSError: 独占创建或关闭新文件失败。

    """
    if path.is_symlink():
        raise ValueError("Trace 数据库不能是符号链接。")
    if not path.exists():
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        os.close(descriptor)
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError("现有 Trace 数据库权限必须为 0600。")


def _trace_from_row(row: sqlite3.Row) -> TraceRecord:
    feedback = row["feedback_useful"]
    return TraceRecord(
        trace_id=str(row["trace_id"]),
        schema_version=str(row["schema_version"]),
        mode=TraceMode(str(row["mode"])),
        created_at=_parse_timestamp(row["created_at"]),
        finished_at=(
            None
            if row["finished_at"] is None
            else _parse_timestamp(row["finished_at"])
        ),
        duration_ms=(
            None if row["duration_ms"] is None else int(row["duration_ms"])
        ),
        pipeline_fingerprint=str(row["pipeline_fingerprint"]),
        serving_fingerprint=str(row["serving_fingerprint"]),
        release_revision=str(row["release_revision"]),
        active_collection=str(row["active_collection"]),
        index_manifest_sha256=str(row["index_manifest_sha256"]),
        payload_schema_version=int(row["payload_schema_version"]),
        status=TraceStatus(str(row["status"])),
        refusal_code=(
            None if row["refusal_code"] is None else str(row["refusal_code"])
        ),
        error_code=(
            None if row["error_code"] is None else str(row["error_code"])
        ),
        feedback_useful=None if feedback is None else bool(feedback),
        capture_complete=bool(row["capture_complete"]),
        expires_at=_parse_timestamp(row["expires_at"]),
        kind=str(row["kind"]),
        project_id=_optional_text(row["project_id"]),
        knowledge_base_id=_optional_text(row["knowledge_base_id"]),
        owner_sha256=_optional_text(row["owner_sha256"]),
        request_id=_optional_text(row["request_id"]),
        job_id=_optional_text(row["job_id"]),
        document_id=_optional_text(row["document_id"]),
        revision_id=_optional_text(row["revision_id"]),
        profile_id=_optional_text(row["profile_id"]),
        index_fingerprint=_optional_text(row["index_fingerprint"]),
        source_revision=_optional_text(row["source_revision"]),
        capture_incomplete_reason=_optional_text(
            row["capture_incomplete_reason"]
        ),
        dropped_span_count=int(row["dropped_span_count"]),
        dropped_decision_count=int(row["dropped_decision_count"]),
        writer_queue_high_water=int(row["writer_queue_high_water"]),
    )


def _span_from_row(row: sqlite3.Row) -> SpanRecord:
    return SpanRecord(
        trace_id=str(row["trace_id"]),
        span_id=str(row["span_id"]),
        parent_span_id=(
            None
            if row["parent_span_id"] is None
            else str(row["parent_span_id"])
        ),
        sequence=int(row["sequence"]),
        name=str(row["name"]),
        kind=SpanKind(str(row["kind"])),
        started_at=_parse_timestamp(row["started_at"]),
        finished_at=(
            None
            if row["finished_at"] is None
            else _parse_timestamp(row["finished_at"])
        ),
        duration_ms=(
            None if row["duration_ms"] is None else int(row["duration_ms"])
        ),
        status=SpanStatus(str(row["status"])),
        reason_code=DecisionCode(str(row["reason_code"])),
        attributes=json.loads(str(row["attributes_json"])),
        input_artifact_id=(
            None
            if row["input_artifact_id"] is None
            else str(row["input_artifact_id"])
        ),
        output_artifact_id=(
            None
            if row["output_artifact_id"] is None
            else str(row["output_artifact_id"])
        ),
    )


def _decision_from_row(row: sqlite3.Row) -> CandidateDecision:
    return CandidateDecision(
        trace_id=str(row["trace_id"]),
        sequence=int(row["sequence"]),
        stage=str(row["stage"]),
        chunk_id=str(row["chunk_id"]),
        selected=bool(row["selected"]),
        reason_code=DecisionCode(str(row["reason_code"])),
        details=json.loads(str(row["details_json"])),
        candidate_id=_optional_text(row["candidate_id"]),
        evidence_id=_optional_text(row["evidence_id"]),
        channel=_optional_text(row["channel"]),
        rank=None if row["rank"] is None else int(row["rank"]),
        score_type=_optional_text(row["score_type"]),
        score=None if row["score"] is None else float(row["score"]),
        contribution=(
            None if row["contribution"] is None else float(row["contribution"])
        ),
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _artifact_metadata_from_row(row: sqlite3.Row) -> ArtifactMetadata:
    return ArtifactMetadata(
        artifact_id=str(row["artifact_id"]),
        trace_id=str(row["trace_id"]),
        kind=str(row["kind"]),
        media_type=str(row["media_type"]),
        sha256=str(row["sha256"]),
        original_bytes=int(row["original_bytes"]),
        compressed_bytes=int(row["compressed_bytes"]),
        created_at=_parse_timestamp(row["created_at"]),
    )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Trace 时间必须带时区。")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Trace 时间格式无效。")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Trace 时间必须带时区。")
    return parsed


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _trace_json(trace: TraceRecord) -> dict[str, object]:
    return {
        **trace.as_dict(),
        "mode": trace.mode.value,
        "created_at": _timestamp(trace.created_at),
        "finished_at": (
            None if trace.finished_at is None else _timestamp(trace.finished_at)
        ),
        "status": trace.status.value,
        "expires_at": _timestamp(trace.expires_at),
    }


def _span_json(span: SpanRecord) -> dict[str, object]:
    return {
        **span.as_dict(),
        "kind": span.kind.value,
        "started_at": _timestamp(span.started_at),
        "finished_at": (
            None if span.finished_at is None else _timestamp(span.finished_at)
        ),
        "status": span.status.value,
        "reason_code": span.reason_code.value,
    }


def _decision_json(decision: CandidateDecision) -> dict[str, object]:
    return {
        "trace_id": decision.trace_id,
        "sequence": decision.sequence,
        "stage": decision.stage,
        "chunk_id": decision.chunk_id,
        "selected": decision.selected,
        "reason_code": decision.reason_code.value,
        "details": decision.details,
        "candidate_id": decision.candidate_id,
        "evidence_id": decision.evidence_id,
        "channel": decision.channel,
        "rank": decision.rank,
        "score_type": decision.score_type,
        "score": decision.score,
        "contribution": decision.contribution,
    }


def _artifact_json(metadata: ArtifactMetadata) -> dict[str, object]:
    return {
        "artifact_id": metadata.artifact_id,
        "trace_id": metadata.trace_id,
        "kind": metadata.kind,
        "media_type": metadata.media_type,
        "sha256": metadata.sha256,
        "original_bytes": metadata.original_bytes,
        "compressed_bytes": metadata.compressed_bytes,
        "created_at": _timestamp(metadata.created_at),
    }


def _decode_artifact(content: ArtifactContent) -> object:
    if content.metadata.media_type == "application/json":
        return json.loads(content.payload)
    return content.payload.decode("utf-8")
