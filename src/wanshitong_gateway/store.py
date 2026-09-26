"""网关身份映射、引用与运营记录的独立 SQLite 存储。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

_CONVERSATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_conversations (
    deployment_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (deployment_id, owner_id, conversation_id),
    UNIQUE (native_session_id)
);
"""
_TURN_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_turns (
    trace_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    asker_name TEXT,
    conversation_id TEXT NOT NULL,
    native_session_id TEXT NOT NULL,
    native_message_id TEXT,
    native_request_id TEXT,
    question TEXT NOT NULL,
    client_context_json TEXT NOT NULL DEFAULT '{}',
    kb_scope_json TEXT NOT NULL DEFAULT '[]',
    answer TEXT,
    references_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    finish_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""
_REFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_references (
    reference_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    native_chunk_id TEXT,
    native_knowledge_id TEXT,
    resource_handle TEXT,
    source_json TEXT NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES gateway_turns(trace_id)
);
"""
_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_feedback (
    trace_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    useful INTEGER NOT NULL,
    reason_code TEXT,
    reason_detail TEXT,
    comment TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES gateway_turns(trace_id)
);
"""
_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_events (
    trace_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    native_request_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trace_id, sequence),
    FOREIGN KEY (trace_id) REFERENCES gateway_turns(trace_id)
);
"""
_ADMIN_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""
_FEEDBACK_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_feedback_reviews (
    trace_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('open', 'in_review', 'resolved')),
    note TEXT NOT NULL DEFAULT '',
    root_cause TEXT NOT NULL DEFAULT '',
    fix_reference TEXT NOT NULL DEFAULT '',
    verification_references_json TEXT NOT NULL DEFAULT '[]',
    evaluation_candidate INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (trace_id) REFERENCES gateway_turns(trace_id)
);
"""
_RECOMMENDATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    question TEXT NOT NULL,
    topic_key TEXT NOT NULL,
    source_knowledge_ids_json TEXT NOT NULL DEFAULT '[]',
    review_note TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK(state IN ('DRAFT', 'APPROVED', 'DISABLED')),
    disabled_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gateway_recommendations_scope
ON gateway_recommendations (deployment_id, state, updated_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GatewayStore:
    """只保存必要映射和真实观察事件，不写原生业务表。"""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not database_path.exists():
            descriptor = os.open(
                database_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
        self._database_path = database_path
        with self._connect() as connection:
            connection.executescript(
                _CONVERSATION_SCHEMA
                + _TURN_SCHEMA
                + _REFERENCE_SCHEMA
                + _FEEDBACK_SCHEMA
                + _EVENT_SCHEMA
                + _ADMIN_AUDIT_SCHEMA
                + _FEEDBACK_REVIEW_SCHEMA
                + _RECOMMENDATION_SCHEMA
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(gateway_turns)"
                )
            }
            if "client_context_json" not in columns:
                connection.execute(
                    "ALTER TABLE gateway_turns ADD COLUMN "
                    "client_context_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "kb_scope_json" not in columns:
                connection.execute(
                    "ALTER TABLE gateway_turns ADD COLUMN "
                    "kb_scope_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "asker_name" not in columns:
                connection.execute(
                    "ALTER TABLE gateway_turns ADD COLUMN asker_name TEXT"
                )
            review_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(gateway_feedback_reviews)"
                )
            }
            review_additions = {
                "root_cause": "TEXT NOT NULL DEFAULT ''",
                "fix_reference": "TEXT NOT NULL DEFAULT ''",
                "verification_references_json": "TEXT NOT NULL DEFAULT '[]'",
                "evaluation_candidate": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in review_additions.items():
                if name not in review_columns:
                    connection.execute(
                        f"ALTER TABLE gateway_feedback_reviews ADD COLUMN "
                        f"{name} {definition}"
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def native_session(
        self, *, deployment_id: str, owner_id: str, conversation_id: str
    ) -> str | None:
        """只返回当前部署和用户拥有的原生会话。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT native_session_id FROM gateway_conversations "
                "WHERE deployment_id=? AND owner_id=? AND conversation_id=?",
                (deployment_id, owner_id, conversation_id),
            ).fetchone()
        return str(row["native_session_id"]) if row else None

    def bind_session(
        self,
        *,
        deployment_id: str,
        owner_id: str,
        conversation_id: str,
        native_session_id: str,
    ) -> str:
        """唯一绑定外部会话；并发竞争时返回已存在的权威映射。"""
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO gateway_conversations "
                "(deployment_id,owner_id,conversation_id,native_session_id,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (
                    deployment_id,
                    owner_id,
                    conversation_id,
                    native_session_id,
                    now,
                    now,
                ),
            )
        bound = self.native_session(
            deployment_id=deployment_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
        )
        if bound is None:
            raise RuntimeError("无法保存原生会话映射。")
        return bound

    def list_conversations(
        self, *, deployment_id: str, owner_id: str
    ) -> list[dict[str, str]]:
        """列出当前用户自己的新引擎会话。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT conversation_id,native_session_id,updated_at "
                "FROM gateway_conversations WHERE deployment_id=? "
                "AND owner_id=? ORDER BY updated_at DESC",
                (deployment_id, owner_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def start_turn(  # noqa: PLR0913 - 持久化边界显式接收全部归属键。
        self,
        *,
        trace_id: str,
        deployment_id: str,
        owner_id: str,
        conversation_id: str,
        native_session_id: str,
        question: str,
        asker_name: str | None = None,
        client_context: Mapping[str, Any] | None = None,
        kb_scope: tuple[str, ...] = (),
    ) -> None:
        """记录一次已实际发起的原生生成。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO gateway_turns "
                "(trace_id,deployment_id,owner_id,asker_name,conversation_id,"
                "native_session_id,question,client_context_json,"
                "kb_scope_json,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trace_id,
                    deployment_id,
                    owner_id,
                    asker_name,
                    conversation_id,
                    native_session_id,
                    question,
                    json.dumps(client_context or {}, ensure_ascii=False),
                    json.dumps(kb_scope, ensure_ascii=False),
                    "streaming",
                    _now(),
                ),
            )
            connection.execute(
                "UPDATE gateway_conversations SET updated_at=? "
                "WHERE deployment_id=? AND owner_id=? AND conversation_id=?",
                (_now(), deployment_id, owner_id, conversation_id),
            )

    def record_event(
        self,
        *,
        trace_id: str,
        sequence: int,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        """保存网关实际见到的事件，不伪称原生内部 Trace。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO gateway_events "
                "(trace_id,sequence,event_type,native_request_id,"
                "payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (
                    trace_id,
                    sequence,
                    event_type,
                    payload.get("native_request_id"),
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )

    def set_native_message(
        self,
        *,
        trace_id: str,
        native_message_id: str,
        native_request_id: str | None,
    ) -> None:
        """在流开始时持久化停止所需 ID。"""
        with self._connect() as connection:
            connection.execute(
                "UPDATE gateway_turns SET native_message_id=?,"
                "native_request_id=? WHERE trace_id=?",
                (native_message_id, native_request_id, trace_id),
            )

    def request_stop(self, *, trace_id: str, owner_id: str) -> None:
        """记录已发往原生停止接口的用户动作。"""
        with self._connect() as connection:
            connection.execute(
                "UPDATE gateway_turns SET status='stop_requested' "
                "WHERE trace_id=? AND owner_id=? AND status='streaming'",
                (trace_id, owner_id),
            )

    def add_reference(
        self,
        *,
        reference_id: str,
        trace_id: str,
        source: Mapping[str, Any],
        resource_handle: str | None,
    ) -> None:
        """保存引用与原生 chunk/knowledge 的可验证对应。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO gateway_references "
                "(reference_id,trace_id,native_chunk_id,native_knowledge_id,"
                "resource_handle,source_json) VALUES (?,?,?,?,?,?)",
                (
                    reference_id,
                    trace_id,
                    source.get("id"),
                    source.get("knowledge_id"),
                    resource_handle,
                    json.dumps(source, ensure_ascii=False),
                ),
            )

    def finish_turn(  # noqa: PLR0913 - 终态字段逐项保存便于审计。
        self,
        *,
        trace_id: str,
        status: str,
        answer: str,
        references: tuple[Mapping[str, Any], ...],
        native_message_id: str | None,
        native_request_id: str | None,
        truncated: bool,
        finish_reason: str | None,
    ) -> None:
        """只按真实终态封存本轮回答和原生 ID。"""
        with self._connect() as connection:
            connection.execute(
                "UPDATE gateway_turns SET status=?,answer=?,"
                "references_json=?,native_message_id=?,native_request_id=?,"
                "truncated=?,finish_reason=?,completed_at=? WHERE trace_id=?",
                (
                    status,
                    answer,
                    json.dumps(references, ensure_ascii=False),
                    native_message_id,
                    native_request_id,
                    int(truncated),
                    finish_reason,
                    _now(),
                    trace_id,
                ),
            )

    def get_turn(
        self, *, trace_id: str, owner_id: str | None = None
    ) -> dict[str, Any] | None:
        """按真实用户归属读取单轮；管理员可显式省略 owner。"""
        query = "SELECT * FROM gateway_turns WHERE trace_id=?"
        arguments: tuple[str, ...] = (trace_id,)
        if owner_id is not None:
            query += " AND owner_id=?"
            arguments = (trace_id, owner_id)
        with self._connect() as connection:
            row = connection.execute(query, arguments).fetchone()
        return dict(row) if row else None

    def list_turns(
        self,
        *,
        deployment_id: str,
        owner_id: str,
        conversation_id: str,
    ) -> list[dict[str, Any]]:
        """按会话返回同一用户的新引擎历史映射。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_turns WHERE deployment_id=? "
                "AND owner_id=? AND conversation_id=? ORDER BY created_at",
                (deployment_id, owner_id, conversation_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_reference(
        self, *, reference_id: str, trace_id: str
    ) -> dict[str, Any] | None:
        """引用必须先匹配已授权的本地 Trace。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_references "
                "WHERE reference_id=? AND trace_id=?",
                (reference_id, trace_id),
            ).fetchone()
        return dict(row) if row else None

    def save_feedback(  # noqa: PLR0913 - 反馈字段保持产品协议原样。
        self,
        *,
        trace_id: str,
        owner_id: str,
        useful: bool,
        reason_code: str | None,
        reason_detail: str | None,
        comment: str | None,
    ) -> None:
        """关联真实原生消息保存湾事通产品反馈。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO gateway_feedback "
                "(trace_id,owner_id,useful,reason_code,reason_detail,"
                "comment,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    trace_id,
                    owner_id,
                    int(useful),
                    reason_code,
                    reason_detail,
                    comment,
                    _now(),
                ),
            )

    def record_admin_action(
        self,
        *,
        actor: str,
        method: str,
        path: str,
        status_code: int,
    ) -> None:
        """只记管理员审计标识和操作结果，不记录内容及凭据。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO gateway_admin_audit "
                "(actor,method,path,status_code,created_at) "
                "VALUES (?,?,?,?,?)",
                (actor, method, path, status_code, _now()),
            )

    def list_traces(
        self,
        *,
        deployment_id: str,
        limit: int = 100,
        offset: int = 0,
        query: str = "",
        feedback_only: bool = False,
    ) -> list[dict[str, Any]]:
        """管理员列表返回本候选的提问者、问题和原生 ID。"""
        arguments = (
            deployment_id,
            query,
            query,
            query,
            f"rdms:{deployment_id}:{query}",
            int(feedback_only),
            limit,
            offset,
        )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.trace_id,t.created_at,t.completed_at,t.status,"
                "t.owner_id,t.asker_name,t.question,"
                "t.native_session_id,t.native_message_id,t.native_request_id,"
                "t.truncated,t.finish_reason,"
                "LENGTH(t.question) AS question_chars,"
                "LENGTH(t.answer) AS answer_chars,"
                "f.useful AS feedback_useful,"
                "r.status AS review_status "
                "FROM gateway_turns AS t LEFT JOIN gateway_feedback AS f "
                "ON f.trace_id=t.trace_id "
                "LEFT JOIN gateway_feedback_reviews AS r "
                "ON r.trace_id=t.trace_id WHERE t.deployment_id=? "
                "AND (?='' OR instr(t.question,?)>0 OR "
                "instr(COALESCE(t.asker_name,''),?)>0 OR t.owner_id=?) "
                "AND (?=0 OR f.trace_id IS NOT NULL) "
                "ORDER BY t.created_at DESC LIMIT ? OFFSET ?",
                arguments,
            ).fetchall()
        return [
            {
                **dict(row),
                "asker_id": str(row["owner_id"]).rsplit(":", maxsplit=1)[-1],
            }
            for row in rows
        ]

    def count_traces(
        self,
        *,
        deployment_id: str,
        query: str = "",
        feedback_only: bool = False,
    ) -> int:
        """给管理页分页提供与列表相同的筛选总数。"""
        arguments = (
            deployment_id,
            query,
            query,
            query,
            f"rdms:{deployment_id}:{query}",
            int(feedback_only),
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM gateway_turns AS t "
                "LEFT JOIN gateway_feedback AS f ON f.trace_id=t.trace_id "
                "WHERE t.deployment_id=? "
                "AND (?='' OR instr(t.question,?)>0 OR "
                "instr(COALESCE(t.asker_name,''),?)>0 OR t.owner_id=?) "
                "AND (?=0 OR f.trace_id IS NOT NULL)",
                arguments,
            ).fetchone()
        return int(row["total"])

    def ops_summary(self, *, deployment_id: str) -> dict[str, int]:
        """统计当前候选部署内的新引擎记录与反馈。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS turns,COUNT(DISTINCT t.owner_id) AS users,"
                "SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) "
                "AS completed,"
                "SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) "
                "AS failed,"
                "SUM(CASE WHEN f.useful=0 THEN 1 ELSE 0 END) "
                "AS negative_feedback,"
                "SUM(CASE WHEN f.trace_id IS NOT NULL THEN 1 ELSE 0 END) "
                "AS feedback_count,"
                "SUM(CASE WHEN f.useful=1 THEN 1 ELSE 0 END) "
                "AS helpful_feedback,"
                "SUM(CASE WHEN f.trace_id IS NOT NULL AND "
                "COALESCE(r.status,'open')!='resolved' THEN 1 ELSE 0 END) "
                "AS pending_feedback "
                "FROM gateway_turns AS t LEFT JOIN gateway_feedback AS f "
                "ON f.trace_id=t.trace_id "
                "LEFT JOIN gateway_feedback_reviews AS r "
                "ON r.trace_id=t.trace_id WHERE t.deployment_id=?",
                (deployment_id,),
            ).fetchone()
        return {
            key: int(row[key] or 0)
            for key in (
                "turns",
                "users",
                "completed",
                "failed",
                "negative_feedback",
                "feedback_count",
                "helpful_feedback",
                "pending_feedback",
            )
        }

    def frequent_questions(
        self, *, deployment_id: str, days: int = 7, limit: int = 20
    ) -> list[dict[str, Any]]:
        """按原问题精确归组，不把不同业务表述擅自合并。"""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT TRIM(t.question) AS question,COUNT(*) AS count,"
                "COUNT(DISTINCT t.owner_id) AS user_count,"
                "SUM(CASE WHEN f.useful=0 THEN 1 ELSE 0 END) "
                "AS negative_feedback,MAX(t.created_at) AS last_asked_at "
                "FROM gateway_turns AS t LEFT JOIN gateway_feedback AS f "
                "ON f.trace_id=t.trace_id "
                "WHERE t.deployment_id=? AND t.created_at>=? "
                "GROUP BY TRIM(t.question) "
                "ORDER BY count DESC,last_asked_at DESC LIMIT ?",
                (deployment_id, since, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recommendations(
        self, *, deployment_id: str, approved_only: bool = False
    ) -> list[dict[str, Any]]:
        """运营题目独立于旧题库；公开端只读取人工审核通过的题面。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT recommendation_id,question,topic_key,"
                "source_knowledge_ids_json,review_note,state,disabled_reason,"
                "version,created_at,updated_at FROM gateway_recommendations "
                "WHERE deployment_id=? AND (?=0 OR state='APPROVED') "
                "ORDER BY updated_at DESC LIMIT 200",
                (deployment_id, int(approved_only)),
            ).fetchall()
        return [
            {
                **dict(row),
                "source_knowledge_ids": json.loads(
                    row["source_knowledge_ids_json"]
                ),
            }
            for row in rows
        ]

    def save_recommendation(  # noqa: PLR0913 - 草稿版本与审核字段逐项入库。
        self,
        *,
        deployment_id: str,
        recommendation_id: str | None,
        expected_version: int | None,
        question: str,
        topic_key: str,
        source_knowledge_ids: list[str],
        review_note: str,
        state: str,
        disabled_reason: str | None,
    ) -> dict[str, Any]:
        """草稿、发布和下架在网关留版本；不改原生知识或答案。"""
        if state not in {"DRAFT", "APPROVED", "DISABLED"}:
            raise ValueError("invalid recommendation state")
        now = _now()
        identifier = recommendation_id or f"pq_{uuid4().hex}"
        with self._connect() as connection:
            if recommendation_id is None:
                connection.execute(
                    "INSERT INTO gateway_recommendations "
                    "(recommendation_id,deployment_id,question,topic_key,"
                    "source_knowledge_ids_json,review_note,state,"
                    "disabled_reason,version,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        identifier,
                        deployment_id,
                        question,
                        topic_key,
                        json.dumps(source_knowledge_ids, ensure_ascii=False),
                        review_note,
                        state,
                        disabled_reason,
                        now,
                        now,
                    ),
                )
            else:
                cursor = connection.execute(
                    "UPDATE gateway_recommendations SET question=?,topic_key=?,"
                    "source_knowledge_ids_json=?,review_note=?,state=?,"
                    "disabled_reason=?,version=version+1,updated_at=? "
                    "WHERE recommendation_id=? AND deployment_id=? "
                    "AND version=?",
                    (
                        question,
                        topic_key,
                        json.dumps(source_knowledge_ids, ensure_ascii=False),
                        review_note,
                        state,
                        disabled_reason,
                        now,
                        identifier,
                        deployment_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("recommendation version conflict")
            row = connection.execute(
                "SELECT recommendation_id,question,topic_key,"
                "source_knowledge_ids_json,review_note,state,disabled_reason,"
                "version,created_at,updated_at FROM gateway_recommendations "
                "WHERE recommendation_id=? AND deployment_id=?",
                (identifier, deployment_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("recommendation not saved")
        return {
            **dict(row),
            "source_knowledge_ids": json.loads(
                row["source_knowledge_ids_json"]
            ),
        }

    def trace_overview(
        self, *, trace_id: str, deployment_id: str
    ) -> dict[str, Any] | None:
        """管理员在页面查看原问答，技术事件仍只返回时序。"""
        record = self.trace_export(trace_id=trace_id, include_content=False)
        turn = self.get_turn(trace_id=trace_id)
        if (
            record is None
            or turn is None
            or turn["deployment_id"] != deployment_id
        ):
            return None
        with self._connect() as connection:
            review = connection.execute(
                "SELECT status,note,root_cause,fix_reference,"
                "verification_references_json,evaluation_candidate,updated_at "
                "FROM gateway_feedback_reviews "
                "WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
        return {
            **record,
            "asker_id": str(turn["owner_id"]).rsplit(":", maxsplit=1)[-1],
            "asker_name": turn["asker_name"],
            "question": turn["question"],
            "answer": turn["answer"],
            "review": (
                {
                    **dict(review),
                    "verification_references": json.loads(
                        review["verification_references_json"]
                    ),
                    "evaluation_candidate": bool(
                        review["evaluation_candidate"]
                    ),
                }
                if review
                else None
            ),
        }

    def save_feedback_review(  # noqa: PLR0913 - 保留运营复核字段。
        self,
        *,
        trace_id: str,
        status: str,
        note: str,
        root_cause: str = "",
        fix_reference: str = "",
        verification_references: list[str] | None = None,
        evaluation_candidate: bool = False,
    ) -> None:
        """记录人工复核状态；不改变原生回答或模型配置。"""
        if status not in {"open", "in_review", "resolved"}:
            raise ValueError("invalid review status")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO gateway_feedback_reviews "
                "(trace_id,status,note,root_cause,fix_reference,"
                "verification_references_json,evaluation_candidate,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(trace_id) DO UPDATE SET "
                "status=excluded.status,note=excluded.note,"
                "root_cause=excluded.root_cause,"
                "fix_reference=excluded.fix_reference,"
                "verification_references_json=excluded.verification_references_json,"
                "evaluation_candidate=excluded.evaluation_candidate,"
                "updated_at=excluded.updated_at",
                (
                    trace_id,
                    status,
                    note,
                    root_cause,
                    fix_reference,
                    json.dumps(
                        verification_references or [], ensure_ascii=False
                    ),
                    int(evaluation_candidate),
                    _now(),
                ),
            )

    def trace_export(
        self, *, trace_id: str, include_content: bool
    ) -> dict[str, Any] | None:
        """导出网关真实可见事件；默认去除问题、答案和原生事件正文。"""
        turn = self.get_turn(trace_id=trace_id)
        if turn is None:
            return None
        with self._connect() as connection:
            events = connection.execute(
                "SELECT sequence,event_type,native_request_id,payload_json,"
                "created_at FROM gateway_events WHERE trace_id=? "
                "ORDER BY sequence",
                (trace_id,),
            ).fetchall()
            references = connection.execute(
                "SELECT reference_id,native_chunk_id,native_knowledge_id,"
                "resource_handle,source_json FROM gateway_references "
                "WHERE trace_id=? ORDER BY rowid",
                (trace_id,),
            ).fetchall()
            feedback = connection.execute(
                "SELECT useful,reason_code,reason_detail,comment,created_at "
                "FROM gateway_feedback WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
        result = {
            "engine": "weknora",
            "bridge_trace_id": trace_id,
            "native_trace": "NOT_CONFIGURED",
            "deployment_id": turn["deployment_id"],
            "conversation_id": turn["conversation_id"],
            "native_session_id": turn["native_session_id"],
            "native_message_id": turn["native_message_id"],
            "native_request_id": turn["native_request_id"],
            "status": turn["status"],
            "truncated": bool(turn["truncated"]),
            "finish_reason": turn["finish_reason"],
            "created_at": turn["created_at"],
            "completed_at": turn["completed_at"],
            "client_context": json.loads(turn["client_context_json"]),
            "kb_scope": json.loads(turn["kb_scope_json"]),
            "question": turn["question"] if include_content else None,
            "answer": turn["answer"] if include_content else None,
            "references": [
                {
                    "reference_id": row["reference_id"],
                    "native_chunk_id": row["native_chunk_id"],
                    "native_knowledge_id": row["native_knowledge_id"],
                    "resource_available": row["resource_handle"] is not None,
                    "native_reference": json.loads(row["source_json"])
                    if include_content
                    else None,
                }
                for row in references
            ],
            "events": [
                {
                    "sequence": row["sequence"],
                    "event_type": row["event_type"],
                    "native_request_id": row["native_request_id"],
                    "created_at": row["created_at"],
                    "payload": json.loads(row["payload_json"])
                    if include_content
                    else None,
                }
                for row in events
            ],
            "feedback": (
                {
                    "useful": bool(feedback["useful"]),
                    "reason_code": feedback["reason_code"],
                    "reason_detail": feedback["reason_detail"],
                    "comment": feedback["comment"] if include_content else None,
                    "created_at": feedback["created_at"],
                }
                if feedback is not None
                else None
            ),
            "content_redacted": not include_content,
        }
        return result
