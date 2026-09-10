"""复用产品主库和现有加密工具保存本机问答历史。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import cast

from cryptography.exceptions import InvalidTag

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    Conflict,
    NotFound,
    ProviderUnavailable,
    RagError,
)
from rag_app.core.events import TraceEvent
from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.provider import ProviderCall
from rag_app.core.models.search import RetrievalDiagnostics, SearchAnswerResult
from rag_app.product.crypto import SecretAad, SecretCipher
from rag_app.product.feedback import normalize_trace_id

_LOGGER = logging.getLogger(__name__)
_MAX_PAGE_SIZE = 200
_MAX_RETENTION_DAYS = 365
_MAX_KEYWORD_SCAN_RECORDS = 1_000
_MAX_KEYWORD_SCAN_SECONDS = 0.25
_EXPORT_LEASE_SECONDS = 15 * 60
_WRITE_QUEUE_SIZE = 1_024
_WRITE_BATCH_SIZE = 64
_WRITE_BATCH_WINDOW_SECONDS = 0.002
_WRITE_WAIT_SECONDS = 10.0
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TRACE_ID_PATTERN = re.compile(r"^(?:trace_)?[0-9a-f]{32}$")
_SECRET_TEXT = re.compile(
    r"(?i)(?:\b(?:authorization|cookie|api[_-]?key)\s*[:=]\s*"
    r"[^\r\n]+|\bBearer\s+\S+|\bsk-[a-zA-Z0-9_-]{12,})"
)
_STOP_WRITER = object()


@dataclass(frozen=True, slots=True)
class HistoryExportSnapshot:
    """支持包使用的单条 History 一致读快照。"""

    trace_id: str
    history_status: str
    project_id: str | None
    knowledge_base_id: str | None
    payload: dict[str, object]
    body_included: bool
    body_unavailable_reason: str | None


class HistorySnapshotLimitError(ValueError):
    """History 快照在生成归档前已命中成员或累计字节上限。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(slots=True)
class _HistoryWriteCommand:
    """等待同一 durable group commit 的单条 History 写命令。"""

    trace_id: str
    action: Callable[[sqlite3.Connection], object]
    completion: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None


class ProductQueryHistory:
    """持久化 TracePort 适配器；正文只存在 AES-GCM 密文中。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.TRACE_SINK,
        name="sqlite-product-history",
        version="1",
        mode=ProviderMode.LOCAL,
    )

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        cipher: SecretCipher,
        *,
        save_body: bool = True,
        retention_days: int = 7,
    ) -> None:
        if not 1 <= retention_days <= _MAX_RETENTION_DAYS:
            raise ValueError("历史保留天数必须在 1 到 365 天之间。")
        self._connections = connections
        self._cipher = cipher
        self.save_body = save_body
        self.retention_days = retention_days
        self._instance_id = uuid.uuid4().hex
        self._process_id = os.getpid()
        self._write_lock = threading.RLock()
        self._writer_state_lock = threading.Lock()
        self._write_queue: queue.Queue[_HistoryWriteCommand | object] = (
            queue.Queue(maxsize=_WRITE_QUEUE_SIZE)
        )
        self._writer: threading.Thread | None = None
        self._accepting_writes = True

    def _submit_write(
        self,
        trace_id: str,
        action: Callable[[sqlite3.Connection], object],
    ) -> None:
        """提交同步 History 写入并等待所属 group commit。

        Args:
            trace_id: 用于稳定错误关联的当前 Trace ID。
            action: 在 writer 外层事务及独立 savepoint 中执行的写操作。

        Returns:
            所属批次完成 durable commit 后无返回值。

        Raises:
            ProviderUnavailable: writer 已关闭、队列已满或等待超时。
            Exception: 当前命令自身的原始业务或 SQLite 异常。

        """
        command = _HistoryWriteCommand(trace_id=trace_id, action=action)
        with self._writer_state_lock:
            if not self._accepting_writes:
                raise self._unavailable(trace_id)
            if self._writer is None:
                self._writer = threading.Thread(
                    target=self._run_writer,
                    name="rag-history-writer",
                    daemon=False,
                )
                self._writer.start()
            try:
                self._write_queue.put_nowait(command)
            except queue.Full as error:
                raise self._unavailable(trace_id) from error
        if not command.completion.wait(timeout=_WRITE_WAIT_SECONDS):
            raise self._unavailable(trace_id)
        if command.error is not None:
            raise command.error

    def _run_writer(self) -> None:
        """在唯一后台线程中收集并提交有界 History 写批次。

        Args:
            无参数；持续消费本实例的有界队列。

        Returns:
            收到关闭标记并提交此前命令后返回。

        """
        while True:
            item = self._write_queue.get()
            if item is _STOP_WRITER:
                self._write_queue.task_done()
                return
            if not isinstance(item, _HistoryWriteCommand):
                self._write_queue.task_done()
                continue
            commands = [item]
            stop_after_batch = self._collect_write_batch(commands)
            self._execute_write_batch(commands)
            for _command in commands:
                self._write_queue.task_done()
            if stop_after_batch:
                self._write_queue.task_done()
                return

    def _collect_write_batch(
        self,
        commands: list[_HistoryWriteCommand],
    ) -> bool:
        """在固定微窗口内收集同一 durable commit 的并发命令。

        Args:
            commands: 已含首条命令且由 writer 独占的可变批次。

        Returns:
            收集期间是否同时收到关闭标记。

        """
        deadline = monotonic() + _WRITE_BATCH_WINDOW_SECONDS
        while len(commands) < _WRITE_BATCH_SIZE:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            try:
                candidate = self._write_queue.get(timeout=remaining)
            except queue.Empty:
                return False
            if candidate is _STOP_WRITER:
                return True
            if isinstance(candidate, _HistoryWriteCommand):
                commands.append(candidate)
            else:
                self._write_queue.task_done()
        return False

    def _execute_write_batch(
        self,
        commands: Sequence[_HistoryWriteCommand],
    ) -> None:
        """以逐命令 savepoint 和单次外层 commit 执行批次。

        Args:
            commands: 按队列顺序排列的非空写命令。

        Returns:
            全部命令均记录成功或错误并唤醒等待方后返回。

        """
        batch_error: Exception | None = None
        try:
            with (
                self._write_lock,
                self._connections.transaction(write=True) as connection,
            ):
                for index, command in enumerate(commands):
                    savepoint = f"history_write_{index}"
                    connection.execute(f"SAVEPOINT {savepoint}")
                    try:
                        command.action(connection)
                    except Exception as error:  # 每条命令独立失败，不污染同批。
                        connection.execute(f"ROLLBACK TO {savepoint}")
                        connection.execute(f"RELEASE {savepoint}")
                        command.error = error
                    else:
                        connection.execute(f"RELEASE {savepoint}")
        except Exception as error:
            batch_error = error
        finally:
            for command in commands:
                if command.error is None and batch_error is not None:
                    command.error = batch_error
                command.completion.set()

    def _stop_writer(self) -> None:
        """停止准入并等待此前 History 写命令全部提交。

        Args:
            无参数；幂等关闭本实例的 writer。

        Returns:
            writer 不存在或已退出后返回。

        Raises:
            ProviderUnavailable: 关闭标记无法入队或 writer 超时未退出。

        """
        with self._writer_state_lock:
            if not self._accepting_writes:
                return
            self._accepting_writes = False
            writer = self._writer
            if writer is None:
                return
            try:
                self._write_queue.put(_STOP_WRITER, timeout=_WRITE_WAIT_SECONDS)
            except queue.Full as error:
                raise self._unavailable("history-close") from error
        self._write_queue.join()
        writer.join(timeout=_WRITE_WAIT_SECONDS)
        if writer.is_alive():
            raise self._unavailable("history-close")

    def recover(self) -> None:
        """启动时将未终态请求标记中断，清理已到期记录。

        Args:
            无参数；核对主库中的存活进程。

        Returns:
            恢复完成时无返回值。

        """
        now = datetime.now(UTC).isoformat()
        try:
            with (
                self._write_lock,
                self._connections.transaction(write=True) as connection,
            ):
                rows = connection.execute(
                    "SELECT trace_id, process_id, metadata_json "
                    "FROM query_history WHERE status='STARTED'"
                ).fetchall()
                process_state: dict[int, bool] = {}
                for row in rows:
                    process_id = int(row["process_id"])
                    if process_id not in process_state:
                        process_state[process_id] = _process_alive(process_id)
                    alive = process_state[process_id]
                    if not alive:
                        metadata = cast(
                            dict[str, object],
                            json.loads(str(row["metadata_json"])),
                        )
                        metadata["reason_code"] = "PROCESS_INTERRUPTED"
                        connection.execute(
                            "UPDATE query_history SET status='INTERRUPTED', "
                            "finished_at=?, metadata_json=? "
                            "WHERE status='STARTED' AND trace_id=?",
                            (
                                now,
                                json.dumps(
                                    metadata,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                                row["trace_id"],
                            ),
                        )
            self.clear(expired_only=True)
        except (
            InvalidTag,
            ProviderUnavailable,
            sqlite3.Error,
            ValueError,
        ) as error:
            raise self._unavailable("startup") from error

    def start(  # noqa: PLR0913
        self,
        trace_id: str,
        scope: KnowledgeBaseScope,
        question: str,
        *,
        owner_id: str,
        save_body: bool,
        conversation_context_digest: str | None = None,
    ) -> None:
        """在模型和快照之前同步写入请求；不可用时不回退内存。

        Args:
            trace_id: 请求的安全标识。
            scope: 已鉴权项目及知识库。
            question: 当前问题正文。
            owner_id: 管理员会话或 Token 主体。
            save_body: 本次是否允许加密保存正文。
            conversation_context_digest: 可选的会话上下文 SHA256；不保存
                上下文正文。

        Returns:
            STARTED 落盘完成时无返回值。

        """
        if (
            conversation_context_digest is not None
            and _SHA256_PATTERN.fullmatch(conversation_context_digest) is None
        ):
            raise ValueError("会话上下文摘要必须是 64 位小写十六进制 SHA256。")
        now = datetime.now(UTC)
        body_saved = self.save_body and save_body
        ciphertext, nonce = self._encode(
            trace_id, {"question": question}, enabled=body_saved
        )
        parameters = (
            trace_id,
            scope.project_id,
            scope.knowledge_base_id,
            owner_id,
            now.isoformat(),
            (now + timedelta(days=self.retention_days)).isoformat(),
            hashlib.sha256(question.encode()).hexdigest(),
            int(body_saved),
            ciphertext,
            nonce,
            self._instance_id,
            self._process_id,
            json.dumps(
                {
                    "conversation_context_digest": conversation_context_digest,
                    "conversation_context_present": (
                        conversation_context_digest is not None
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        try:
            self._submit_write(
                trace_id,
                lambda connection: connection.execute(
                    "INSERT INTO query_history (trace_id, project_id, "
                    "knowledge_base_id, owner_id, created_at, expires_at, "
                    "status, question_sha256, body_saved, ciphertext, nonce, "
                    "instance_id, process_id, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'STARTED', ?, ?, ?, ?, ?, ?, ?)",
                    parameters,
                ),
            )
        except (sqlite3.Error, ProviderUnavailable) as error:
            raise self._unavailable(trace_id) from error

    def finish(
        self,
        trace_id: str,
        *,
        result: SearchAnswerResult | None,
        error: RagError | None,
        cancelled: bool,
        cancelled_calls: tuple[ProviderCall, ...] = (),
    ) -> None:
        """记录实际终态、候选决定和计量；从不持久化模型思考过程。

        Args:
            trace_id: 已建立的请求标识。
            result: 实际查询结果，可为空。
            error: 安全异常，可为空。
            cancelled: 是否发生取消。
            cancelled_calls: 取消前已经结算的脱敏 Provider 调用。

        Returns:
            最终状态落盘后无返回值。

        """
        try:
            self._submit_write(
                trace_id,
                lambda connection: self._finish_in_transaction(
                    connection,
                    trace_id,
                    result=result,
                    error=error,
                    cancelled=cancelled,
                    cancelled_calls=cancelled_calls,
                ),
            )
        except (sqlite3.Error, ProviderUnavailable) as failure:
            raise self._unavailable(trace_id) from failure

    def _finish_in_transaction(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        trace_id: str,
        *,
        result: SearchAnswerResult | None,
        error: RagError | None,
        cancelled: bool,
        cancelled_calls: tuple[ProviderCall, ...],
    ) -> None:
        """在 writer 批次内把一条 STARTED History 结算为唯一终态。

        Args:
            connection: 已进入外层写事务和当前命令 savepoint 的连接。
            trace_id: 待结算的请求标识。
            result: 可选成功或拒答结果。
            error: 可选安全业务错误。
            cancelled: 是否按取消终态结算。
            cancelled_calls: 取消前已结算的安全 Provider 调用。

        Returns:
            当前命令的 SQL 更新完成后无返回值；durable commit 由批次负责。

        """
        row = connection.execute(
            "SELECT * FROM query_history WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
        if row is None:
            raise self._unavailable(trace_id)
        now = datetime.now(UTC)
        status, metadata = _completion(
            result,
            error,
            cancelled,
            cancelled_calls,
        )
        start_metadata = cast(
            dict[str, object], json.loads(row["metadata_json"])
        )
        metadata = {**start_metadata, **metadata}
        payload = self._decode(row)
        if result is not None:
            payload["answer"] = result.answer
            payload["result"] = result.model_dump(mode="json")
        ciphertext, nonce = self._encode(
            trace_id, payload, enabled=bool(row["body_saved"])
        )
        elapsed = now - datetime.fromisoformat(row["created_at"])
        connection.execute(
            "UPDATE query_history SET finished_at=?, status=?, "
            "duration_ms=?, metadata_json=?, ciphertext=?, nonce=? "
            "WHERE trace_id=? AND status='STARTED'",
            (
                now.isoformat(),
                status,
                max(0, int(elapsed.total_seconds() * 1000)),
                json.dumps(metadata, ensure_ascii=False),
                ciphertext,
                nonce,
                trace_id,
            ),
        )

    def record(self, event: TraceEvent) -> None:
        """实现当前 TracePort，只写不含正文的结构化事件。

        Args:
            event: 已脱敏的阶段事件。

        Returns:
            事件落盘后无返回值。

        """
        self.record_many((event,))

    def record_many(self, events: Sequence[TraceEvent]) -> None:
        """在一个事务中保存同一查询的有界兼容事件批次。

        Args:
            events: 同属一个 Trace 的安全事件序列。

        Returns:
            无返回值。

        """
        if not events:
            return
        trace_id = events[0].trace_id
        if any(event.trace_id != trace_id for event in events):
            raise ValueError("兼容事件批次必须属于同一 Trace。")
        try:
            with (
                self._write_lock,
                self._connections.transaction(write=True) as connection,
            ):
                self._insert_events(connection, events)
        except (sqlite3.Error, ProviderUnavailable) as error:
            raise self._unavailable(trace_id) from error

    @staticmethod
    def _insert_events(
        connection: sqlite3.Connection,
        events: Sequence[TraceEvent],
    ) -> None:
        """把同一查询的兼容事件批量写入调用方事务。"""
        connection.executemany(
            "INSERT INTO query_trace_events (trace_id, occurred_at, "
            "event_name, payload_json) VALUES (?, ?, ?, ?)",
            (
                (
                    event.trace_id,
                    event.occurred_at.isoformat(),
                    event.event_name,
                    event.model_dump_json(),
                )
                for event in events
            ),
        )

    def events(self, trace_id: str) -> tuple[TraceEvent, ...]:
        """按原顺序读取跨重启仍存在的安全事件。

        Args:
            trace_id: 已授权读取的请求标识。

        Returns:
            按写入顺序排列的事件元组。

        """
        with self._connections.transaction() as connection:
            stored_trace_id = _stored_event_trace_id(connection, trace_id)
            if stored_trace_id is None:
                return ()
            rows = connection.execute(
                "SELECT payload_json FROM query_trace_events "
                "WHERE trace_id=? ORDER BY sequence",
                (stored_trace_id,),
            ).fetchall()
        return tuple(
            TraceEvent.model_validate(
                {
                    **_validated_event_payload(str(row[0]), trace_id),
                    "trace_id": normalize_trace_id(trace_id),
                }
            )
            for row in rows
        )

    def event_payloads(
        self,
        trace_id: str,
        *,
        max_total_bytes: int | None = None,
    ) -> tuple[dict[str, object], ...]:
        """读取兼容旧 32hex ID 的安全 flat event JSON。

        Args:
            trace_id: 新式或旧式共享 Trace ID。
            max_total_bytes: 可选的原始 JSON 累计字节上限。

        Returns:
            与数据库 sequence 相同顺序的 JSON object。

        Raises:
            ProviderUnavailable: 事件库不可读、JSON 损坏或身份不一致。

        """
        if max_total_bytes is not None and max_total_bytes <= 0:
            raise ValueError("flat event 字节上限必须为正数。")
        try:
            with self._connections.transaction() as connection:
                stored_trace_id = _stored_event_trace_id(connection, trace_id)
                if stored_trace_id is None:
                    return ()
                rows = connection.execute(
                    "SELECT payload_json FROM query_trace_events "
                    "WHERE trace_id=? ORDER BY sequence",
                    (stored_trace_id,),
                ).fetchall()
                payloads: list[dict[str, object]] = []
                total_bytes = 0
                for row in rows:
                    raw_payload = str(row["payload_json"])
                    total_bytes += len(raw_payload.encode("utf-8"))
                    if (
                        max_total_bytes is not None
                        and total_bytes > max_total_bytes
                    ):
                        raise HistorySnapshotLimitError(
                            "EXPORT_TOTAL_BYTES_EXCEEDED"
                        )
                    payloads.append(
                        _validated_event_payload(raw_payload, trace_id)
                    )
            return tuple(payloads)
        except HistorySnapshotLimitError:
            raise
        except (sqlite3.Error, ValueError) as error:
            raise self._unavailable(trace_id) from error

    def diagnostics(self, trace_id: str) -> RetrievalDiagnostics:
        """兼容 SDK 原有诊断读取语义，数据来自权威主库。

        Args:
            trace_id: 待读取的请求标识。

        Returns:
            持久化的安全检索诊断。

        """
        with self._connections.transaction() as connection:
            stored_trace_id = _stored_history_trace_id(connection, trace_id)
            if stored_trace_id is None:
                raise NotFound("检索过程不存在或已过期。", stage="history.read")
            row = connection.execute(
                "SELECT metadata_json FROM query_history "
                "WHERE trace_id=? AND expires_at>?",
                (stored_trace_id, datetime.now(UTC).isoformat()),
            ).fetchone()
        if row is None:
            raise NotFound("检索过程不存在或已过期。", stage="history.read")
        return RetrievalDiagnostics.model_validate(
            json.loads(row[0]).get("diagnostics", {})
        )

    def list_history(  # noqa: PLR0913
        self,
        *,
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
        owner_id: str | None = None,
        status: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        keyword: str | None = None,
        page_size: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        """按范围和时间分页，正文关键词只在重新鉴权后匹配。

        Args:
            project_id: 可选项目筛选。
            knowledge_base_id: 可选知识库筛选。
            owner_id: 可选会话或 Token 主体。
            status: 可选最终状态。
            created_from: 可选开始时间。
            created_to: 可选结束时间。
            keyword: 仅在获准正文中搜索的关键词。
            page_size: 每页记录数量。
            offset: 已跳过记录数量。

        Returns:
            历史条目、总量和本地保存策略。

        """
        if not 1 <= page_size <= _MAX_PAGE_SIZE or offset < 0:
            raise ValueError("历史分页越界。")
        conditions = ["expires_at>?"]
        parameters: list[object] = [datetime.now(UTC).isoformat()]
        for column, value in (
            ("project_id", project_id),
            ("knowledge_base_id", knowledge_base_id),
            ("owner_id", owner_id),
            ("status", status),
        ):
            if value is not None:
                conditions.append(column + "=?")
                parameters.append(value)
        for operator, value in ((">=", created_from), ("<=", created_to)):
            if value is not None:
                conditions.append("created_at" + operator + "?")
                parameters.append(value)
        # 列名和运算符均来自以上固定集合，用户值只进入绑定参数。
        where_clause = " AND ".join(conditions)
        ordered_query = (
            "SELECT * FROM query_history WHERE "  # noqa: S608
            + where_clause
            + " ORDER BY created_at DESC, trace_id DESC"
        )
        with self._connections.transaction() as connection:
            if not keyword:
                total = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM query_history WHERE "  # noqa: S608
                        + where_clause,
                        parameters,
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    ordered_query + " LIMIT ? OFFSET ?",
                    (*parameters, page_size, offset),
                ).fetchall()
                items = [
                    self._view(connection, row, detail=False) for row in rows
                ]
                search_complete = True
                scanned_count = len(rows)
                truncation_reason = None
            else:
                (
                    items,
                    total,
                    search_complete,
                    scanned_count,
                    truncation_reason,
                ) = self._keyword_page(
                    connection,
                    ordered_query,
                    parameters,
                    keyword=keyword,
                    page_size=page_size,
                    offset=offset,
                )
        result: dict[str, object] = {
            "items": items,
            "total": total,
            "total_is_exact": search_complete,
            "offset": offset,
            "page_size": page_size,
            "body_enabled": self.save_body,
            "retention_days": self.retention_days,
            "storage": "local_encrypted_sqlite",
            "search_complete": search_complete,
            "scanned_count": scanned_count,
            "next_cursor": None,
        }
        if truncation_reason is not None:
            result.update(
                {
                    "truncation_reason": truncation_reason,
                    "candidate_scan_limit": _MAX_KEYWORD_SCAN_RECORDS,
                    "time_scan_limit_ms": int(
                        _MAX_KEYWORD_SCAN_SECONDS * 1_000
                    ),
                }
            )
        return result

    def _keyword_page(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        ordered_query: str,
        parameters: Sequence[object],
        *,
        keyword: str,
        page_size: int,
        offset: int,
    ) -> tuple[list[dict[str, object]], int, bool, int, str | None]:
        """在固定候选数和墙钟预算内搜索获准解密的正文。

        Args:
            connection: 当前一致读事务。
            ordered_query: 仅由固定列构成并已带排序的 SQL。
            parameters: SQL 绑定参数。
            keyword: 调用方提供的正文关键词。
            page_size: 返回页大小。
            offset: 匹配结果偏移量。

        Returns:
            页面、已知匹配数、是否扫完、扫描数和截断原因。

        """
        items: list[dict[str, object]] = []
        matched_count = 0
        scanned_count = 0
        search_complete = True
        truncation_reason: str | None = None
        deadline = monotonic() + _MAX_KEYWORD_SCAN_SECONDS
        normalized_keyword = keyword.casefold()
        rows = connection.execute(ordered_query, parameters)
        for row in rows:
            if scanned_count >= _MAX_KEYWORD_SCAN_RECORDS:
                search_complete = False
                truncation_reason = "CANDIDATE_LIMIT"
                break
            if scanned_count and monotonic() >= deadline:
                search_complete = False
                truncation_reason = "TIME_LIMIT"
                break
            scanned_count += 1
            # _view 会先重验 Revision、Document 和 Version，再决定是否解密。
            item = self._view(connection, row, detail=False)
            question = str(item.get("question") or "")
            if normalized_keyword not in question.casefold():
                continue
            if offset <= matched_count < offset + page_size:
                items.append(item)
            matched_count += 1
        return (
            items,
            matched_count,
            search_complete,
            scanned_count,
            truncation_reason,
        )

    def detail(
        self,
        trace_id: str,
        *,
        project_id: str | None = None,
        knowledge_base_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, object]:
        """复核调用方范围及来源删除状态后返回正文和完整阶段。

        Args:
            trace_id: 待读取请求标识。
            project_id: 可选的强制项目范围。
            knowledge_base_id: 可选的强制知识库范围。
            owner_id: 可选的强制会话主体。

        Returns:
            经授权的正文、引用和阶段详情。

        """
        with self._connections.transaction() as connection:
            stored_trace_id = _stored_history_trace_id(connection, trace_id)
            if stored_trace_id is None:
                raise NotFound("历史不存在或无权查看。", stage="history.read")
            row = connection.execute(
                "SELECT * FROM query_history WHERE trace_id=? AND expires_at>?",
                (stored_trace_id, datetime.now(UTC).isoformat()),
            ).fetchone()
            if row is None or any(
                expected is not None and row[name] != expected
                for name, expected in (
                    ("project_id", project_id),
                    ("knowledge_base_id", knowledge_base_id),
                    ("owner_id", owner_id),
                )
            ):
                raise NotFound("历史不存在或无权查看。", stage="history.read")
            item = self._view(connection, row, detail=True)
        item["events"] = list(self.event_payloads(stored_trace_id))
        return item

    @contextmanager
    def export_snapshots(  # noqa: PLR0913
        self,
        trace_ids: Sequence[str],
        *,
        include_body: bool,
        body_authorized: bool,
        now: datetime | None = None,
        max_item_bytes: int | None = None,
        max_total_bytes: int | None = None,
    ) -> Iterator[dict[str, HistoryExportSnapshot]]:
        """在持久 lease 内物化一组问答历史快照。

        Args:
            trace_ids: 已经过严格格式校验且没有重复项的 Trace ID。
            include_body: 调用方是否显式请求正文。
            body_authorized: 当前主体是否为获准导出正文的管理员。
            now: 可选的冻结导出时间，测试可据此验证稳定输出。
            max_item_bytes: 可选的单条 canonical History JSON 字节上限。
            max_total_bytes: 可选的全部 History JSON 累计字节上限。

        Yields:
            以 Trace ID 为键的历史快照；缺失记录也有明确占位。

        Returns:
            管理持久导出 lease 的上下文迭代器。

        Raises:
            HistorySnapshotLimitError: 快照命中调用方提供的字节上限。
            ProviderUnavailable: journal、lease 或历史读取不可用。

        """
        if not trace_ids:
            raise ValueError("History 导出至少需要一个 Trace ID。")
        if len(trace_ids) != len(set(trace_ids)):
            raise ValueError("History 导出不接受重复 Trace ID。")
        if any(
            _TRACE_ID_PATTERN.fullmatch(trace_id) is None
            for trace_id in trace_ids
        ):
            raise ValueError("History 导出 Trace ID 格式无效。")
        normalized_trace_ids = tuple(
            normalize_trace_id(trace_id) for trace_id in trace_ids
        )
        if len(normalized_trace_ids) != len(set(normalized_trace_ids)):
            raise ValueError("History 导出不接受等价的新旧重复 ID。")
        for limit in (max_item_bytes, max_total_bytes):
            if limit is not None and limit <= 0:
                raise ValueError("History 导出字节上限必须为正数。")
        lease_now = datetime.now(UTC)
        frozen_now = (now or lease_now).astimezone(UTC)
        export_id = f"hexp_{uuid.uuid4().hex}"
        expires_at = lease_now + timedelta(seconds=_EXPORT_LEASE_SECONDS)
        requested_sha256 = hashlib.sha256(
            json.dumps(
                list(trace_ids),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        snapshots: dict[str, HistoryExportSnapshot] = {}
        try:
            with (
                self._write_lock,
                self._connections.transaction(write=True) as connection,
            ):
                _purge_export_journal(connection, lease_now)
                connection.execute(
                    "INSERT INTO history_export_journal("
                    "export_id, requested_sha256, state, created_at, "
                    "expires_at) "
                    "VALUES (?, ?, 'ACTIVE', ?, ?)",
                    (
                        export_id,
                        requested_sha256,
                        lease_now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                connection.executemany(
                    "INSERT INTO history_export_leases("
                    "export_id, trace_id, expires_at) VALUES (?, ?, ?)",
                    (
                        (export_id, alias, expires_at.isoformat())
                        for canonical in normalized_trace_ids
                        for alias in (
                            canonical,
                            canonical.removeprefix("trace_"),
                        )
                    ),
                )
                total_bytes = 0
                for trace_id in trace_ids:
                    snapshot = self._export_snapshot(
                        connection,
                        trace_id,
                        include_body=include_body,
                        body_authorized=body_authorized,
                        now=frozen_now,
                        max_payload_bytes=max_item_bytes,
                    )
                    item_bytes = len(_canonical_json_bytes(snapshot.payload))
                    if (
                        max_item_bytes is not None
                        and item_bytes > max_item_bytes
                    ):
                        raise HistorySnapshotLimitError(
                            "EXPORT_MEMBER_BYTES_EXCEEDED"
                        )
                    total_bytes += item_bytes
                    if (
                        max_total_bytes is not None
                        and total_bytes > max_total_bytes
                    ):
                        raise HistorySnapshotLimitError(
                            "EXPORT_TOTAL_BYTES_EXCEEDED"
                        )
                    snapshots[trace_id] = snapshot
        except HistorySnapshotLimitError:
            raise
        except (sqlite3.Error, ProviderUnavailable, ValueError) as error:
            raise self._unavailable("history-export") from error

        completed = False
        try:
            yield snapshots
            completed = True
        finally:
            self._finish_export(
                export_id,
                completed=completed,
                now=datetime.now(UTC),
            )

    def record_scope(self, trace_id: str) -> tuple[str, str] | None:
        """返回未按保留期过滤的 History 项目与知识库身份。

        Args:
            trace_id: 当前或旧格式 Trace ID。

        Returns:
            Project 与知识库 ID；记录不存在时为 None。

        """
        try:
            with self._connections.transaction() as connection:
                stored_trace_id = _stored_history_trace_id(connection, trace_id)
                if stored_trace_id is None:
                    return None
                row = connection.execute(
                    "SELECT project_id, knowledge_base_id FROM query_history "
                    "WHERE trace_id=?",
                    (stored_trace_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise self._unavailable(trace_id) from error
        if row is None:
            return None
        return str(row["project_id"]), str(row["knowledge_base_id"])

    def has_record(self, trace_id: str) -> bool:
        """判断 History 是否仍保存指定 ID，不按正文或到期策略过滤。

        Args:
            trace_id: 当前或旧格式 Trace ID。

        Returns:
            任一等价 History 记录存在时为 True。

        """
        return self.record_scope(trace_id) is not None

    def clear(self, *, expired_only: bool = False) -> int:
        """清理历史和旧平面事件；不触碰独立 Operational Trace。

        Args:
            expired_only: 是否仅清理已到期记录。

        Returns:
            实际清理的历史记录数。

        """
        with (
            self._write_lock,
            self._connections.transaction(write=True) as connection,
        ):
            now = datetime.now(UTC)
            _purge_export_journal(connection, now)
            if _export_conflicts_with_clear(
                connection,
                now=now,
                expired_only=expired_only,
            ):
                raise Conflict(
                    "问答历史支持包正在导出，请稍后重试清理。",
                    stage="history.clear",
                    retryable=True,
                )
            if expired_only:
                now_text = now.isoformat()
                connection.execute(
                    "DELETE FROM query_trace_events WHERE trace_id IN "
                    "(SELECT trace_id FROM query_history WHERE expires_at<=?)",
                    (now_text,),
                )
                cursor = connection.execute(
                    "DELETE FROM query_history WHERE expires_at<=?", (now_text,)
                )
            else:
                connection.execute("DELETE FROM query_trace_events")
                cursor = connection.execute("DELETE FROM query_history")
        return cursor.rowcount

    def _export_snapshot(  # noqa: PLR0913
        self,
        connection: sqlite3.Connection,
        trace_id: str,
        *,
        include_body: bool,
        body_authorized: bool,
        now: datetime,
        max_payload_bytes: int | None,
    ) -> HistoryExportSnapshot:
        """从当前写事务物化一条不泄漏正文的导出视图。"""
        stored_history_id = _stored_history_trace_id(connection, trace_id)
        row = (
            None
            if stored_history_id is None
            else connection.execute(
                "SELECT * FROM query_history WHERE trace_id=?",
                (stored_history_id,),
            ).fetchone()
        )
        stored_event_id = _stored_event_trace_id(connection, trace_id)
        event_rows = (
            ()
            if stored_event_id is None
            else connection.execute(
                "SELECT payload_json FROM query_trace_events "
                "WHERE trace_id=? ORDER BY sequence",
                (stored_event_id,),
            )
        )
        events: list[dict[str, object]] = []
        event_bytes = 0
        for event in event_rows:
            raw_event = str(event["payload_json"])
            event_bytes += len(raw_event.encode("utf-8"))
            if (
                max_payload_bytes is not None
                and event_bytes > max_payload_bytes
            ):
                raise HistorySnapshotLimitError("EXPORT_MEMBER_BYTES_EXCEEDED")
            events.append(_validated_event_payload(raw_event, trace_id))
        if row is None:
            reason = "HISTORY_MISSING"
            return HistoryExportSnapshot(
                trace_id=trace_id,
                history_status="MISSING",
                project_id=None,
                knowledge_base_id=None,
                payload={
                    "trace_id": trace_id,
                    "history_status": "MISSING",
                    "body_included": False,
                    "body_unavailable_reason": reason,
                    "events": events,
                },
                body_included=False,
                body_unavailable_reason=reason,
            )

        raw_metadata = str(row["metadata_json"])
        if (
            max_payload_bytes is not None
            and len(raw_metadata.encode("utf-8")) > max_payload_bytes
        ):
            raise HistorySnapshotLimitError("EXPORT_MEMBER_BYTES_EXCEEDED")
        metadata = cast(dict[str, object], json.loads(raw_metadata))
        source_reason = _source_unavailable_reason(connection, row, metadata)
        expired = datetime.fromisoformat(str(row["expires_at"])) <= now
        body_reason = _body_unavailable_reason(
            requested=include_body,
            authorized=body_authorized,
            policy_enabled=self.save_body,
            body_saved=bool(row["body_saved"]),
            expired=expired,
            source_reason=source_reason,
        )
        body_included = body_reason is None
        history_status = "EXPIRED" if expired else "AVAILABLE"
        payload: dict[str, object] = {
            name: row[name]
            for name in (
                "trace_id",
                "project_id",
                "knowledge_base_id",
                "created_at",
                "finished_at",
                "status",
                "duration_ms",
                "expires_at",
                "question_sha256",
            )
        }
        payload.update(metadata)
        payload.update(
            {
                "history_status": history_status,
                "body_saved": bool(row["body_saved"]),
                "body_included": body_included,
                "body_unavailable_reason": body_reason,
                "events": events,
            }
        )
        if body_included:
            ciphertext = row["ciphertext"]
            if (
                max_payload_bytes is not None
                and isinstance(ciphertext, str)
                and len(ciphertext.encode("ascii")) > max_payload_bytes * 2
            ):
                raise HistorySnapshotLimitError("EXPORT_MEMBER_BYTES_EXCEEDED")
            decoded = self._decode(row)
            payload.update(
                {
                    "question": decoded.get("question"),
                    "answer": decoded.get("answer"),
                    "result": decoded.get("result"),
                }
            )
        return HistoryExportSnapshot(
            trace_id=trace_id,
            history_status=history_status,
            project_id=str(row["project_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            payload=payload,
            body_included=body_included,
            body_unavailable_reason=body_reason,
        )

    def _finish_export(
        self,
        export_id: str,
        *,
        completed: bool,
        now: datetime,
    ) -> None:
        """释放持久 lease，并保留无正文的完成 journal。"""
        try:
            with (
                self._write_lock,
                self._connections.transaction(write=True) as connection,
            ):
                connection.execute(
                    "DELETE FROM history_export_leases WHERE export_id=?",
                    (export_id,),
                )
                connection.execute(
                    "UPDATE history_export_journal SET state=?, finished_at=?, "
                    "failure_code=? WHERE export_id=?",
                    (
                        "COMPLETED" if completed else "FAILED",
                        now.astimezone(UTC).isoformat(),
                        None if completed else "EXPORT_ABORTED",
                        export_id,
                    ),
                )
        except sqlite3.Error as error:
            raise self._unavailable("history-export-release") from error

    def close(self) -> None:
        """仅将本实例剩余请求收尾，不中断共用数据库的其他进程。

        Args:
            无参数；只处理当前实例未终态请求。

        Returns:
            收尾后无返回值。

        """
        self._stop_writer()
        try:
            with (
                self._write_lock,
                self._connections.transaction(write=True) as connection,
            ):
                rows = connection.execute(
                    "SELECT trace_id, metadata_json FROM query_history "
                    "WHERE status='STARTED' AND instance_id=?",
                    (self._instance_id,),
                ).fetchall()
                finished_at = datetime.now(UTC).isoformat()
                for row in rows:
                    metadata = cast(
                        dict[str, object],
                        json.loads(str(row["metadata_json"])),
                    )
                    metadata["reason_code"] = "PROCESS_INTERRUPTED"
                    connection.execute(
                        "UPDATE query_history SET status='INTERRUPTED', "
                        "finished_at=?, metadata_json=? "
                        "WHERE status='STARTED' AND trace_id=?",
                        (
                            finished_at,
                            json.dumps(
                                metadata,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            row["trace_id"],
                        ),
                    )
        except (sqlite3.Error, ProviderUnavailable, ValueError):
            self._unavailable("shutdown")

    def _view(
        self, connection: sqlite3.Connection, row: sqlite3.Row, *, detail: bool
    ) -> dict[str, object]:
        metadata = json.loads(row["metadata_json"])
        readable = _sources_readable(connection, row, metadata)
        payload = self._decode(row) if readable else {}
        item = {
            name: row[name]
            for name in (
                "trace_id",
                "project_id",
                "knowledge_base_id",
                "created_at",
                "finished_at",
                "status",
                "duration_ms",
                "expires_at",
            )
        }
        item.update(metadata)
        item.update(
            {
                "body_saved": bool(row["body_saved"]),
                "body_available": bool(row["body_saved"]) and readable,
                "body_message": (
                    "来源已删除或范围已失效，正文不可查看。"
                    if not readable
                    else (
                        "本地加密保存"
                        if row["body_saved"]
                        else "本次未保存正文"
                    )
                ),
                "question": payload.get("question"),
                "answer": payload.get("answer") if detail else None,
                "answer_summary": str(payload.get("answer") or "")[:240],
            }
        )
        if detail and readable:
            item["result"] = payload.get("result")
        if not detail:
            item.pop("diagnostics", None)
        return item

    def _encode(
        self, trace_id: str, payload: dict[str, object], *, enabled: bool
    ) -> tuple[str | None, str | None]:
        if not enabled:
            return None, None
        text = json.dumps(_redact_payload(payload), ensure_ascii=False)
        return self._cipher.encrypt(text, aad=_aad(trace_id))

    def _decode(self, row: sqlite3.Row) -> dict[str, object]:
        if not row["body_saved"] or row["ciphertext"] is None:
            return {}
        return cast(
            dict[str, object],
            json.loads(
                self._cipher.decrypt(
                    row["ciphertext"], row["nonce"], aad=_aad(row["trace_id"])
                )
            ),
        )

    @staticmethod
    def _unavailable(trace_id: str) -> ProviderUnavailable:
        _LOGGER.error("TRACE_PERSISTENCE_UNAVAILABLE request_id=%s", trace_id)
        return ProviderUnavailable(
            "本地问答历史无法持久化，请检查数据目录和可用空间。",
            code="TRACE_PERSISTENCE_UNAVAILABLE",
            stage="history.persistence",
            trace_id=trace_id if trace_id.startswith("trace_") else None,
        )


def _stored_history_trace_id(
    connection: sqlite3.Connection, trace_id: str
) -> str | None:
    """按 canonical 优先级解析新旧 History ID 的实际存储形式。"""
    canonical = normalize_trace_id(trace_id)
    legacy = canonical.removeprefix("trace_")
    row = connection.execute(
        "SELECT trace_id FROM query_history WHERE trace_id IN (?, ?) "
        "ORDER BY CASE WHEN trace_id=? THEN 0 ELSE 1 END LIMIT 1",
        (canonical, legacy, canonical),
    ).fetchone()
    return None if row is None else str(row["trace_id"])


def _stored_event_trace_id(
    connection: sqlite3.Connection, trace_id: str
) -> str | None:
    """按 canonical 优先级解析新旧 flat event ID 的实际存储形式。"""
    canonical = normalize_trace_id(trace_id)
    legacy = canonical.removeprefix("trace_")
    row = connection.execute(
        "SELECT trace_id FROM query_trace_events WHERE trace_id IN (?, ?) "
        "ORDER BY CASE WHEN trace_id=? THEN 0 ELSE 1 END LIMIT 1",
        (canonical, legacy, canonical),
    ).fetchone()
    return None if row is None else str(row["trace_id"])


def _aad(trace_id: str) -> SecretAad:
    return SecretAad(
        credential_id=trace_id,
        provider_type="local-query-history",
        field_name="history-payload",
        key_version=1,
    )


def _process_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _redact_payload(value: object) -> object:
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    """按支持包一致口径计算 History 快照字节。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validated_event_payload(
    raw_payload: str,
    expected_trace_id: str,
) -> dict[str, object]:
    """校验旧事件 schema，同时仅为模型校验临时补齐 trace_ 前缀。"""
    return _validated_event_object(json.loads(raw_payload), expected_trace_id)


def _validated_event_object(
    payload: object,
    expected_trace_id: str,
) -> dict[str, object]:
    """校验数据库行或新式兼容投影中的单个安全事件。"""
    if not isinstance(payload, dict):
        raise ValueError("flat event Trace 身份不一致。")
    stored_trace_id = payload.get("trace_id")
    if not isinstance(stored_trace_id, str):
        raise ValueError("flat event Trace 身份不一致。")
    try:
        identities_match = normalize_trace_id(
            stored_trace_id
        ) == normalize_trace_id(expected_trace_id)
    except ValueError as error:
        raise ValueError("flat event Trace 身份不一致。") from error
    if not identities_match:
        raise ValueError("flat event Trace 身份不一致。")
    candidate = dict(payload)
    candidate["trace_id"] = normalize_trace_id(expected_trace_id)
    validated = TraceEvent.model_validate(candidate).model_dump(mode="json")
    validated["trace_id"] = expected_trace_id
    return cast(dict[str, object], validated)


def _source_unavailable_reason(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    metadata: dict[str, object],
) -> str | None:
    """区分范围失效与引用文档失效，避免导出端猜测。"""
    scope = connection.execute(
        "SELECT 1 FROM knowledge_bases k JOIN projects p "
        "ON p.project_id=k.project_id WHERE k.project_id=? "
        "AND k.knowledge_base_id=? AND k.deleted_at IS NULL "
        "AND p.deleted_at IS NULL",
        (row["project_id"], row["knowledge_base_id"]),
    ).fetchone()
    if scope is None:
        return "SCOPE_UNAVAILABLE"
    for document_id in cast(list[str], metadata.get("document_ids", [])):
        found = connection.execute(
            "SELECT 1 FROM documents WHERE document_id=? AND project_id=? "
            "AND knowledge_base_id=? AND deleted_at IS NULL "
            "AND lifecycle_status='active'",
            (document_id, row["project_id"], row["knowledge_base_id"]),
        ).fetchone()
        if found is None:
            return "SOURCE_UNAVAILABLE"
    return None


def _sources_readable(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    metadata: dict[str, object],
) -> bool:
    return _source_unavailable_reason(connection, row, metadata) is None


def _body_unavailable_reason(  # noqa: PLR0913
    *,
    requested: bool,
    authorized: bool,
    policy_enabled: bool,
    body_saved: bool,
    expired: bool,
    source_reason: str | None,
) -> str | None:
    """按固定优先级返回正文未进入支持包的稳定原因。"""
    if not requested:
        return "NOT_REQUESTED"
    if not authorized:
        return "PRINCIPAL_NOT_AUTHORIZED"
    if not policy_enabled:
        return "POLICY_DISABLED"
    if not body_saved:
        return "BODY_NOT_SAVED"
    if expired:
        return "HISTORY_EXPIRED"
    return source_reason


def _purge_export_journal(
    connection: sqlite3.Connection,
    now: datetime,
) -> None:
    """删除已过期 journal；外键级联清除崩溃遗留 lease。"""
    connection.execute(
        "DELETE FROM history_export_journal WHERE expires_at<=?",
        (now.astimezone(UTC).isoformat(),),
    )


def _export_conflicts_with_clear(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    expired_only: bool,
) -> bool:
    """仅在清理会触碰 lease 所保护的 History/flat event 时冲突。"""
    now_text = now.astimezone(UTC).isoformat()
    if expired_only:
        row = connection.execute(
            "SELECT 1 FROM history_export_leases l "
            "JOIN query_history h ON h.trace_id=l.trace_id "
            "WHERE l.expires_at>? AND h.expires_at<=? LIMIT 1",
            (now_text, now_text),
        ).fetchone()
        return row is not None
    row = connection.execute(
        "SELECT 1 FROM history_export_leases l WHERE l.expires_at>? AND ("
        "EXISTS(SELECT 1 FROM query_history h WHERE h.trace_id=l.trace_id) "
        "OR EXISTS(SELECT 1 FROM query_trace_events e "
        "WHERE e.trace_id=l.trace_id)) LIMIT 1",
        (now_text,),
    ).fetchone()
    return row is not None


def _completion(
    result: SearchAnswerResult | None,
    error: RagError | None,
    cancelled: bool,
    cancelled_calls: tuple[ProviderCall, ...] = (),
) -> tuple[str, dict[str, object]]:
    metadata: dict[str, object] = {}
    calls: tuple[ProviderCall, ...] = ()
    if result is not None:
        status = "ANSWERED" if result.answer else "REFUSED"
        metadata.update(
            {
                "reason_code": result.reason_code,
                "cache_hit": result.cache_hit,
                "generation_mode": result.generation_mode,
                "generation_reason_code": result.generation_reason_code,
                "degraded_reason_codes": list(result.degraded_reason_codes),
                "requested_answer_type": (result.requested_answer_type.value),
                "query_semantic_source": result.query_semantic_source,
                "active_index_revision_id": result.active_index_revision_id,
                "index_fingerprint": result.index_fingerprint,
                "serving_fingerprint": result.serving_fingerprint,
                "document_ids": sorted(
                    {
                        item.document_id
                        for item in result.evidence
                        if item.document_id is not None
                    }
                    | {item.document_id for item in result.related_contents}
                ),
            }
        )
        if result.data_plane is not None:
            metadata["data_plane"] = result.data_plane.model_dump(mode="json")
        if result.diagnostics is not None:
            metadata["diagnostics"] = result.diagnostics.model_dump(mode="json")
            calls = result.diagnostics.provider_call_details
        if result.generation_mode == "extractive_fallback":
            metadata["fallback_answer_available"] = bool(result.answer)
    elif cancelled:
        status = "CANCELLED"
        metadata["reason_code"] = "REQUEST_CANCELLED"
        calls = cancelled_calls
    else:
        status = "FAILED"
        metadata["reason_code"] = error.code if error else "INTERNAL_ERROR"
        metadata["error_stage"] = error.stage if error else "query"
        if error:
            calls = error.provider_calls or (
                () if error.provider_call is None else (error.provider_call,)
            )
            metadata["provider_calls"] = [
                call.model_dump(mode="json") for call in calls
            ]
    metadata["provider_usage"] = _usage_summary(calls, result, error)
    metadata["models"] = sorted({call.model for call in calls if call.model})
    return status, metadata


def _usage_summary(
    calls: tuple[ProviderCall, ...],
    result: SearchAnswerResult | None,
    error: RagError | None,
) -> list[dict[str, object]]:
    operations = {call.operation for call in calls} | {
        "embedding.query",
        "reranking",
        "generation",
        "query.interpret",
        "query.rewrite",
    }
    items = []
    for operation in sorted(operations):
        actual = [call for call in calls if call.operation == operation]
        count = sum(call.call_count for call in actual)
        reason = next(
            (call.reason_code for call in actual if call.reason_code), None
        )
        if count == 0 and reason is None:
            if result is not None and result.cache_hit:
                reason = "CACHE_HIT"
            elif error is not None:
                reason = error.code
            elif operation == "generation":
                reason = (
                    result.generation_reason_code
                    if result is not None
                    else None
                ) or "GENERATOR_NOT_CONFIGURED"
            elif operation == "query.rewrite":
                reason = (
                    result.rewrite_reason_code if result is not None else None
                ) or "REWRITE_NOT_CONFIGURED"
            elif operation == "query.interpret":
                reason = (
                    result.interpret_reason_code if result is not None else None
                ) or "INTERPRET_NOT_CONFIGURED"
            else:
                reason = "LOCAL_OR_NOT_REQUIRED_BY_PLAN"
        observed = [call.observed_tokens for call in actual]
        items.append(
            {
                "operation": operation,
                "call_count": count,
                "reason_code": reason,
                "usage": (
                    sum(cast(list[int], observed))
                    if observed and all(value is not None for value in observed)
                    else ("unknown" if count else None)
                ),
            }
        )
    return items
