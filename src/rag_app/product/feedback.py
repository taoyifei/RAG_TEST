"""Product 反馈主表与 Operational Trace 可恢复投影。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol, cast

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.core.errors import NotFound, PolicyDenied, ProviderUnavailable
from rag_app.tracing.store import TraceNotFoundError

FeedbackReason = Literal[
    "INCORRECT",
    "INCOMPLETE",
    "WRONG_SOURCE",
    "OUTDATED",
    "TOO_SLOW",
    "OTHER",
]
ProjectionState = Literal["PENDING", "APPLIED", "NOT_APPLICABLE"]

_TRACE_ID = re.compile(r"^(?:trace_)?[0-9a-f]{32}$")
_REASONS = frozenset(
    {
        "INCORRECT",
        "INCOMPLETE",
        "WRONG_SOURCE",
        "OUTDATED",
        "TOO_SLOW",
        "OTHER",
    }
)
_TERMINAL_QUERY_STATES = frozenset({"ANSWERED", "REFUSED"})
_MAX_RECOVERY_BATCH: Final = 1000


@dataclass(frozen=True, slots=True)
class ProductFeedback:
    """不含问答、证据、网络或凭据正文的反馈视图。"""

    trace_id: str
    project_id: str
    knowledge_base_id: str
    useful: bool
    reason_code: FeedbackReason | None
    projection_state: ProjectionState
    updated_at: str


class FeedbackProjector(Protocol):
    """只接收安全身份与布尔信号的 Trace 投影端口。"""

    def __call__(self, trace_id: str, *, useful: bool) -> bool:
        """更新或明确跳过对应 Operational Trace 根。"""
        ...


class ProductFeedbackStore:
    """将主库作为 canonical feedback，并幂等维护 Trace 筛选投影。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        projector: FeedbackProjector,
    ) -> None:
        """保存主库与只接收 trace ID/布尔值的投影函数。

        Args:
            connections: Product 主库连接工厂。
            projector: 更新 Operational Trace feedback_useful 的函数。

        """
        self._connections = connections
        self._projector = projector

    def upsert(  # noqa: PLR0913
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
        actor_owner_id: str,
        actor_is_admin: bool,
        useful: bool,
        reason_code: FeedbackReason | None,
    ) -> ProductFeedback:
        """验证终态 Query 和 scope 后幂等写主表并尝试投影。

        Args:
            trace_id: 新旧两种格式的 Query Trace ID。
            project_id: 路由已绑定的项目 ID。
            knowledge_base_id: 路由已绑定的知识库 ID。
            actor_owner_id: 当前 Session 或 Access Token 主体。
            actor_is_admin: 是否允许管理员代表原 owner 提交。
            useful: 有用或没用信号。
            reason_code: 可选有限原因；有用反馈不得附原因。

        Returns:
            canonical 反馈及当前投影状态。

        Raises:
            NotFound: Trace 不存在或不是可反馈的终态 Query。
            PolicyDenied: scope 或 owner 不匹配。
            ValueError: ID 或 reason 合同无效。

        """
        canonical = normalize_trace_id(trace_id)
        reason = _validate_reason(useful, reason_code)
        now = datetime.now(UTC).isoformat()
        try:
            with self._connections.transaction(write=True) as connection:
                trace = _query_trace(connection, canonical)
                if trace is None or str(trace["status"]) not in (
                    _TERMINAL_QUERY_STATES
                ):
                    raise NotFound(
                        "只允许对已完成的 Query 提交反馈。",
                        stage="feedback.validate",
                    )
                if (
                    str(trace["project_id"]) != project_id
                    or str(trace["knowledge_base_id"]) != knowledge_base_id
                ):
                    raise PolicyDenied(
                        "反馈资源范围不匹配。", stage="feedback.scope"
                    )
                if not actor_is_admin and str(trace["owner_id"]) != (
                    actor_owner_id
                ):
                    raise PolicyDenied(
                        "只能提交当前主体自己的反馈。",
                        stage="feedback.owner",
                    )
                connection.execute(
                    "INSERT INTO product_feedback(trace_id, project_id, "
                    "knowledge_base_id, owner_id, useful, reason_code, "
                    "projection_state, projection_version, "
                    "projection_attempts, "
                    "projection_error_code, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 1, 0, NULL, ?, ?) "
                    "ON CONFLICT(trace_id) DO UPDATE SET "
                    "useful=excluded.useful, "
                    "reason_code=excluded.reason_code, "
                    "projection_state='PENDING', projection_attempts=0, "
                    "projection_version=product_feedback.projection_version+1, "
                    "projection_error_code=NULL, "
                    "updated_at=excluded.updated_at",
                    (
                        canonical,
                        project_id,
                        knowledge_base_id,
                        str(trace["owner_id"]),
                        int(useful),
                        reason,
                        now,
                        now,
                    ),
                )
        except sqlite3.Error as error:
            raise _unavailable("feedback.upsert") from error
        self.reconcile_trace(canonical)
        value = self.get(
            canonical,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            actor_owner_id=actor_owner_id,
            actor_is_admin=actor_is_admin,
        )
        if value is None:
            raise RuntimeError("反馈提交后无法在同一主库回读。")
        return value

    def get(
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
        actor_owner_id: str,
        actor_is_admin: bool,
    ) -> ProductFeedback | None:
        """按 Query 原 owner 与路由 scope 授权读取当前反馈。

        Args:
            trace_id: 新旧两种 Trace ID。
            project_id: 路由项目 ID。
            knowledge_base_id: 路由知识库 ID。
            actor_owner_id: 当前主体。
            actor_is_admin: 管理员可读取任一 owner。

        Returns:
            尚未提交时为 None，否则返回不含正文的反馈。

        """
        canonical = normalize_trace_id(trace_id)
        try:
            with self._connections.transaction() as connection:
                trace = _query_trace(connection, canonical)
                if trace is None:
                    raise NotFound(
                        "Query Trace 不存在。", stage="feedback.read"
                    )
                if (
                    str(trace["project_id"]) != project_id
                    or str(trace["knowledge_base_id"]) != knowledge_base_id
                ):
                    raise PolicyDenied(
                        "反馈资源范围不匹配。", stage="feedback.scope"
                    )
                if not actor_is_admin and str(trace["owner_id"]) != (
                    actor_owner_id
                ):
                    raise PolicyDenied(
                        "只能读取当前主体自己的反馈。",
                        stage="feedback.owner",
                    )
                row = connection.execute(
                    "SELECT * FROM product_feedback WHERE trace_id=?",
                    (canonical,),
                ).fetchone()
        except sqlite3.Error as error:
            raise _unavailable("feedback.read") from error
        return None if row is None else _feedback(row)

    def reconcile_trace(self, trace_id: str) -> ProductFeedback | None:
        """幂等应用一条 PENDING 投影；明确不适用时形成终态。

        Args:
            trace_id: canonical 或旧格式 Trace ID。

        Returns:
            不存在 canonical 反馈时为 None，否则返回最新状态。

        """
        canonical = normalize_trace_id(trace_id)
        for _ in range(3):
            try:
                with self._connections.transaction() as connection:
                    row = connection.execute(
                        "SELECT * FROM product_feedback WHERE trace_id=?",
                        (canonical,),
                    ).fetchone()
                    trace = _query_trace(connection, canonical)
            except sqlite3.Error as error:
                raise _unavailable("feedback.reconcile") from error
            if row is None:
                return None
            if str(row["projection_state"]) != "PENDING":
                return _feedback(row)
            useful = bool(row["useful"])
            version = int(row["projection_version"])
            projected_trace_id = (
                canonical if trace is None else str(trace["trace_id"])
            )
            try:
                applied = self._projector(
                    projected_trace_id,
                    useful=useful,
                )
            except (
                TraceNotFoundError,
                sqlite3.Error,
                ProviderUnavailable,
            ) as error:
                updated = self._mark_projection_attempt(
                    canonical,
                    expected_version=version,
                    state="PENDING",
                    error_code=type(error).__name__,
                )
            else:
                updated = self._mark_projection_attempt(
                    canonical,
                    expected_version=version,
                    state="APPLIED" if applied else "NOT_APPLICABLE",
                    error_code=None,
                )
            if updated:
                break
            self._requeue_newer_projection(canonical, version)
        try:
            with self._connections.transaction() as connection:
                current = connection.execute(
                    "SELECT * FROM product_feedback WHERE trace_id=?",
                    (canonical,),
                ).fetchone()
        except sqlite3.Error as error:
            raise _unavailable("feedback.reconcile") from error
        return None if current is None else _feedback(current)

    def recover(self, *, limit: int = _MAX_RECOVERY_BATCH) -> int:
        """启动或运维时有界重放未完成的跨库投影。

        Args:
            limit: 单次最多处理的 canonical 反馈数。

        Returns:
            本次尝试处理的行数。

        """
        if not 1 <= limit <= _MAX_RECOVERY_BATCH:
            raise ValueError("反馈恢复批次必须在 1 到 1000。")
        try:
            with self._connections.transaction() as connection:
                rows = connection.execute(
                    "SELECT trace_id FROM product_feedback "
                    "WHERE projection_state='PENDING' "
                    "ORDER BY updated_at, trace_id LIMIT ?",
                    (limit,),
                ).fetchall()
        except sqlite3.Error as error:
            raise _unavailable("feedback.recover") from error
        for row in rows:
            self.reconcile_trace(str(row["trace_id"]))
        return len(rows)

    def _mark_projection_attempt(
        self,
        trace_id: str,
        *,
        expected_version: int,
        state: ProjectionState,
        error_code: str | None,
    ) -> bool:
        try:
            with self._connections.transaction(write=True) as connection:
                cursor = connection.execute(
                    "UPDATE product_feedback SET projection_state=?, "
                    "projection_attempts=projection_attempts+1, "
                    "projection_error_code=?, updated_at=? WHERE trace_id=? "
                    "AND projection_state='PENDING' AND projection_version=?",
                    (
                        state,
                        error_code,
                        datetime.now(UTC).isoformat(),
                        trace_id,
                        expected_version,
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as error:
            raise _unavailable("feedback.projection") from error

    def _requeue_newer_projection(
        self,
        trace_id: str,
        completed_version: int,
    ) -> None:
        """旧投影晚到时，把较新 canonical 值重新放回待投影队列。"""
        try:
            with self._connections.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE product_feedback SET projection_state='PENDING', "
                    "projection_error_code='STALE_PROJECTION_REQUEUED' "
                    "WHERE trace_id=? AND projection_version>?",
                    (trace_id, completed_version),
                )
        except sqlite3.Error as error:
            raise _unavailable("feedback.projection") from error


def normalize_trace_id(trace_id: str) -> str:
    """将旧 32hex 规范化为当前公开前缀格式。

    Args:
        trace_id: 当前前缀格式或旧 32hex Trace ID。

    Returns:
        带 ``trace_`` 前缀的 canonical Trace ID。

    """
    if _TRACE_ID.fullmatch(trace_id) is None:
        raise ValueError("trace_id 格式无效。")
    return trace_id if trace_id.startswith("trace_") else f"trace_{trace_id}"


def _query_trace(
    connection: sqlite3.Connection, canonical_trace_id: str
) -> sqlite3.Row | None:
    legacy = canonical_trace_id.removeprefix("trace_")
    return cast(
        sqlite3.Row | None,
        connection.execute(
            "SELECT trace_id, project_id, knowledge_base_id, owner_id, status "
            "FROM query_history WHERE trace_id IN (?, ?) "
            "ORDER BY CASE WHEN trace_id=? THEN 0 ELSE 1 END LIMIT 1",
            (canonical_trace_id, legacy, canonical_trace_id),
        ).fetchone(),
    )


def _validate_reason(
    useful: bool, reason_code: FeedbackReason | None
) -> FeedbackReason | None:
    if reason_code is not None and reason_code not in _REASONS:
        raise ValueError("feedback reason_code 不受支持。")
    if useful and reason_code is not None:
        raise ValueError("有用反馈不应附带负向 reason_code。")
    return reason_code


def _feedback(row: sqlite3.Row) -> ProductFeedback:
    return ProductFeedback(
        trace_id=str(row["trace_id"]),
        project_id=str(row["project_id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        useful=bool(row["useful"]),
        reason_code=cast(FeedbackReason | None, row["reason_code"]),
        projection_state=cast(ProjectionState, row["projection_state"]),
        updated_at=str(row["updated_at"]),
    )


def _unavailable(stage: str) -> ProviderUnavailable:
    return ProviderUnavailable(
        "本地反馈暂时不可用。",
        stage=stage,
        code="FEEDBACK_STORE_UNAVAILABLE",
        retryable=True,
    )


__all__ = [
    "FeedbackProjector",
    "FeedbackReason",
    "ProductFeedback",
    "ProductFeedbackStore",
    "ProjectionState",
    "normalize_trace_id",
]
