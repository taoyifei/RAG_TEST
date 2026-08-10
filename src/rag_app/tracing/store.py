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
from datetime import UTC, datetime, timedelta
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
    "TRACE_SCHEMA_VERSION",
    "ArtifactExpiredError",
    "ArtifactNotFoundError",
    "TraceArtifactLimitError",
    "TraceNotFoundError",
    "TraceStore",
    "TraceStoreClosedError",
]

_DEFAULT_ARTIFACT_LIMIT = 5 * 1024 * 1024
TRACE_SCHEMA_VERSION = 2
_MIN_QUESTION_RETENTION_SECONDS = 60
_MAX_QUESTION_RETENTION_SECONDS = 604_800
_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    question_text TEXT,
    question_sha256 TEXT,
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

    def __init__(
        self,
        database_path: Path,
        *,
        artifact_limit_bytes: int = _DEFAULT_ARTIFACT_LIMIT,
        busy_timeout_ms: int = 5000,
    ) -> None:
        """保存独立数据库路径和硬容量。

        Args:
            database_path: 不与状态库共用的 SQLite 文件。
            artifact_limit_bytes: 单条 Trace 的原始 artifact 字节上限。
            busy_timeout_ms: SQLite 锁等待上限。

        Raises:
            ValueError: 容量、超时或路径无效。

        """
        if artifact_limit_bytes <= 0 or busy_timeout_ms <= 0:
            raise ValueError("Trace 容量和 busy timeout 必须为正数。")
        self._path = _canonical_database_path(database_path)
        self._artifact_limit_bytes = artifact_limit_bytes
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

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
            try:
                _initialize_schema(connection)
            except Exception:
                connection.close()
                raise
            self._connection = connection

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
                    trace_id, schema_version, mode, question_text,
                    question_sha256, created_at,
                    finished_at, duration_ms, pipeline_fingerprint,
                    serving_fingerprint, release_revision,
                    active_collection, index_manifest_sha256,
                    payload_schema_version, status, refusal_code,
                    error_code, feedback_useful, capture_complete,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?,
                          NULL, NULL, NULL, ?, ?)
                """,
                (
                    trace.trace_id,
                    trace.schema_version,
                    trace.mode.value,
                    trace.question_text,
                    trace.question_sha256,
                    _timestamp(trace.created_at),
                    trace.pipeline_fingerprint,
                    trace.serving_fingerprint,
                    trace.release_revision,
                    trace.active_collection,
                    trace.index_manifest_sha256,
                    trace.payload_schema_version,
                    trace.status.value,
                    int(trace.capture_complete),
                    _timestamp(trace.expires_at),
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
                "SELECT created_at FROM traces WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            if row is None:
                raise TraceNotFoundError(trace_id)
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
                WHERE trace_id=?
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

    def put_span(self, span: SpanRecord) -> None:
        """插入或关闭同一身份的 span。

        Args:
            span: RUNNING 或终态 span 记录。

        Returns:
            无返回值。

        """
        with self._lock:
            connection = self._require_connection()
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
            connection.execute(
                """
                INSERT INTO candidate_decisions (
                    trace_id, sequence, stage, chunk_id, selected,
                    reason_code, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.trace_id,
                    decision.sequence,
                    decision.stage,
                    decision.chunk_id,
                    int(decision.selected),
                    decision.reason_code.value,
                    _json(decision.details),
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
            if used + len(payload) > self._artifact_limit_bytes:
                self._mark_capture_incomplete(connection, trace_id)
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

    def mark_capture_incomplete(self, trace_id: str) -> None:
        """把 Trace 标记为未完整捕获。

        Args:
            trace_id: 待标记的 Trace ID。

        Returns:
            无返回值。

        """
        with self._lock:
            connection = self._require_connection()
            self._mark_capture_incomplete(connection, trace_id)
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

    def prune(
        self,
        *,
        now: datetime,
        question_retention_seconds: int | None = None,
    ) -> int:
        """清空到期问题正文并删除已超过 mode 到期时点的 Trace。

        Args:
            now: 带时区的固定清理时点。
            question_retention_seconds: 问题正文独立保留秒数；为空时
                只执行既有 Trace 删除。

        Returns:
            级联删除的根 Trace 数量。

        """
        if question_retention_seconds is not None and not (
            _MIN_QUESTION_RETENTION_SECONDS
            <= question_retention_seconds
            <= _MAX_QUESTION_RETENTION_SECONDS
        ):
            raise ValueError("问题正文保留期必须在 60 秒到 7 天之间。")
        with self._lock:
            connection = self._require_connection()
            if question_retention_seconds is not None:
                question_cutoff = now - timedelta(
                    seconds=question_retention_seconds
                )
                connection.execute(
                    """
                    UPDATE traces
                    SET question_text=NULL
                    WHERE question_text IS NOT NULL AND created_at<=?
                    """,
                    (_timestamp(question_cutoff),),
                )
            cursor = connection.execute(
                "DELETE FROM traces WHERE expires_at<=?",
                (_timestamp(now),),
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
        return _json(payload).encode()

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
    def _mark_capture_incomplete(
        connection: sqlite3.Connection,
        trace_id: str,
    ) -> None:
        cursor = connection.execute(
            "UPDATE traces SET capture_complete=0 WHERE trace_id=?",
            (trace_id,),
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


def _initialize_schema(connection: sqlite3.Connection) -> None:
    """创建 Trace v2 schema 或原子升级完整的 v1 schema。

    Args:
        connection: 已配置安全 PRAGMA 的独占 Store 连接。

    Raises:
        ValueError: schema 版本未知或问题字段处于部分迁移状态。

    """
    trace_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='traces'"
    ).fetchone()
    if trace_table is None:
        connection.executescript(_SCHEMA)
        connection.execute(f"PRAGMA user_version={TRACE_SCHEMA_VERSION}")
        connection.commit()
        return
    connection.executescript(_SCHEMA)
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(traces)").fetchall()
    }
    question_columns = {"question_text", "question_sha256"} & columns
    version_row = connection.execute("PRAGMA user_version").fetchone()
    version = 0 if version_row is None else int(version_row[0])
    if question_columns and question_columns != {
        "question_text",
        "question_sha256",
    }:
        raise ValueError("Trace schema 问题字段处于部分迁移状态。")
    if version not in {0, 1, TRACE_SCHEMA_VERSION}:
        raise ValueError("Trace schema 版本不受支持。")
    if not question_columns:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "ALTER TABLE traces ADD COLUMN question_text TEXT"
            )
            connection.execute(
                "ALTER TABLE traces ADD COLUMN question_sha256 TEXT"
            )
            connection.execute(f"PRAGMA user_version={TRACE_SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return
    connection.execute(f"PRAGMA user_version={TRACE_SCHEMA_VERSION}")
    connection.commit()


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
        question_text=(
            None if row["question_text"] is None else str(row["question_text"])
        ),
        question_sha256=(
            None
            if row["question_sha256"] is None
            else str(row["question_sha256"])
        ),
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
    )


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
