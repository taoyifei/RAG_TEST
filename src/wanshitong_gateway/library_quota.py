"""个人原件额度、持久上传任务及每人独立的解析槽。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from wanshitong_gateway.compat.adapters.stores.sqlite_connection import (
    SqliteConnectionFactory,
)

DEFAULT_MAX_BYTES = 250 * 1024 * 1024
DEFAULT_MAX_FILES = 50
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ACTIVE_PARSES = 1
MAX_QUEUED_PARSES = 5
SUBMISSION_LEASE_SECONDS = 300.0
PENDING_STATES = frozenset(
    {"staging", "queued", "submitting", "processing", "uncertain"}
)
ACTIVE_STATES = frozenset({"submitting", "processing", "uncertain"})
TERMINAL_PARSE_STATES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "canceled",
        "ready",
        "success",
        "succeeded",
        "indexed",
    }
)


def native_pending(item: dict[str, Any]) -> bool:
    """未完成状态继续占解析槽；删除状态不代表又启动了解析。"""
    return str(item.get("parse_status", "pending")).lower() not in (
        TERMINAL_PARSE_STATES | {"deleting", "deleted"}
    )


def native_revision(item: dict[str, Any]) -> str:
    """重试响应不明时以原生修订时间及状态识别真正的新尝试。"""
    return json.dumps(
        {
            key: item[key]
            for key in (
                "updated_at",
                "processed_at",
                "parse_status",
                "parse_attempt",
            )
            if key in item
        },
        sort_keys=True,
        default=str,
    )


@dataclass(frozen=True, slots=True)
class PersonalJob:
    """仅保存原件元数据和投递状态，文件保存在同卷的固定目录。"""

    job_id: str
    owner_id: str
    owner_name: str
    kind: str
    filename: str
    size_bytes: int
    state: str
    native_id: str | None
    kb_id: str | None
    error: str | None
    created_at: float
    updated_at: float
    lease_until: float
    native_revision: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PersonalJob:
        """把数据库记录转换成明确的任务值。"""
        return cls(**dict(row))


class PersonalQuotaStore:
    """在短 SQLite 写事务中预占原件额度，网络和文件写入不持有事务。"""

    def __init__(
        self, database_path: Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._connections = SqliteConnectionFactory(database_path)
        self._clock = clock
        self.spool_path = database_path.parent / "personal-uploads"
        self.spool_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connections.transaction(write=True) as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS library_personal_limits ("
                "owner_id TEXT PRIMARY KEY,max_bytes INTEGER NOT NULL,"
                "max_files INTEGER NOT NULL);"
                "CREATE TABLE IF NOT EXISTS library_personal_jobs ("
                "job_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,"
                "owner_name TEXT NOT NULL,kind TEXT NOT NULL,"
                "filename TEXT NOT NULL,size_bytes INTEGER NOT NULL,"
                "state TEXT NOT NULL,native_id TEXT,kb_id TEXT,error TEXT,"
                "created_at REAL NOT NULL,updated_at REAL NOT NULL,"
                "lease_until REAL NOT NULL DEFAULT 0,"
                "native_revision TEXT NOT NULL DEFAULT '');"
                "CREATE INDEX IF NOT EXISTS ix_personal_jobs_owner "
                "ON library_personal_jobs(owner_id,state,created_at);"
                "CREATE INDEX IF NOT EXISTS ix_personal_jobs_native "
                "ON library_personal_jobs(native_id);"
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_personal_one_active "
                "ON library_personal_jobs(owner_id) "
                "WHERE state IN ('submitting','processing','uncertain');"
            )
            if "native_revision" not in {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(library_personal_jobs)"
                )
            }:
                connection.execute(
                    "ALTER TABLE library_personal_jobs ADD COLUMN "
                    "native_revision TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _limits(
        connection: sqlite3.Connection, owner_id: str
    ) -> dict[str, int]:
        row = connection.execute(
            "SELECT max_bytes,max_files FROM library_personal_limits "
            "WHERE owner_id=?",
            (owner_id,),
        ).fetchone()
        return (
            dict(row)
            if row
            else {
                "max_bytes": DEFAULT_MAX_BYTES,
                "max_files": DEFAULT_MAX_FILES,
            }
        )

    def limits(self, owner_id: str) -> dict[str, int]:
        """返回额度配置，不读取或泄露对应用户的文件信息。"""
        with self._connections.transaction() as connection:
            return self._limits(connection, owner_id)

    def set_limits(
        self, owner_id: str, max_bytes: int, max_files: int
    ) -> dict[str, int]:
        """只调整未来的新增门槛，不移除原件或取消已接受的解析任务。"""
        if (
            isinstance(max_bytes, bool)
            or isinstance(max_files, bool)
            or not isinstance(max_bytes, int)
            or not isinstance(max_files, int)
            or not 1 <= max_bytes <= 2**63 - 1
            or not 1 <= max_files <= 2**63 - 1
        ):
            raise ValueError("个人额度必须是大于零的整数。")
        with self._connections.transaction(write=True) as connection:
            if (max_bytes, max_files) == (DEFAULT_MAX_BYTES, DEFAULT_MAX_FILES):
                connection.execute(
                    "DELETE FROM library_personal_limits WHERE owner_id=?",
                    (owner_id,),
                )
                return {"max_bytes": max_bytes, "max_files": max_files}
            connection.execute(
                "INSERT INTO library_personal_limits"
                "(owner_id,max_bytes,max_files) "
                "VALUES (?,?,?) ON CONFLICT(owner_id) DO UPDATE SET "
                "max_bytes=excluded.max_bytes,max_files=excluded.max_files",
                (owner_id, max_bytes, max_files),
            )
        return {"max_bytes": max_bytes, "max_files": max_files}

    @staticmethod
    def _jobs(
        connection: sqlite3.Connection, owner_id: str
    ) -> list[PersonalJob]:
        return [
            PersonalJob.from_row(row)
            for row in connection.execute(
                "SELECT * FROM library_personal_jobs "
                "WHERE owner_id=? AND state!='deleted' "
                "ORDER BY created_at,rowid",
                (owner_id,),
            )
        ]

    def jobs(self, owner_id: str) -> list[PersonalJob]:
        """任务列表始终按本人过滤。"""
        with self._connections.transaction() as connection:
            return self._jobs(connection, owner_id)

    def job(self, identifier: str) -> PersonalJob | None:
        """读取精确任务 ID，调用方仍须核对归属。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM library_personal_jobs WHERE job_id=?",
                (identifier,),
            ).fetchone()
        return PersonalJob.from_row(row) if row else None

    def owner_jobs(self) -> list[str]:
        """后台只轮询尚有待处理工作的用户，不扫描文件内容。"""
        with self._connections.transaction() as connection:
            return [
                str(row["owner_id"])
                for row in connection.execute(
                    "SELECT DISTINCT owner_id FROM library_personal_jobs "
                    "WHERE state IN ('staging','queued','submitting',"
                    "'processing','uncertain') ORDER BY owner_id"
                )
            ]

    @staticmethod
    def _usage(
        jobs: Sequence[PersonalJob], native_items: Sequence[dict[str, Any]]
    ) -> tuple[int, int, int, int]:
        sizes = {
            str(item["id"]): max(0, int(item.get("file_size") or 0))
            for item in native_items
        }
        pending_ids = {
            str(item["id"]) for item in native_items if native_pending(item)
        }
        active_ids: set[str] = set(pending_ids)
        queued_ids: set[str] = set()
        for job in jobs:
            key = job.native_id or job.job_id
            if job.kind == "upload":
                # 原生快照可能比刚投递的任务旧，也可能尚未回填 file_size。
                # 保留原始预占行，按同一 native_id 合并而非相加或提前释放。
                sizes[key] = max(sizes.get(key, 0), job.size_bytes)
            if job.state in ACTIVE_STATES:
                active_ids.add(key)
            elif job.state in {"staging", "queued"}:
                queued_ids.add(key)
        queued_ids.difference_update(active_ids)
        # 空闲时最早的一份是待派发的活动槽，其余才是额外等待项。
        # 预占和写盘中的文件仍计入总数，六个并发请求显示1份准备+5份等待。
        if not active_ids and queued_ids:
            active_ids.add(queued_ids.pop())
        return sum(sizes.values()), len(sizes), len(active_ids), len(queued_ids)

    def summary(
        self, owner_id: str, native_items: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        """页面和写入门禁采用同一计量：预占也占容量及文件份数。"""
        with self._connections.transaction() as connection:
            limits = self._limits(connection, owner_id)
            used_bytes, used_files, active, queued = self._usage(
                self._jobs(connection, owner_id), native_items
            )
        full = (
            used_bytes >= limits["max_bytes"]
            or used_files >= limits["max_files"]
        )
        busy = active + queued >= MAX_ACTIVE_PARSES + MAX_QUEUED_PARSES
        reason = (
            "个人资料已达到容量或份数上限，请删除不需要的资料或联系超级管理员调整额度。"
            if full
            else "已有资料正在处理，等待队列已满，请完成后再上传。"
            if busy
            else None
        )
        return {
            **limits,
            "used_bytes": used_bytes,
            "used_files": used_files,
            "max_file_bytes": MAX_FILE_BYTES,
            "warning": used_bytes * 5 >= limits["max_bytes"] * 4
            or used_files * 5 >= limits["max_files"] * 4,
            "full": full,
            "active_parses": active,
            "queued_parses": queued,
            "max_active_parses": MAX_ACTIVE_PARSES,
            "max_queued_parses": MAX_QUEUED_PARSES,
            "upload_allowed": not full and not busy,
            "upload_block_reason": reason,
        }

    def reserve_upload(
        self,
        owner_id: str,
        owner_name: str,
        filename: str,
        size_bytes: int,
        native_items: Sequence[dict[str, Any]],
    ) -> PersonalJob:
        """在同一写事务中复核额度和队列并预占，阻止多窗口越过上限。"""
        now = self._clock()
        job_id = str(uuid4())
        with self._connections.transaction(write=True) as connection:
            limits = self._limits(connection, owner_id)
            used_bytes, used_files, active, queued = self._usage(
                self._jobs(connection, owner_id), native_items
            )
            if used_bytes + size_bytes > limits["max_bytes"]:
                raise HTTPException(
                    409,
                    "个人资料容量不足，请先删除资料或联系超级管理员调整额度。",
                )
            if used_files + 1 > limits["max_files"]:
                raise HTTPException(
                    409,
                    "个人资料已达到文件份数上限，请先删除资料或联系超级管理员调整额度。",
                )
            self._check_queue(active, queued)
            connection.execute(
                "INSERT INTO library_personal_jobs "
                "(job_id,owner_id,owner_name,kind,filename,size_bytes,state,"
                "created_at,updated_at) "
                "VALUES (?,?,?,'upload',?,?,'staging',?,?)",
                (job_id, owner_id, owner_name, filename, size_bytes, now, now),
            )
        job = self.job(job_id)
        if job is None:
            raise RuntimeError("个人原件预占记录未保存。")
        return job

    @staticmethod
    def _check_queue(active: int, queued: int) -> None:
        if active + queued >= MAX_ACTIVE_PARSES + MAX_QUEUED_PARSES:
            raise HTTPException(
                429, "您已有资料正在处理，最多可再等待5份，请稍后再试。"
            )

    def reserve_retry(
        self,
        owner_id: str,
        owner_name: str,
        item: dict[str, Any],
        native_items: Sequence[dict[str, Any]],
    ) -> PersonalJob:
        """已有原件重试仅占解析槽，容量已满也不产生额外原件预占。"""
        native_id = str(item["id"])
        now = self._clock()
        job_id = str(uuid4())
        with self._connections.transaction(write=True) as connection:
            jobs = self._jobs(connection, owner_id)
            if native_pending(item) or any(
                job.native_id == native_id and job.state in PENDING_STATES
                for job in jobs
            ):
                raise HTTPException(
                    409, "这份资料已在处理，请等待当前解析结束。"
                )
            _, _, active, queued = self._usage(jobs, native_items)
            self._check_queue(active, queued)
            connection.execute(
                "INSERT INTO library_personal_jobs "
                "(job_id,owner_id,owner_name,kind,filename,size_bytes,state,"
                "native_id,kb_id,created_at,updated_at) "
                "VALUES (?,?,?,'retry',?,0,'queued',?,?,?,?)",
                (
                    job_id,
                    owner_id,
                    owner_name,
                    str(item.get("file_name") or item.get("title") or "资料"),
                    native_id,
                    str(item["knowledge_base_id"]),
                    now,
                    now,
                ),
            )
        job = self.job(job_id)
        if job is None:
            raise RuntimeError("个人原件重试记录未保存。")
        return job

    def set_state(
        self, job_id: str, state: str, *, error: str | None = None
    ) -> None:
        """终结或展示状态不覆盖本人已经提交的删除。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE library_personal_jobs SET state=?,error=?,updated_at=? "
                "WHERE job_id=? AND state!='deleted'",
                (state, error, self._clock(), job_id),
            )

    def accept_native(self, job_id: str, item: dict[str, Any]) -> None:
        """记录原生接收映射；失败也保留原件额度，直到确认删除。"""
        state = (
            "processing"
            if native_pending(item)
            else (
                "failed"
                if str(item.get("parse_status")) == "failed"
                else "completed"
            )
        )
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE library_personal_jobs SET native_id=?,kb_id=?,"
                "state=?,error=?,updated_at=?,lease_until=0 "
                "WHERE job_id=? AND state!='deleted'",
                (
                    str(item["id"]),
                    str(item.get("knowledge_base_id") or ""),
                    state,
                    "解析失败，请重试。" if state == "failed" else None,
                    self._clock(),
                    job_id,
                ),
            )

    def set_retry_revision(self, job_id: str, item: dict[str, Any]) -> None:
        """在真正投递重试前记录原件版本，而非采用排队时的旧快照。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE library_personal_jobs SET native_revision=? "
                "WHERE job_id=? AND kind='retry' AND state='submitting'",
                (native_revision(item), job_id),
            )

    def claim_next(
        self, owner_id: str, native_items: Sequence[dict[str, Any]]
    ) -> PersonalJob | None:
        """任一未确认的投递或原生处理中资料都会挡住本人的下一份。"""
        now = self._clock()
        with self._connections.transaction(write=True) as connection:
            jobs = self._jobs(connection, owner_id)
            if any(job.state in ACTIVE_STATES for job in jobs) or any(
                native_pending(item) for item in native_items
            ):
                return None
            job = next((job for job in jobs if job.state == "queued"), None)
            if job is None:
                return None
            connection.execute(
                "UPDATE library_personal_jobs SET state='submitting',"
                "updated_at=?,lease_until=? WHERE job_id=? AND state='queued'",
                (now, now + SUBMISSION_LEASE_SECONDS, job.job_id),
            )
        return self.job(job.job_id)

    def reset_retry(
        self, job: PersonalJob, native_items: Sequence[dict[str, Any]]
    ) -> None:
        """本人显式重试暂存原件，未确认的请求需先越过恢复冷却期。"""
        with self._connections.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT * FROM library_personal_jobs WHERE job_id=?",
                (job.job_id,),
            ).fetchone()
            if current is None or current["state"] == "deleted":
                raise HTTPException(404, "资料不存在或已删除。")
            if current["state"] in {
                "queued",
                "staging",
                "submitting",
                "processing",
            }:
                raise HTTPException(
                    409, "这份资料已在处理，请等待当前解析结束。"
                )
            if (
                current["state"] == "uncertain"
                and self._clock() < current["lease_until"]
            ):
                raise HTTPException(
                    409, "原件投递结果正在确认，请稍后再试；原件已保留。"
                )
            other_jobs = [
                entry
                for entry in self._jobs(connection, job.owner_id)
                if entry.job_id != job.job_id
            ]
            _, _, active, queued = self._usage(other_jobs, native_items)
            if current["state"] == "uncertain" and (
                any(entry.state in ACTIVE_STATES for entry in other_jobs)
                or any(native_pending(item) for item in native_items)
            ):
                raise HTTPException(
                    409, "原件投递结果尚未确认，请等待当前处理结束。"
                )
            self._check_queue(active, queued)
            connection.execute(
                "UPDATE library_personal_jobs SET state='queued',error=NULL,"
                "updated_at=?,lease_until=0 WHERE job_id=?",
                (self._clock(), job.job_id),
            )

    def mark_deleted(self, identifier: str, owner_id: str) -> list[str]:
        """只有已确认删除的任务或原生原件才释放额度并交回暂存路径。"""
        with self._connections.transaction(write=True) as connection:
            identifiers = [
                str(row["job_id"])
                for row in connection.execute(
                    "SELECT job_id FROM library_personal_jobs WHERE owner_id=? "
                    "AND (job_id=? OR native_id=?) AND state!='deleted'",
                    (owner_id, identifier, identifier),
                )
            ]
            connection.execute(
                "UPDATE library_personal_jobs SET state='deleted',error=NULL,"
                "updated_at=? WHERE owner_id=? AND (job_id=? OR native_id=?)",
                (self._clock(), owner_id, identifier, identifier),
            )
        return identifiers

    def forget_staging(self, job_id: str) -> None:
        """未接受的上传写盘失败时回滚预占，不留下可见空文件。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM library_personal_jobs "
                "WHERE job_id=? AND state='staging'",
                (job_id,),
            )
