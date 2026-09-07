"""复用产品主库和现有加密工具保存本机问答历史。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import NotFound, ProviderUnavailable, RagError
from rag_app.core.events import TraceEvent
from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.provider import ProviderCall
from rag_app.core.models.search import RetrievalDiagnostics, SearchAnswerResult
from rag_app.product.crypto import SecretAad, SecretCipher

_LOGGER = logging.getLogger(__name__)
_MAX_PAGE_SIZE = 200
_MAX_RETENTION_DAYS = 365
_SECRET_TEXT = re.compile(
    r"(?i)(?:\b(?:authorization|cookie|api[_-]?key)\s*[:=]\s*"
    r"[^\r\n]+|\bBearer\s+\S+|\bsk-[a-zA-Z0-9_-]{12,})"
)


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

    def recover(self) -> None:
        """启动时将未终态请求标记中断，清理已到期记录。

        Args:
            无参数；核对主库中的存活进程。

        Returns:
            恢复完成时无返回值。

        """
        now = datetime.now(UTC).isoformat()
        try:
            with self._connections.transaction(write=True) as connection:
                rows = connection.execute(
                    "SELECT DISTINCT process_id FROM query_history "
                    "WHERE status='STARTED'"
                ).fetchall()
                for row in rows:
                    if not _process_alive(int(row[0])):
                        connection.execute(
                            "UPDATE query_history SET status='INTERRUPTED', "
                            "finished_at=?, metadata_json=? "
                            "WHERE status='STARTED' AND process_id=?",
                            (
                                now,
                                json.dumps(
                                    {"reason_code": "PROCESS_INTERRUPTED"}
                                ),
                                row[0],
                            ),
                        )
            self.clear(expired_only=True)
        except (sqlite3.Error, ProviderUnavailable) as error:
            raise self._unavailable("startup") from error

    def start(
        self,
        trace_id: str,
        scope: KnowledgeBaseScope,
        question: str,
        *,
        owner_id: str,
        save_body: bool,
    ) -> None:
        """在模型和快照之前同步写入请求；不可用时不回退内存。

        Args:
            trace_id: 请求的安全标识。
            scope: 已鉴权项目及知识库。
            question: 当前问题正文。
            owner_id: 管理员会话或 Token 主体。
            save_body: 本次是否允许加密保存正文。

        Returns:
            STARTED 落盘完成时无返回值。

        """
        now = datetime.now(UTC)
        body_saved = self.save_body and save_body
        ciphertext, nonce = self._encode(
            trace_id, {"question": question}, enabled=body_saved
        )
        try:
            with self._connections.transaction(write=True) as connection:
                connection.execute(
                    "INSERT INTO query_history (trace_id, project_id, "
                    "knowledge_base_id, owner_id, created_at, expires_at, "
                    "status, question_sha256, body_saved, ciphertext, nonce, "
                    "instance_id, process_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'STARTED', ?, ?, ?, ?, ?, ?)",
                    (
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
    ) -> None:
        """记录实际终态、候选决定和计量；从不持久化模型思考过程。

        Args:
            trace_id: 已建立的请求标识。
            result: 实际查询结果，可为空。
            error: 安全异常，可为空。
            cancelled: 是否发生取消。

        Returns:
            最终状态落盘后无返回值。

        """
        try:
            with self._connections.transaction(write=True) as connection:
                row = connection.execute(
                    "SELECT * FROM query_history WHERE trace_id=?",
                    (trace_id,),
                ).fetchone()
                if row is None:
                    raise self._unavailable(trace_id)
                now = datetime.now(UTC)
                status, metadata = _completion(result, error, cancelled)
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
        except (sqlite3.Error, ProviderUnavailable) as failure:
            raise self._unavailable(trace_id) from failure

    def record(self, event: TraceEvent) -> None:
        """实现当前 TracePort，只写不含正文的结构化事件。

        Args:
            event: 已脱敏的阶段事件。

        Returns:
            事件落盘后无返回值。

        """
        try:
            with self._connections.transaction(write=True) as connection:
                connection.execute(
                    "INSERT INTO query_trace_events (trace_id, occurred_at, "
                    "event_name, payload_json) VALUES (?, ?, ?, ?)",
                    (
                        event.trace_id,
                        event.occurred_at.isoformat(),
                        event.event_name,
                        event.model_dump_json(),
                    ),
                )
        except (sqlite3.Error, ProviderUnavailable) as error:
            raise self._unavailable(event.trace_id) from error

    def events(self, trace_id: str) -> tuple[TraceEvent, ...]:
        """按原顺序读取跨重启仍存在的安全事件。

        Args:
            trace_id: 已授权读取的请求标识。

        Returns:
            按写入顺序排列的事件元组。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM query_trace_events "
                "WHERE trace_id=? ORDER BY sequence",
                (trace_id,),
            ).fetchall()
        return tuple(TraceEvent.model_validate_json(row[0]) for row in rows)

    def diagnostics(self, trace_id: str) -> RetrievalDiagnostics:
        """兼容 SDK 原有诊断读取语义，数据来自权威主库。

        Args:
            trace_id: 待读取的请求标识。

        Returns:
            持久化的安全检索诊断。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM query_history "
                "WHERE trace_id=? AND expires_at>?",
                (trace_id, datetime.now(UTC).isoformat()),
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
        query = (
            "SELECT * FROM query_history WHERE "  # noqa: S608
            + " AND ".join(conditions)
            + " ORDER BY created_at DESC, trace_id DESC"
        )
        with self._connections.transaction() as connection:
            # 加密正文不能交给明文 FTS；游标逐条解密，不保存另一个搜索副本。
            items = []
            total = 0
            for row in connection.execute(query, parameters):
                item = self._view(connection, row, detail=False)
                question = str(item.get("question") or "")
                if keyword and keyword.casefold() not in question.casefold():
                    continue
                if offset <= total < offset + page_size:
                    items.append(item)
                total += 1
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "page_size": page_size,
            "body_enabled": self.save_body,
            "retention_days": self.retention_days,
            "storage": "local_encrypted_sqlite",
        }

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
            row = connection.execute(
                "SELECT * FROM query_history WHERE trace_id=? AND expires_at>?",
                (trace_id, datetime.now(UTC).isoformat()),
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
        item["events"] = [
            event.model_dump(mode="json") for event in self.events(trace_id)
        ]
        return item

    def clear(self, *, expired_only: bool = False) -> int:
        """清理历史和对应事件；不触碰源文件或模型账本。

        Args:
            expired_only: 是否仅清理已到期记录。

        Returns:
            实际清理的历史记录数。

        """
        with self._connections.transaction(write=True) as connection:
            if expired_only:
                now = datetime.now(UTC).isoformat()
                connection.execute(
                    "DELETE FROM query_trace_events WHERE trace_id IN "
                    "(SELECT trace_id FROM query_history WHERE expires_at<=?)",
                    (now,),
                )
                cursor = connection.execute(
                    "DELETE FROM query_history WHERE expires_at<=?", (now,)
                )
            else:
                connection.execute(
                    "DELETE FROM query_trace_events WHERE trace_id IN "
                    "(SELECT trace_id FROM query_history)"
                )
                cursor = connection.execute("DELETE FROM query_history")
        return cursor.rowcount

    def close(self) -> None:
        """仅将本实例剩余请求收尾，不中断共用数据库的其他进程。

        Args:
            无参数；只处理当前实例未终态请求。

        Returns:
            收尾后无返回值。

        """
        try:
            with self._connections.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE query_history SET status='INTERRUPTED', "
                    "finished_at=?, metadata_json=? "
                    "WHERE status='STARTED' AND instance_id=?",
                    (
                        datetime.now(UTC).isoformat(),
                        json.dumps({"reason_code": "PROCESS_INTERRUPTED"}),
                        self._instance_id,
                    ),
                )
        except (sqlite3.Error, ProviderUnavailable):
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


def _sources_readable(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    metadata: dict[str, object],
) -> bool:
    scope = connection.execute(
        "SELECT 1 FROM knowledge_bases k JOIN projects p "
        "ON p.project_id=k.project_id WHERE k.project_id=? "
        "AND k.knowledge_base_id=? AND k.deleted_at IS NULL "
        "AND p.deleted_at IS NULL",
        (row["project_id"], row["knowledge_base_id"]),
    ).fetchone()
    if scope is None:
        return False
    for document_id in cast(list[str], metadata.get("document_ids", [])):
        found = connection.execute(
            "SELECT 1 FROM documents WHERE document_id=? AND project_id=? "
            "AND knowledge_base_id=? AND deleted_at IS NULL "
            "AND lifecycle_status='active'",
            (document_id, row["project_id"], row["knowledge_base_id"]),
        ).fetchone()
        if found is None:
            return False
    return True


def _completion(
    result: SearchAnswerResult | None, error: RagError | None, cancelled: bool
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
        if result.diagnostics is not None:
            metadata["diagnostics"] = result.diagnostics.model_dump(mode="json")
            calls = result.diagnostics.provider_call_details
        if result.generation_mode == "extractive_fallback":
            status = "FAILED"
            metadata["fallback_answer_available"] = bool(result.answer)
            metadata["reason_code"] = result.generation_reason_code
    elif cancelled:
        status = "CANCELLED"
        metadata["reason_code"] = "REQUEST_CANCELLED"
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
