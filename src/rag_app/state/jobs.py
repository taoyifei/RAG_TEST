"""SQLite 基础连接与可恢复任务租约。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rag_app.state.models import (
    Job,
    JobKind,
    JobState,
    _job_from_row,
    _require_row,
)
from rag_app.state.schema import SCHEMA


class JobStore:
    """管理 WAL 配置与单写者任务租约。"""

    def __init__(self, path: Path) -> None:
        """保存 SQLite 文件位置。

        Args:
            path: SQLite 数据库文件。

        Returns:
            无返回值。

        """
        self.path = path

    def initialize(self) -> None:
        """初始化 WAL 数据库与约束。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(SCHEMA)

    def pragmas(self) -> dict[str, int | str]:
        """读取恢复相关 SQLite 配置。

        Args:
            无参数。

        Returns:
            journal_mode、foreign_keys 与 synchronous。

        """
        with self._connect() as connection:
            journal_mode = connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            foreign_keys = connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()[0]
            synchronous = connection.execute(
                "PRAGMA synchronous"
            ).fetchone()[0]
        return {
            "journal_mode": str(journal_mode),
            "foreign_keys": int(foreign_keys),
            "synchronous": int(synchronous),
        }

    def create_job(
        self,
        *,
        idempotency_key: str,
        kind: JobKind,
        pipeline_fingerprint: str,
    ) -> Job:
        """按幂等键创建或返回原任务。

        Args:
            idempotency_key: 调用方稳定生成的任务幂等键。
            kind: 全量或增量任务。
            pipeline_fingerprint: 本次任务使用的 pipeline 指纹。

        Returns:
            新建或已存在的同一任务。

        """
        now = _utc_now_text()
        job_id = f"job_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    job_id, idempotency_key, kind, state,
                    pipeline_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    kind.value,
                    JobState.PENDING.value,
                    pipeline_fingerprint,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _job_from_row(_require_row(row))

    def count_jobs(self) -> int:
        """返回任务总数。

        Args:
            无参数。

        Returns:
            jobs 表行数。

        """
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        return int(_require_row(row)[0])

    def list_jobs(self) -> tuple[Job, ...]:
        """列出全部任务的不可变快照。

        Args:
            无参数。

        Returns:
            按创建时间和 job ID 稳定排序的任务元组。

        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                ORDER BY created_at, job_id
                """
            ).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    def claim_next_job(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> Job | None:
        """原子领取 pending 或租约过期的 running 任务。

        Args:
            worker_id: 单索引写入者身份。
            now: 带时区的当前时间。
            lease_seconds: 正数租约秒数。

        Returns:
            领取后的任务；没有可领取任务时返回 None。

        Raises:
            ValueError: now 无时区或租约不为正数。

        """
        _require_aware(now)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正数。")
        now_text = now.astimezone(UTC).isoformat()
        expires_text = (
            now.astimezone(UTC) + timedelta(seconds=lease_seconds)
        ).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = ?
                   OR (state = ? AND lease_expires_at <= ?)
                ORDER BY created_at, job_id
                LIMIT 1
                """,
                (
                    JobState.PENDING.value,
                    JobState.RUNNING.value,
                    now_text,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, lease_owner = ?, lease_expires_at = ?,
                    attempt = attempt + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobState.RUNNING.value,
                    worker_id,
                    expires_text,
                    now_text,
                    row["job_id"],
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()
            connection.commit()
        return _job_from_row(_require_row(claimed))

    def renew_job_lease(
        self,
        *,
        job_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> Job:
        """延长当前 worker 持有的运行中任务租约。

        Args:
            job_id: 运行中任务标识。
            worker_id: 当前租约所有者。
            now: 带时区的当前时间。
            lease_seconds: 正数租约秒数。

        Returns:
            续租后的任务快照。

        Raises:
            ValueError: now 无时区或租约不为正数。
            LookupError: worker 不持有该运行中租约。

        """
        _require_aware(now)
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须为正数。")
        now_utc = now.astimezone(UTC)
        expires_at = now_utc + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND state = ? AND lease_owner = ?
                """,
                (
                    expires_at.isoformat(),
                    now_utc.isoformat(),
                    job_id,
                    JobState.RUNNING.value,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError("worker 不持有该运行中任务租约。")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return _job_from_row(_require_row(row))

    def finish_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        error_code: str | None,
    ) -> None:
        """由租约所有者把运行中任务置为终态。

        Args:
            job_id: 运行中任务标识。
            worker_id: 当前租约所有者。
            error_code: None 表示成功，否则记录无原文错误码并失败。

        Returns:
            无返回值。

        Raises:
            LookupError: worker 不持有该运行中租约。

        """
        terminal_state = (
            JobState.SUCCEEDED if error_code is None else JobState.FAILED
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, error_code = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND state = ? AND lease_owner = ?
                """,
                (
                    terminal_state.value,
                    error_code,
                    _utc_now_text(),
                    job_id,
                    JobState.RUNNING.value,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError("worker 不持有该运行中任务租约。")

    def get_job(self, job_id: str) -> Job:
        """读取任务快照。

        Args:
            job_id: 任务标识。

        Returns:
            不可变任务快照。

        Raises:
            LookupError: 任务不存在。

        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise LookupError("任务不存在。")
        return _job_from_row(row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


def _utc_now_text() -> str:
    """返回 UTC ISO8601 时间。"""
    return datetime.now(UTC).isoformat()


def _require_aware(value: datetime) -> None:
    """拒绝无时区 datetime。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime 必须包含时区。")
