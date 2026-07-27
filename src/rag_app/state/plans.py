"""增量同步计划与可重入任务项持久化。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rag_app.index.planner import SyncAction, SyncActionKind, SyncPlan
from rag_app.state.jobs import _utc_now_text

__all__ = ["StoredSyncItem", "SyncItemState", "SyncPlanStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_plans (
    job_id TEXT PRIMARY KEY,
    plan_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS job_items (
    item_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source_id TEXT,
    previous_path TEXT,
    source_path TEXT,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, ordinal),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_items_state
ON job_items(job_id, state, ordinal);
"""


class SyncItemState(StrEnum):
    """同步任务项状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StoredSyncItem:
    """一个持久、可重试的同步动作。"""

    item_id: str
    job_id: str
    ordinal: int
    action: SyncAction
    state: SyncItemState
    attempt: int
    error_code: str | None


class SyncPlanStore:
    """保存计划，并逐项重入单 worker 执行。"""

    def __init__(self, database_path: Path) -> None:
        """保存与 jobs 共享的 SQLite 路径。

        Args:
            database_path: 已初始化 jobs 表的 SQLite 文件。

        """
        self._database_path = database_path

    def initialize(self) -> None:
        """初始化同步计划 schema。"""
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def save(self, job_id: str, plan: SyncPlan) -> None:
        """按 job ID 幂等保存不可变计划。

        Args:
            job_id: 父索引任务。
            plan: 规范排序并带摘要的同步计划。

        Raises:
            ValueError: 同一 job 已绑定其他计划摘要。

        """
        now = _utc_now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT plan_digest FROM job_plans WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["plan_digest"]) != plan.digest:
                    connection.rollback()
                    raise ValueError("同一 job 的 plan digest 不允许变化。")
                connection.commit()
                return
            connection.execute(
                """
                INSERT INTO job_plans (job_id, plan_digest, created_at)
                VALUES (?, ?, ?)
                """,
                (job_id, plan.digest, now),
            )
            for ordinal, action in enumerate(plan.actions):
                initial_state = (
                    SyncItemState.SUCCEEDED
                    if action.kind == SyncActionKind.UNCHANGED
                    else SyncItemState.PENDING
                )
                connection.execute(
                    """
                    INSERT INTO job_items (
                        item_id, job_id, ordinal, kind, source_id,
                        previous_path, source_path, content_sha256,
                        state, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _item_id(job_id, ordinal, plan.digest),
                        job_id,
                        ordinal,
                        action.kind.value,
                        action.source_id,
                        action.previous_path,
                        action.source_path,
                        action.content_sha256,
                        initial_state.value,
                        now,
                    ),
                )
            connection.commit()

    def has_plan(self, job_id: str) -> bool:
        """判断 job 是否已经冻结同步计划。

        Args:
            job_id: 父索引任务。

        Returns:
            即使计划没有动作，只要摘要已持久化就返回 True。

        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM job_plans WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return row is not None

    def claim_next(self, job_id: str) -> StoredSyncItem | None:
        """领取最早 pending 项，或重领进程中断时的 running 项。

        Args:
            job_id: 已由单 worker 持有租约的任务。

        Returns:
            running 任务项；全部完成或已有 failed 项时返回 None。

        """
        now = _utc_now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM job_items
                WHERE job_id = ? AND state IN (?, ?)
                ORDER BY ordinal
                LIMIT 1
                """,
                (
                    job_id,
                    SyncItemState.RUNNING.value,
                    SyncItemState.PENDING.value,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE job_items
                SET state = ?, attempt = attempt + 1, updated_at = ?
                WHERE item_id = ?
                """,
                (
                    SyncItemState.RUNNING.value,
                    now,
                    row["item_id"],
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM job_items WHERE item_id = ?",
                (row["item_id"],),
            ).fetchone()
            connection.commit()
        return _item_from_row(_require_row(claimed))

    def succeed(self, item_id: str) -> None:
        """标记 running 任务项成功。

        Args:
            item_id: 持久任务项标识。

        """
        self._finish(item_id, SyncItemState.SUCCEEDED, error_code=None)

    def fail(self, item_id: str, *, error_code: str) -> None:
        """标记 running 任务项失败。

        Args:
            item_id: 持久任务项标识。
            error_code: 不含原文的稳定错误码。

        """
        self._finish(
            item_id,
            SyncItemState.FAILED,
            error_code=error_code,
        )

    def retry_or_fail(
        self,
        item_id: str,
        *,
        error_code: str,
        max_attempts: int,
    ) -> SyncItemState:
        """在尝试上限内重排队，否则标记失败。

        Args:
            item_id: 当前 running 任务项。
            error_code: 不含原文的稳定错误码。
            max_attempts: 正数尝试上限。

        Returns:
            PENDING 或 FAILED 新状态。

        Raises:
            ValueError: max_attempts 不为正数。
            LookupError: 任务项不处于 running。

        """
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须为正数。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT attempt FROM job_items
                WHERE item_id = ? AND state = ?
                """,
                (item_id, SyncItemState.RUNNING.value),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise LookupError("只有 running 任务项可以重排队。")
            target = (
                SyncItemState.PENDING
                if int(row["attempt"]) < max_attempts
                else SyncItemState.FAILED
            )
            connection.execute(
                """
                UPDATE job_items
                SET state = ?, error_code = ?, updated_at = ?
                WHERE item_id = ?
                """,
                (
                    target.value,
                    error_code,
                    _utc_now_text(),
                    item_id,
                ),
            )
            connection.commit()
        return target

    def has_failures(self, job_id: str) -> bool:
        """判断计划是否含终态失败项。

        Args:
            job_id: 父索引任务。

        Returns:
            至少一个任务项 failed 时为 True。

        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM job_items
                WHERE job_id = ? AND state = ?
                LIMIT 1
                """,
                (job_id, SyncItemState.FAILED.value),
            ).fetchone()
        return row is not None

    def list_items(self, job_id: str) -> tuple[StoredSyncItem, ...]:
        """按序列出一个计划的全部任务项。

        Args:
            job_id: 父索引任务。

        Returns:
            不可变任务项快照。

        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_items
                WHERE job_id = ?
                ORDER BY ordinal
                """,
                (job_id,),
            ).fetchall()
        return tuple(_item_from_row(row) for row in rows)

    def _finish(
        self,
        item_id: str,
        state: SyncItemState,
        *,
        error_code: str | None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE job_items
                SET state = ?, error_code = ?, updated_at = ?
                WHERE item_id = ? AND state = ?
                """,
                (
                    state.value,
                    error_code,
                    _utc_now_text(),
                    item_id,
                    SyncItemState.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError("只有 running 任务项可以结束。")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


def _item_id(job_id: str, ordinal: int, plan_digest: str) -> str:
    payload = f"{job_id}\x00{ordinal}\x00{plan_digest}".encode()
    return f"item_{hashlib.sha256(payload).hexdigest()[:32]}"


def _item_from_row(row: sqlite3.Row) -> StoredSyncItem:
    return StoredSyncItem(
        item_id=str(row["item_id"]),
        job_id=str(row["job_id"]),
        ordinal=int(row["ordinal"]),
        action=SyncAction(
            kind=SyncActionKind(str(row["kind"])),
            source_id=(
                None if row["source_id"] is None else str(row["source_id"])
            ),
            previous_path=(
                None
                if row["previous_path"] is None
                else str(row["previous_path"])
            ),
            source_path=(
                None
                if row["source_path"] is None
                else str(row["source_path"])
            ),
            content_sha256=str(row["content_sha256"]),
        ),
        state=SyncItemState(str(row["state"])),
        attempt=int(row["attempt"]),
        error_code=(
            None if row["error_code"] is None else str(row["error_code"])
        ),
    )


def _require_row(row: sqlite3.Row | None) -> sqlite3.Row:
    if row is None:
        raise RuntimeError("SQLite 未返回预期同步任务项。")
    return row
