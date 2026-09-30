"""验证 90 天滚动清理不会删除新记录或留下失败后的孤儿映射。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from wanshitong_gateway.history_retention import prune_history
from wanshitong_gateway.store import GatewayStore

_DEPLOYMENT = "pilot"
_OWNER = "rdms:pilot:user-a"


class FakeNative:
    """只记录原生删除动作，模拟可见消息。"""

    def __init__(self) -> None:
        self.messages_by_session: dict[str, list[dict[str, Any]]] = {}
        self.deleted_sessions: list[str] = []
        self.deleted_messages: list[str] = []
        self.fail_session_delete = False

    async def messages(
        self, *, user_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        assert user_id == "user-a"
        return self.messages_by_session[session_id]

    async def delete_message(
        self, *, user_id: str, session_id: str, message_id: str
    ) -> None:
        assert user_id == "user-a"
        self.deleted_messages.append(message_id)

    async def delete_session(self, *, user_id: str, session_id: str) -> None:
        assert user_id == "user-a"
        if self.fail_session_delete:
            raise RuntimeError("原生服务不可用")
        self.deleted_sessions.append(session_id)


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _add_turn(
    store: GatewayStore,
    path: Path,
    *,
    conversation_id: str,
    session_id: str,
    created_at: str,
    feedback_at: str | None = None,
) -> tuple[str, str, str]:
    trace_id = "trace_" + uuid4().hex
    message_id = str(uuid4())
    request_id = str(uuid4())
    store.bind_session(
        deployment_id=_DEPLOYMENT,
        owner_id=_OWNER,
        conversation_id=conversation_id,
        native_session_id=session_id,
    )
    store.start_turn(
        trace_id=trace_id,
        deployment_id=_DEPLOYMENT,
        owner_id=_OWNER,
        conversation_id=conversation_id,
        native_session_id=session_id,
        question="测试问题",
    )
    store.finish_turn(
        trace_id=trace_id,
        status="completed",
        answer="测试回答",
        references=(),
        native_message_id=message_id,
        native_request_id=request_id,
        truncated=False,
        finish_reason=None,
    )
    if feedback_at is not None:
        store.save_feedback(
            trace_id=trace_id,
            owner_id=_OWNER,
            useful=False,
            reason_code=None,
            reason_detail=None,
            comment=None,
        )
    with _connection(path) as connection:
        connection.execute(
            "UPDATE gateway_turns SET created_at=? WHERE trace_id=?",
            (created_at, trace_id),
        )
        if feedback_at is not None:
            connection.execute(
                "UPDATE gateway_feedback SET created_at=? WHERE trace_id=?",
                (feedback_at, trace_id),
            )
    return trace_id, message_id, request_id


def test_daily_run_only_removes_expired_records(tmp_path: Path) -> None:
    """整段过期会话、活跃会话旧轮次和旧反馈各自滚动清理。"""
    path = tmp_path / "gateway.sqlite3"
    store = GatewayStore(path)
    cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    fresh = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    old_session = str(uuid4())
    active_session = str(uuid4())
    _add_turn(
        store, path, conversation_id="old", session_id=old_session,
        created_at=old, feedback_at=old,
    )
    old_trace, old_message, old_request = _add_turn(
        store, path, conversation_id="active", session_id=active_session,
        created_at=old,
    )
    fresh_trace, _, _ = _add_turn(
        store, path, conversation_id="active", session_id=active_session,
        created_at=fresh, feedback_at=old,
    )
    with _connection(path) as connection:
        connection.execute(
            "UPDATE gateway_conversations SET updated_at=? "
            "WHERE conversation_id='old'",
            (old,),
        )
    user_message = str(uuid4())
    native = FakeNative()
    native.messages_by_session[active_session] = [
        {"id": user_message, "request_id": old_request, "role": "user"},
        {"id": old_message, "request_id": old_request, "role": "assistant"},
    ]

    with _connection(path) as connection:
        preview = asyncio.run(prune_history(
            connection, None, deployment_id=_DEPLOYMENT,
            cutoff=cutoff, execute=False,
        ))
        assert preview == {"sessions": 1, "turns": 1, "feedback": 2}
        result = asyncio.run(prune_history(
            connection, native, deployment_id=_DEPLOYMENT,
            cutoff=cutoff, execute=True,
        ))
        assert result == {"sessions": 1, "turns": 1, "feedback": 1}
        assert connection.execute(
            "SELECT COUNT(*) FROM gateway_conversations"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM gateway_turns"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM gateway_feedback"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT trace_id FROM gateway_turns"
        ).fetchone()[0] == fresh_trace
        assert connection.execute(
            "SELECT COUNT(*) FROM gateway_turns WHERE trace_id=?",
            (old_trace,),
        ).fetchone()[0] == 0
    assert native.deleted_sessions == [old_session]
    assert native.deleted_messages == [user_message, old_message]


def test_recent_review_defers_expired_history(tmp_path: Path) -> None:
    """管理员近期处理的反馈保留到其自身满 90 天。"""
    path = tmp_path / "gateway.sqlite3"
    store = GatewayStore(path)
    cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    fresh = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    session_id = str(uuid4())
    trace_id, _, _ = _add_turn(
        store, path, conversation_id="reviewed", session_id=session_id,
        created_at=old, feedback_at=old,
    )
    other_trace, other_message, other_request = _add_turn(
        store, path, conversation_id="reviewed", session_id=session_id,
        created_at=old,
    )
    with _connection(path) as connection:
        connection.execute(
            "UPDATE gateway_conversations SET updated_at=?",
            (old,),
        )
        connection.execute(
            "INSERT INTO gateway_feedback_reviews "
            "(trace_id,status,updated_at) VALUES (?,?,?)",
            (trace_id, "in_review", fresh),
        )
        connection.commit()
        native = FakeNative()
        user_message = str(uuid4())
        native.messages_by_session[session_id] = [
            {"id": user_message, "request_id": other_request, "role": "user"},
            {"id": other_message, "request_id": other_request,
             "role": "assistant"},
        ]
        result = asyncio.run(prune_history(
            connection, native, deployment_id=_DEPLOYMENT,
            cutoff=cutoff, execute=True,
        ))
        assert result == {"sessions": 0, "turns": 1, "feedback": 0}
        assert connection.execute(
            "SELECT COUNT(*) FROM gateway_turns"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT trace_id FROM gateway_turns"
        ).fetchone()[0] == trace_id
        assert connection.execute(
            "SELECT COUNT(*) FROM gateway_turns WHERE trace_id=?",
            (other_trace,),
        ).fetchone()[0] == 0
        assert native.deleted_sessions == []
        assert native.deleted_messages == [user_message, other_message]


def test_upstream_failure_keeps_local_history(tmp_path: Path) -> None:
    """原生删除失败时保留全部本地映射，供次日重试。"""
    path = tmp_path / "gateway.sqlite3"
    store = GatewayStore(path)
    cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    _add_turn(
        store, path, conversation_id="retry", session_id=str(uuid4()),
        created_at=old, feedback_at=old,
    )
    with _connection(path) as connection:
        connection.execute(
            "UPDATE gateway_conversations SET updated_at=?", (old,)
        )
        connection.commit()
        native = FakeNative()
        native.fail_session_delete = True
        with pytest.raises(RuntimeError, match="原生服务不可用"):
            asyncio.run(prune_history(
                connection, native, deployment_id=_DEPLOYMENT,
                cutoff=cutoff, execute=True,
            ))
        for table in (
            "gateway_conversations", "gateway_turns", "gateway_feedback"
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 1


def test_cutoff_is_strictly_older_than_ninety_days(tmp_path: Path) -> None:
    """刚好落在边界的记录当天仍保留。"""
    path = tmp_path / "gateway.sqlite3"
    store = GatewayStore(path)
    cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    session_id = str(uuid4())
    _add_turn(
        store, path, conversation_id="boundary", session_id=session_id,
        created_at=cutoff, feedback_at=cutoff,
    )
    with _connection(path) as connection:
        connection.execute(
            "UPDATE gateway_conversations SET updated_at=?", (cutoff,)
        )
        assert asyncio.run(prune_history(
            connection, None, deployment_id=_DEPLOYMENT,
            cutoff=cutoff, execute=False,
        )) == {"sessions": 0, "turns": 0, "feedback": 0}
