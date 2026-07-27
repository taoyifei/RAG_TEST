"""SQLite 状态表的枚举与不可变快照。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "ActiveSource",
    "Job",
    "JobKind",
    "JobState",
    "MediaReference",
    "OcrResult",
    "SourceVersion",
    "VersionState",
]


class JobKind(StrEnum):
    """索引任务类型。"""

    INCREMENTAL = "incremental"
    FULL = "full"


class JobState(StrEnum):
    """索引任务状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class VersionState(StrEnum):
    """来源版本状态。"""

    STAGING = "staging"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Job:
    """任务表中的一条不可变快照。"""

    job_id: str
    idempotency_key: str
    kind: JobKind
    state: JobState
    pipeline_fingerprint: str
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempt: int
    error_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """来源版本表中的一条不可变快照。"""

    source_id: str
    doc_version: str
    content_sha256: str
    source_path: str
    pipeline_fingerprint: str
    state: VersionState
    job_id: str
    chunk_count: int | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ActiveSource:
    """当前可查询来源的身份与版本。"""

    source_id: str
    current_path: str
    content_sha256: str
    doc_version: str


@dataclass(frozen=True, slots=True)
class OcrResult:
    """按媒体与 OCR revision 去重的结果。"""

    media_sha256: str
    ocr_revision: str
    state: str
    text: str | None
    confidence: float | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class MediaReference:
    """DOCX 中每一次图片引用及其 OCR 状态。"""

    source_id: str
    doc_version: str
    element_id: str
    media_sha256: str
    media_type: str
    media_name: str | None
    locator: str
    ocr_revision: str
    state: str
    error_code: str | None


def _job_from_row(row: sqlite3.Row) -> Job:
    """把 SQLite 行转换为任务快照。"""
    expires = row["lease_expires_at"]
    return Job(
        job_id=str(row["job_id"]),
        idempotency_key=str(row["idempotency_key"]),
        kind=JobKind(str(row["kind"])),
        state=JobState(str(row["state"])),
        pipeline_fingerprint=str(row["pipeline_fingerprint"]),
        lease_owner=(
            None if row["lease_owner"] is None else str(row["lease_owner"])
        ),
        lease_expires_at=(
            None if expires is None else datetime.fromisoformat(str(expires))
        ),
        attempt=int(row["attempt"]),
        error_code=(
            None if row["error_code"] is None else str(row["error_code"])
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def _version_from_row(row: sqlite3.Row) -> SourceVersion:
    """把 SQLite 行转换为来源版本快照。"""
    return SourceVersion(
        source_id=str(row["source_id"]),
        doc_version=str(row["doc_version"]),
        content_sha256=str(row["content_sha256"]),
        source_path=str(row["source_path"]),
        pipeline_fingerprint=str(row["pipeline_fingerprint"]),
        state=VersionState(str(row["state"])),
        job_id=str(row["job_id"]),
        chunk_count=(
            None if row["chunk_count"] is None else int(row["chunk_count"])
        ),
        error_code=(
            None if row["error_code"] is None else str(row["error_code"])
        ),
    )


def _require_row(row: sqlite3.Row | None) -> sqlite3.Row:
    """要求 SQLite 操作返回一行。"""
    if row is None:
        raise RuntimeError("SQLite 操作未返回预期行。")
    return row
