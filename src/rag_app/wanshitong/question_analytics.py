"""F05 后台问题榜：有界 History 快照、保守分组与持久运行状态。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import threading
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import cast
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.core.errors import NotFound
from rag_app.product.feedback import normalize_trace_id
from rag_app.product.query_history import (
    ProductQueryHistory,
    _source_unavailable_reason,
)
from rag_app.product.usage_audit import usage_audit_view

_LOGGER = logging.getLogger(__name__)
_SCHEMA_VERSION = "wst-question-stats-v1"
_NORMALIZER_REVISION = "nfc-conservative-v1"
_ALIAS_REVISION = "none"
_POLICY_REVISION = "f05-v1"
_BUSINESS_TIMEZONE = "Asia/Shanghai"
_WINDOW_DAYS = 7
_MAX_ROWS = 5_000
_MAX_BYTES = 64 * 1024 * 1024
_READ_SECONDS = 5.0
_PUBLISH_REVISION_SECONDS = 1.0
_MAX_PAGE_SIZE = 100
_MAX_SAMPLES = 3
_TERMINAL = frozenset({"ANSWERED", "REFUSED", "FAILED", "INTERRUPTED"})
_FAILED = frozenset({"FAILED", "INTERRUPTED"})
_QUESTION_ENDINGS = "?？.。"

_SNAPSHOT_SQL = (
    "SELECT h.*, f.useful AS feedback_useful, "
    "f.updated_at AS feedback_updated_at, "
    "d.reason_detail AS feedback_reason_detail, "
    "d.updated_at AS detail_updated_at, "
    "COALESCE(d.feedback_revision, 1) AS feedback_revision, "
    "r.review_status AS review_status, "
    "r.root_cause AS review_root_cause, "
    "r.reviewed_feedback_revision AS reviewed_feedback_revision "
    "FROM query_history h LEFT JOIN product_feedback f "
    "ON f.trace_id=CASE WHEN substr(h.trace_id,1,6)='trace_' "
    "THEN h.trace_id ELSE 'trace_' || h.trace_id END "
    "LEFT JOIN wanshitong_feedback_details d ON d.trace_id=f.trace_id "
    "LEFT JOIN wanshitong_feedback_reviews r ON r.trace_id=f.trace_id "
    "WHERE h.project_id=? AND h.knowledge_base_id=? "
    "AND h.created_at>=? AND h.created_at<? "
    "AND CASE WHEN json_valid(h.metadata_json) "
    "THEN COALESCE(json_extract(h.metadata_json, "
    "'$.usage_audit.deployment_id'), 'LEGACY_UNKNOWN') "
    "ELSE 'LEGACY_UNKNOWN' END=? "
    "ORDER BY h.created_at, h.trace_id"
)

_REVISION_SQL = (
    "SELECT h.trace_id, h.metadata_json FROM query_history h "
    "WHERE h.project_id=? AND h.knowledge_base_id=? "
    "AND h.created_at>=? AND h.created_at<? "
    "AND CASE WHEN json_valid(h.metadata_json) "
    "THEN COALESCE(json_extract(h.metadata_json, "
    "'$.usage_audit.deployment_id'), 'LEGACY_UNKNOWN') "
    "ELSE 'LEGACY_UNKNOWN' END=? "
    "ORDER BY h.created_at, h.trace_id LIMIT ?"
)

_FREQUENT_COUNT_SQL = "SELECT COUNT(*) FROM question_stats_items WHERE run_id=?"
_FREQUENT_PAGE_SQL = (
    "SELECT * FROM question_stats_items WHERE run_id=? "
    "ORDER BY distinct_users DESC, user_day_heat DESC, "
    "request_count DESC, last_seen_at DESC, group_key LIMIT ? OFFSET ?"
)
_UNRESOLVED_COUNT_SQL = (
    "SELECT COUNT(*) FROM question_stats_items WHERE run_id=? "
    "AND (confirmed_open_issue_count>0 OR negative_feedback_count>0 "
    "OR failed_count>0)"
)
_UNRESOLVED_PAGE_SQL = (
    "SELECT * FROM question_stats_items WHERE run_id=? "
    "AND (confirmed_open_issue_count>0 OR negative_feedback_count>0 "
    "OR failed_count>0) "
    "ORDER BY confirmed_open_issue_count DESC, distinct_users DESC, "
    "request_count DESC, last_seen_at DESC, group_key LIMIT ? OFFSET ?"
)
_SAMPLE_FEEDBACK_SQL = (
    "SELECT trace_id FROM product_feedback WHERE trace_id IN (?,?,?) "
    "AND project_id=? AND knowledge_base_id=?"
)


@dataclass(slots=True)
class _RunCounts:
    """单次重算的覆盖与排除账目。"""

    source_total: int = 0
    eligible_count: int = 0
    unknown_count: int = 0
    excluded_count: int = 0
    unknown_identity_count: int = 0
    body_missing_count: int = 0
    unreadable_count: int = 0
    pending_count: int = 0
    coverage_start: str | None = None


@dataclass(slots=True)
class _Group:
    """只在本次进程内持有 owner 去重集合。"""

    group_key: str
    group_kind: str
    request_count: int = 0
    owners: set[str] = field(default_factory=set)
    owner_days: set[tuple[str, str]] = field(default_factory=set)
    manual_request_count: int = 0
    manual_owners: set[str] = field(default_factory=set)
    manual_owner_days: set[tuple[str, str]] = field(default_factory=set)
    suggestion_count: int = 0
    popular_count: int = 0
    retry_count: int = 0
    unknown_entry_count: int = 0
    answered_count: int = 0
    refused_count: int = 0
    failed_count: int = 0
    feedback_count: int = 0
    helpful_count: int = 0
    negative_feedback_count: int = 0
    false_refusal_count: int = 0
    confirmed_open_issue_count: int = 0
    last_seen_at: str = ""
    sample_trace_ids: list[str] = field(default_factory=list)

    def stored_values(self, run_id: str) -> tuple[object, ...]:
        """把去重集合折算为标量，绝不写出提问者名单。"""
        return (
            run_id,
            self.group_key,
            self.group_kind,
            self.request_count,
            len(self.owners),
            len(self.owner_days),
            self.manual_request_count,
            len(self.manual_owners),
            len(self.manual_owner_days),
            self.suggestion_count,
            self.popular_count,
            self.retry_count,
            self.unknown_entry_count,
            self.answered_count,
            self.refused_count,
            self.failed_count,
            self.feedback_count,
            self.helpful_count,
            self.negative_feedback_count,
            self.false_refusal_count,
            self.confirmed_open_issue_count,
            self.last_seen_at,
            json.dumps(list(reversed(self.sample_trace_ids))),
        )


@dataclass(slots=True)
class _Snapshot:
    """读事务关闭后的有界原始行。"""

    rows: list[dict[str, object]]
    classification_digest: str
    source_revision: tuple[object, ...]
    limit_code: str | None


def normalize_question(question: str) -> str:
    """仅做 NFC 与外侧标点/空白的确定性规整。"""
    normalized = unicodedata.normalize("NFC", question).strip()
    normalized = " ".join(normalized.split())
    return normalized.rstrip(_QUESTION_ENDINGS).strip()


def _group_key(
    deployment_id: str,
    project_id: str,
    knowledge_base_id: str,
    normalized_question: str,
    context_digest: str | None,
) -> str:
    parts = [
        deployment_id,
        project_id,
        knowledge_base_id,
        _NORMALIZER_REVISION,
        normalized_question,
    ]
    if context_digest is not None:
        parts.append(context_digest)
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _metadata(raw: object) -> dict[str, object] | None:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _classification_digest(
    rows: list[dict[str, object]], *, deadline: float | None = None
) -> str:
    digest = hashlib.sha256()
    for row in rows:
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError("QUESTION_ANALYTICS_REVISION_TIME_LIMIT")
        metadata = _metadata(row["metadata_json"])
        audit = None if metadata is None else metadata.get("usage_audit")
        payload = json.dumps(
            [row["trace_id"], audit],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


class QuestionAnalyticsService:
    """按固定部署及知识范围生成与读取两张管理员榜。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        history: ProductQueryHistory,
        *,
        deployment_id: str,
        retention_days: int,
    ) -> None:
        self._connections = connections
        self._history = history
        self.deployment_id = deployment_id
        self.retention_days = retention_days
        self._lock_path = Path(
            str(connections.database_path) + ".question-stats.lock"
        )
        self._recover_abandoned()

    def _try_lock(self) -> int | None:
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        descriptor = os.open(self._lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return descriptor

    def _recover_abandoned(self) -> None:
        """仅在没有活跃刷新持锁时结算重启遗留状态。"""
        descriptor = self._try_lock()
        if descriptor is None:
            return
        try:
            with self._connections.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE question_stats_runs SET state='FAILED', "
                    "failure_code='PROCESS_INTERRUPTED', finished_at=? "
                    "WHERE state='BUILDING' AND deployment_id=?",
                    (_utc_now().isoformat(), self.deployment_id),
                )
        finally:
            os.close(descriptor)

    def refresh(
        self, *, project_id: str, knowledge_base_id: str
    ) -> dict[str, object]:
        """建立一个有界后台 run；重复请求返回正在运行的同一 run。"""
        descriptor = self._try_lock()
        if descriptor is None:
            # 持锁者写入 BUILDING 前有一个极短窗口；等待该 run 可见。
            deadline = monotonic() + 0.2
            while monotonic() < deadline:
                current = self._building_run(project_id, knowledge_base_id)
                if current is not None:
                    return current
                sleep(0.01)
            raise RuntimeError("QUESTION_ANALYTICS_REFRESH_BUSY")
        now = _utc_now()
        run_id = "qrun_" + uuid.uuid4().hex
        try:
            with self._connections.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE question_stats_runs SET state='FAILED', "
                    "failure_code='PROCESS_INTERRUPTED', finished_at=? "
                    "WHERE state='BUILDING' AND deployment_id=? "
                    "AND project_id=? AND knowledge_base_id=?",
                    (
                        now.isoformat(),
                        self.deployment_id,
                        project_id,
                        knowledge_base_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM question_stats_runs WHERE expires_at<=? "
                    "AND state!='BUILDING'",
                    (now.isoformat(),),
                )
                connection.execute(
                    "INSERT INTO question_stats_runs (run_id, schema_version, "
                    "deployment_id, project_id, knowledge_base_id, "
                    "window_start, window_end, business_timezone, observed_at, "
                    "normalizer_revision, alias_revision, policy_revision, "
                    "state, created_at, expires_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'BUILDING', ?, ?)",
                    (
                        run_id,
                        _SCHEMA_VERSION,
                        self.deployment_id,
                        project_id,
                        knowledge_base_id,
                        (now - timedelta(days=_WINDOW_DAYS)).isoformat(),
                        now.isoformat(),
                        _BUSINESS_TIMEZONE,
                        now.isoformat(),
                        _NORMALIZER_REVISION,
                        _ALIAS_REVISION,
                        _POLICY_REVISION,
                        now.isoformat(),
                        (now + timedelta(days=self.retention_days)).isoformat(),
                    ),
                )
            worker = threading.Thread(
                target=self._run,
                args=(run_id, project_id, knowledge_base_id, descriptor),
                name="wst-question-analytics",
                daemon=True,
            )
            worker.start()
        except Exception:
            os.close(descriptor)
            raise
        return self.run(
            run_id, project_id=project_id, knowledge_base_id=knowledge_base_id
        )

    def _building_run(
        self, project_id: str, knowledge_base_id: str
    ) -> dict[str, object] | None:
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM question_stats_runs WHERE deployment_id=? "
                "AND project_id=? AND knowledge_base_id=? AND state='BUILDING' "
                "ORDER BY created_at DESC LIMIT 1",
                (self.deployment_id, project_id, knowledge_base_id),
            ).fetchone()
        return None if row is None else dict(row)

    def run(
        self,
        run_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
    ) -> dict[str, object]:
        """仅向同一固定 Scope 管理员返回本部署的运行状态。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM question_stats_runs WHERE run_id=? "
                "AND deployment_id=? AND project_id=? AND knowledge_base_id=?",
                (run_id, self.deployment_id, project_id, knowledge_base_id),
            ).fetchone()
        if row is None:
            raise NotFound("统计任务不存在。", stage="wanshitong.analytics.run")
        return dict(row)

    def _run(
        self,
        run_id: str,
        project_id: str,
        knowledge_base_id: str,
        descriptor: int,
    ) -> None:
        """在不持有数据库写锁的阶段完成快照解密与分组。"""
        try:
            run = self.run(
                run_id,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
            snapshot = self._snapshot(run)
            counts, groups = self._calculate(snapshot.rows, run)
            state = "COMPLETE"
            failure_code = snapshot.limit_code
            if failure_code is not None:
                state = "LIMITED"
            self._finish_run(
                run_id,
                run=run,
                snapshot=snapshot,
                counts=counts,
                groups=groups if state == "COMPLETE" else [],
                state=state,
                failure_code=failure_code,
            )
        except Exception as error:
            _LOGGER.error(
                "QUESTION_ANALYTICS_FAILED run_id=%s error_type=%s",
                run_id,
                type(error).__name__,
            )
            try:
                self._finish_run(
                    run_id,
                    run=None,
                    snapshot=None,
                    counts=_RunCounts(),
                    groups=[],
                    state="FAILED",
                    failure_code="UNEXPECTED_" + type(error).__name__.upper(),
                )
            except Exception:
                _LOGGER.error(
                    "QUESTION_ANALYTICS_STATUS_WRITE_FAILED run_id=%s", run_id
                )
        finally:
            os.close(descriptor)

    def _snapshot(self, run: dict[str, object]) -> _Snapshot:
        """同一只读事务中物化 History、最新反馈、复核和来源可读性。"""
        started = monotonic()
        deadline = started + _READ_SECONDS
        rows: list[dict[str, object]] = []
        total_bytes = 0
        limit_code: str | None = None
        source_revision: tuple[object, ...] = (None,)
        parameters = (
            run["project_id"],
            run["knowledge_base_id"],
            run["window_start"],
            run["window_end"],
            run["deployment_id"],
        )
        try:
            with self._read_snapshot_connection(deadline) as connection:
                cursor = connection.execute(_SNAPSHOT_SQL, parameters)
                for source_row in cursor:
                    if len(rows) >= _MAX_ROWS:
                        limit_code = "SNAPSHOT_ROW_LIMIT"
                        break
                    row = dict(source_row)
                    total_bytes += sum(
                        len(value.encode("utf-8"))
                        if isinstance(value, str)
                        else len(value)
                        if isinstance(value, bytes)
                        else 16
                        for value in row.values()
                    )
                    if total_bytes > _MAX_BYTES:
                        limit_code = "SNAPSHOT_BYTE_LIMIT"
                        break
                    metadata = _metadata(row["metadata_json"])
                    source_reason: str | None
                    if metadata is None:
                        source_reason = "METADATA_UNREADABLE"
                    else:
                        try:
                            source_reason = _source_unavailable_reason(
                                connection, source_row, metadata
                            )
                        except (TypeError, ValueError, sqlite3.Error):
                            source_reason = "SOURCE_METADATA_UNREADABLE"
                    row["source_unavailable_reason"] = source_reason
                    rows.append(row)
                    if monotonic() >= deadline:
                        limit_code = "SNAPSHOT_TIME_LIMIT"
                        break
                if limit_code is None:
                    source_revision = self._source_revision(
                        connection,
                        project_id=str(run["project_id"]),
                        knowledge_base_id=str(run["knowledge_base_id"]),
                    )
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT:
                raise
            limit_code = "SNAPSHOT_TIME_LIMIT"
        return _Snapshot(
            rows=rows,
            classification_digest=_classification_digest(rows),
            source_revision=source_revision,
            limit_code=limit_code,
        )

    @contextmanager
    def _read_snapshot_connection(
        self, deadline: float
    ) -> Iterator[sqlite3.Connection]:
        """以 SQLite 的只读 URI 建立独立快照并限制 SQL 扫描时间。"""
        uri = self._connections.database_path.as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=min(
                self._connections.busy_timeout_ms / 1000, _READ_SECONDS
            ),
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(
                lambda: int(monotonic() >= deadline), 1000
            )
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.set_progress_handler(None, 0)
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    @staticmethod
    def _source_revision(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        knowledge_base_id: str,
    ) -> tuple[object, ...]:
        """记录 active revision 与来源目录的可观察版本。"""
        knowledge_base = connection.execute(
            "SELECT active_revision_id, updated_at, deleted_at "
            "FROM knowledge_bases WHERE project_id=? AND knowledge_base_id=?",
            (project_id, knowledge_base_id),
        ).fetchone()
        documents = connection.execute(
            "SELECT COUNT(*) AS total, MAX(updated_at) AS latest "
            "FROM documents WHERE project_id=? AND knowledge_base_id=?",
            (project_id, knowledge_base_id),
        ).fetchone()
        if knowledge_base is None or documents is None:
            return (None,)
        return (
            knowledge_base["active_revision_id"],
            knowledge_base["updated_at"],
            knowledge_base["deleted_at"],
            documents["total"],
            documents["latest"],
        )

    def _revision_unchanged(  # noqa: PLR0911
        self,
        connection: sqlite3.Connection,
        run: dict[str, object],
        snapshot: _Snapshot,
    ) -> bool:
        """在发布写事务内有界复核来源与整个窗口的分类版本。"""
        if (
            run["normalizer_revision"] != _NORMALIZER_REVISION
            or run["alias_revision"] != _ALIAS_REVISION
            or run["policy_revision"] != _POLICY_REVISION
        ):
            return False
        deadline = monotonic() + _PUBLISH_REVISION_SECONDS
        connection.set_progress_handler(
            lambda: int(monotonic() >= deadline), 1000
        )
        try:
            source_revision = self._source_revision(
                connection,
                project_id=str(run["project_id"]),
                knowledge_base_id=str(run["knowledge_base_id"]),
            )
            if source_revision != snapshot.source_revision:
                return False
            rows: list[dict[str, object]] = []
            for row in connection.execute(
                _REVISION_SQL,
                (
                    run["project_id"],
                    run["knowledge_base_id"],
                    run["window_start"],
                    run["window_end"],
                    run["deployment_id"],
                    _MAX_ROWS + 1,
                ),
            ):
                if monotonic() >= deadline:
                    return False
                rows.append(dict(row))
                if len(rows) > len(snapshot.rows):
                    return False
            return (
                len(rows) == len(snapshot.rows)
                and _classification_digest(rows, deadline=deadline)
                == snapshot.classification_digest
            )
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT:
                raise
            return False
        except TimeoutError:
            return False
        finally:
            connection.set_progress_handler(None, 0)

    def _calculate(
        self, rows: list[dict[str, object]], run: dict[str, object]
    ) -> tuple[_RunCounts, list[_Group]]:
        """逐 trace 映射终态、分类、正文和最新反馈后再做 distinct。"""
        counts = _RunCounts()
        groups: dict[str, _Group] = {}
        seen_trace_ids: set[str] = set()
        observed_at = datetime.fromisoformat(str(run["observed_at"]))
        zone = ZoneInfo(str(run["business_timezone"]))
        for row in rows:
            counts.source_total += 1
            created_at = str(row["created_at"])
            if (
                counts.coverage_start is None
                or created_at < counts.coverage_start
            ):
                counts.coverage_start = created_at
            eligible = self._eligible_row(row, counts, observed_at, zone)
            if eligible is None:
                continue
            trace_id, normalized, context_digest, business_day, usage = eligible
            if trace_id in seen_trace_ids:
                counts.excluded_count += 1
                continue
            seen_trace_ids.add(trace_id)
            key = _group_key(
                str(run["deployment_id"]),
                str(run["project_id"]),
                str(run["knowledge_base_id"]),
                normalized,
                context_digest,
            )
            group = groups.setdefault(
                key,
                _Group(
                    group_key=key,
                    group_kind="CONTEXT" if context_digest else "EXACT",
                ),
            )
            self._apply_row(
                group,
                row,
                usage,
                trace_id=trace_id,
                business_day=business_day,
            )
            counts.eligible_count += 1
            if usage.get("identity_source") != "RDMS_SSO":
                counts.unknown_identity_count += 1
        return counts, list(groups.values())

    def _eligible_row(  # noqa: PLR0911
        self,
        row: dict[str, object],
        counts: _RunCounts,
        observed_at: datetime,
        zone: ZoneInfo,
    ) -> tuple[str, str, str | None, str, dict[str, object]] | None:
        """按真实终态、可信分类、来源和正文依次记录排除原因。"""
        status = str(row["status"])
        if status == "STARTED":
            counts.pending_count += 1
            return None
        if status not in _TERMINAL:
            counts.excluded_count += 1
            return None
        metadata = _metadata(row["metadata_json"])
        usage = usage_audit_view(
            None if metadata is None else metadata.get("usage_audit")
        )
        traffic_class = usage["effective_traffic_class"]
        if traffic_class == "LEGACY_UNKNOWN":
            counts.unknown_count += 1
            return None
        if traffic_class != "INTERACTIVE":
            counts.excluded_count += 1
            return None
        if str(row["expires_at"]) <= observed_at.isoformat():
            counts.excluded_count += 1
            return None
        if not row["body_saved"] or row["ciphertext"] is None:
            counts.body_missing_count += 1
            return None
        if row["source_unavailable_reason"] is not None:
            counts.unreadable_count += 1
            return None
        try:
            payload = self._history.decode_snapshot_payload(row)
            question = payload.get("question")
            trace_id = normalize_trace_id(str(row["trace_id"]))
            business_day = (
                datetime.fromisoformat(str(row["created_at"]))
                .astimezone(zone)
                .date()
                .isoformat()
            )
        except (InvalidTag, ValueError, TypeError, KeyError):
            counts.unreadable_count += 1
            return None
        if not isinstance(question, str):
            counts.body_missing_count += 1
            return None
        normalized = normalize_question(question)
        if not normalized:
            counts.body_missing_count += 1
            return None
        context_digest: str | None = None
        if metadata is not None and (
            metadata.get("conversation_context_present") is True
            or usage.get("has_context") is True
        ):
            candidate = metadata.get("conversation_context_digest")
            if not isinstance(candidate, str) or not candidate:
                counts.unreadable_count += 1
                return None
            context_digest = candidate
        return trace_id, normalized, context_digest, business_day, usage

    @staticmethod
    def _apply_row(
        group: _Group,
        row: dict[str, object],
        usage: dict[str, object],
        *,
        trace_id: str,
        business_day: str,
    ) -> None:
        """一条已准入交互请求只增加一次组内账目。"""
        group.request_count += 1
        created_at = str(row["created_at"])
        group.last_seen_at = max(group.last_seen_at, created_at)
        group.sample_trace_ids.append(trace_id)
        del group.sample_trace_ids[:-_MAX_SAMPLES]
        trusted_owner = (
            str(row["owner_id"])
            if usage.get("identity_source") == "RDMS_SSO"
            else None
        )
        if trusted_owner is not None:
            group.owners.add(trusted_owner)
            group.owner_days.add((trusted_owner, business_day))
        entrypoint = usage.get("entrypoint")
        if entrypoint == "manual":
            group.manual_request_count += 1
            if trusted_owner is not None:
                group.manual_owners.add(trusted_owner)
                group.manual_owner_days.add((trusted_owner, business_day))
        elif entrypoint == "suggestion":
            group.suggestion_count += 1
        elif entrypoint == "popular":
            group.popular_count += 1
        elif entrypoint == "retry":
            group.retry_count += 1
        else:
            group.unknown_entry_count += 1
        status = row["status"]
        if status == "ANSWERED":
            # History 的 ANSWERED 由 finish 时实际 result.answer 非空生成。
            group.answered_count += 1
        elif status == "REFUSED":
            group.refused_count += 1
        elif status in _FAILED:
            group.failed_count += 1
        QuestionAnalyticsService._apply_feedback(group, row)

    @staticmethod
    def _apply_feedback(group: _Group, row: dict[str, object]) -> None:
        """只读取 canonical 最新反馈和匹配当前版本的管理员复核。"""
        useful = row["feedback_useful"]
        if useful is not None:
            group.feedback_count += 1
            if bool(useful):
                group.helpful_count += 1
            else:
                group.negative_feedback_count += 1
                detail_current = (
                    row["detail_updated_at"] is not None
                    and row["detail_updated_at"] == row["feedback_updated_at"]
                )
                if (
                    detail_current
                    and row["feedback_reason_detail"] == "FALSE_REFUSAL"
                ):
                    group.false_refusal_count += 1
                if (
                    detail_current
                    and row["review_status"] in {"REVIEWED", "FIX_PLANNED"}
                    and row["review_root_cause"]
                    not in {None, "EXPECTED_REFUSAL"}
                    and row["reviewed_feedback_revision"]
                    == row["feedback_revision"]
                ):
                    group.confirmed_open_issue_count += 1

    def _finish_run(  # noqa: PLR0913
        self,
        run_id: str,
        *,
        run: dict[str, object] | None,
        snapshot: _Snapshot | None,
        counts: _RunCounts,
        groups: list[_Group],
        state: str,
        failure_code: str | None,
    ) -> None:
        """用一次短写事务原子发布 items 与 COMPLETE 指针。"""
        with self._connections.transaction(write=True) as connection:
            if (
                state == "COMPLETE"
                and run is not None
                and snapshot is not None
                and not self._revision_unchanged(connection, run, snapshot)
            ):
                state = "LIMITED"
                failure_code = "SOURCE_OR_CLASSIFICATION_CHANGED"
                groups = []
            if groups:
                connection.executemany(
                    "INSERT INTO question_stats_items (run_id, group_key, "
                    "group_kind, request_count, distinct_users, user_day_heat, "
                    "manual_request_count, manual_distinct_users, "
                    "manual_user_day_heat, suggestion_count, popular_count, "
                    "retry_count, unknown_entry_count, answered_count, "
                    "refused_count, failed_count, feedback_count, "
                    "helpful_count, "
                    "negative_feedback_count, false_refusal_count, "
                    "confirmed_open_issue_count, last_seen_at, "
                    "sample_trace_ids_json) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?)",
                    (group.stored_values(run_id) for group in groups),
                )
            cursor = connection.execute(
                "UPDATE question_stats_runs SET state=?, source_total=?, "
                "eligible_count=?, unknown_count=?, excluded_count=?, "
                "unknown_identity_count=?, body_missing_count=?, "
                "unreadable_count=?, pending_count=?, coverage_start=?, "
                "finished_at=?, failure_code=? WHERE run_id=? "
                "AND state='BUILDING'",
                (
                    state,
                    counts.source_total,
                    counts.eligible_count,
                    counts.unknown_count,
                    counts.excluded_count,
                    counts.unknown_identity_count,
                    counts.body_missing_count,
                    counts.unreadable_count,
                    counts.pending_count,
                    counts.coverage_start,
                    _utc_now().isoformat(),
                    failure_code,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("QUESTION_ANALYTICS_RUN_STATE_CHANGED")

    def board(
        self,
        *,
        project_id: str,
        knowledge_base_id: str,
        board: str,
        page_size: int = 20,
        offset: int = 0,
    ) -> dict[str, object]:
        """只选择最新未过期 COMPLETE；失败或限流不清空上次榜。"""
        if board not in {"frequent", "unresolved"}:
            raise ValueError("问题榜类型无效。")
        if not 1 <= page_size <= _MAX_PAGE_SIZE or offset < 0:
            raise ValueError("问题榜分页越界。")
        scope = (self.deployment_id, project_id, knowledge_base_id)
        now = _utc_now().isoformat()
        with self._connections.transaction() as connection:
            latest = connection.execute(
                "SELECT * FROM question_stats_runs WHERE deployment_id=? "
                "AND project_id=? AND knowledge_base_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                scope,
            ).fetchone()
            completed = connection.execute(
                "SELECT * FROM question_stats_runs WHERE deployment_id=? "
                "AND project_id=? AND knowledge_base_id=? "
                "AND state='COMPLETE' AND expires_at>? "
                "ORDER BY created_at DESC LIMIT 1",
                (*scope, now),
            ).fetchone()
            if completed is None:
                return {
                    "run": None,
                    "latest_run": None if latest is None else dict(latest),
                    "items": [],
                    "total": 0,
                    "next_offset": None,
                }
            run_id = str(completed["run_id"])
            count_sql, page_sql = (
                (_UNRESOLVED_COUNT_SQL, _UNRESOLVED_PAGE_SQL)
                if board == "unresolved"
                else (_FREQUENT_COUNT_SQL, _FREQUENT_PAGE_SQL)
            )
            total = int(
                connection.execute(
                    count_sql,
                    (run_id,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                page_sql,
                (run_id, page_size, offset),
            ).fetchall()
        items = [
            self._item_view(dict(row), project_id, knowledge_base_id)
            for row in rows
        ]
        next_offset = (
            offset + len(items) if offset + len(items) < total else None
        )
        return {
            "run": dict(completed),
            "latest_run": None if latest is None else dict(latest),
            "items": items,
            "total": total,
            "next_offset": next_offset,
        }

    def _item_view(
        self,
        row: dict[str, object],
        project_id: str,
        knowledge_base_id: str,
    ) -> dict[str, object]:
        """列表只按样本引用实时读取一条仍可授权查看的代表题。"""
        trace_ids = self._sample_ids(row)
        representative: str | None = None
        readable_ids: list[str] = []
        for trace_id in trace_ids:
            sample = self._current_sample(
                trace_id,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
            if sample is not None:
                readable_ids.append(str(sample["trace_id"]))
                if representative is None:
                    representative = cast(str, sample["question"])
        row["representative_question"] = representative
        row["sample_trace_ids"] = readable_ids
        row.pop("sample_trace_ids_json", None)
        return row

    @staticmethod
    def _sample_ids(row: dict[str, object]) -> list[str]:
        try:
            values = json.loads(str(row["sample_trace_ids_json"]))
        except (KeyError, TypeError, ValueError):
            return []
        if not isinstance(values, list):
            return []
        return [
            value for value in values[:_MAX_SAMPLES] if isinstance(value, str)
        ]

    def samples(
        self,
        *,
        run_id: str,
        group_key: str,
        project_id: str,
        knowledge_base_id: str,
    ) -> dict[str, object]:
        """展开时重新执行 History 当前授权、来源与保留期检查。"""
        run = self.run(
            run_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        if (
            run["state"] != "COMPLETE"
            or str(run["expires_at"]) <= _utc_now().isoformat()
        ):
            raise NotFound(
                "统计样本已过期。", stage="wanshitong.analytics.sample"
            )
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT sample_trace_ids_json FROM question_stats_items "
                "WHERE run_id=? AND group_key=?",
                (run_id, group_key),
            ).fetchone()
        if row is None:
            raise NotFound(
                "统计分组不存在。", stage="wanshitong.analytics.sample"
            )
        sample_items = [
            sample
            for trace_id in self._sample_ids(dict(row))
            if (
                sample := self._current_sample(
                    trace_id,
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                )
            )
            is not None
        ]
        if sample_items:
            sample_ids = [str(item["trace_id"]) for item in sample_items]
            sample_ids.extend([""] * (_MAX_SAMPLES - len(sample_ids)))
            with self._connections.transaction() as connection:
                feedback_ids = {
                    str(row["trace_id"])
                    for row in connection.execute(
                        _SAMPLE_FEEDBACK_SQL,
                        (*sample_ids, project_id, knowledge_base_id),
                    )
                }
            for item in sample_items:
                item["has_feedback"] = item["trace_id"] in feedback_ids
        return {"items": sample_items}

    def _current_sample(
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
    ) -> dict[str, object] | None:
        try:
            history = self._history.detail(
                trace_id,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
        except (NotFound, InvalidTag, ValueError, TypeError):
            return None
        usage = history.get("usage_audit")
        if not isinstance(usage, dict) or (
            (usage.get("deployment_id") or "LEGACY_UNKNOWN")
            != self.deployment_id
            or usage.get("effective_traffic_class") != "INTERACTIVE"
            or history.get("body_available") is not True
        ):
            return None
        question = history.get("question")
        if not isinstance(question, str) or not question:
            return None
        return {
            "trace_id": normalize_trace_id(str(history["trace_id"])),
            "question": question,
            "created_at": history["created_at"],
            "status": history["status"],
            "has_feedback": False,
        }


__all__ = ["QuestionAnalyticsService", "normalize_question"]
