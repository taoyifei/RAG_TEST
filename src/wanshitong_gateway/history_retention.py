"""按滚动保留期清理湾事通问答与反馈，不触碰知识库资料。"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import httpx

from wanshitong_gateway.engine_settings import EngineSettings, load_server_secret
from wanshitong_gateway.settings import GatewayAuthSettings
from wanshitong_gateway.weknora.client import WeKnoraClient
from wanshitong_gateway.weknora.principal import ExternalPrincipalSigner

RETENTION_DAYS = 90
_MAX_SESSIONS_PER_RUN = 100
_MAX_TURNS_PER_RUN = 500
_MAX_FEEDBACK_PER_RUN = 500
_NATIVE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
_TURN_CHILD_TABLES = (
    "gateway_feedback_reviews",
    "gateway_feedback",
    "gateway_references",
    "gateway_events",
)


class NativeHistory(Protocol):
    """维护任务需要的原生历史接口。"""

    async def messages(
        self, *, user_id: str, session_id: str
    ) -> list[dict[str, Any]]: ...

    async def delete_message(
        self, *, user_id: str, session_id: str, message_id: str
    ) -> None: ...

    async def delete_session(self, *, user_id: str, session_id: str) -> None: ...


class NativeHistoryClient:
    """使用现有公共 Key 和外部主体身份删除该用户自己的原生历史。"""

    def __init__(
        self, *, base_url: str, api_key: str, signer: ExternalPrincipalSigner
    ) -> None:
        self._api_key = api_key
        self._signer = signer
        self._reader = WeKnoraClient(
            base_url=base_url, api_key=api_key, signer=signer
        )
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        """关闭维护任务的 HTTP 连接池。"""
        await self._reader.close()
        await self._http.aclose()

    async def messages(
        self, *, user_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        """读取完整原生会话，用 request_id 对齐一问一答。"""
        return await self._reader.messages(
            user_id=user_id, session_id=session_id
        )

    async def delete_message(
        self, *, user_id: str, session_id: str, message_id: str
    ) -> None:
        """通过原生接口软删除单条消息及其历史索引。"""
        await self._delete(
            f"/api/v1/messages/{_validated_id(session_id)}/"
            f"{_validated_id(message_id)}",
            user_id=user_id,
        )

    async def delete_session(self, *, user_id: str, session_id: str) -> None:
        """通过原生接口软删除已完全过期的会话。"""
        await self._delete(
            f"/api/v1/sessions/{_validated_id(session_id)}",
            user_id=user_id,
        )

    async def _delete(self, path: str, *, user_id: str) -> None:
        response = await self._http.delete(
            path,
            headers={
                "Accept": "application/json",
                "X-API-Key": self._api_key,
                "X-External-User-Token": self._signer.sign(user_id),
            },
        )
        if response.status_code == 404:
            # 前次执行可能已删原生记录，却在提交网关 SQLite 前中断。
            return
        if response.status_code != 200:
            raise RuntimeError(f"原生历史清理失败，HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError("原生历史清理返回非 JSON") from error
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise RuntimeError("原生历史清理未确认成功")


def _validated_id(value: str) -> str:
    if _NATIVE_ID.fullmatch(value) is None:
        raise ValueError("原生历史 ID 格式无效")
    try:
        UUID(value)
    except ValueError as error:
        raise ValueError("原生历史 ID 格式无效") from error
    return value


def _user_id(owner_id: str, deployment_id: str) -> str:
    prefix = f"rdms:{deployment_id}:"
    if not owner_id.startswith(prefix) or not owner_id[len(prefix) :]:
        raise ValueError("历史记录的部署主体不匹配")
    return owner_id[len(prefix) :]


def _connect(path: Path, *, execute: bool) -> sqlite3.Connection:
    mode = "rw" if execute else "ro"
    connection = sqlite3.connect(
        f"file:{path}?mode={mode}", uri=True, timeout=10.0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _recent_feedback_clause(alias: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM gateway_feedback AS f "
        "LEFT JOIN gateway_feedback_reviews AS r ON r.trace_id=f.trace_id "
        f"WHERE f.trace_id={alias}.trace_id "
        "AND (f.created_at>=? OR r.updated_at>=?)) "
        "OR EXISTS (SELECT 1 FROM gateway_feedback_reviews AS r "
        f"WHERE r.trace_id={alias}.trace_id AND r.updated_at>=?)"
    )


def _old_sessions(
    connection: sqlite3.Connection, deployment_id: str, cutoff: str
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT c.* FROM gateway_conversations AS c "
        "WHERE c.deployment_id=? AND c.updated_at<? AND NOT EXISTS ("
        "SELECT 1 FROM gateway_turns AS t WHERE t.deployment_id=c.deployment_id "
        "AND t.owner_id=c.owner_id AND t.conversation_id=c.conversation_id "
        "AND (t.created_at>=? OR "
        + _recent_feedback_clause("t")
        + ")) ORDER BY c.updated_at LIMIT ?",
        (deployment_id, cutoff, cutoff, cutoff, cutoff, cutoff,
         _MAX_SESSIONS_PER_RUN),
    ).fetchall()


def _old_turns(
    connection: sqlite3.Connection, deployment_id: str, cutoff: str
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT t.* FROM gateway_turns AS t JOIN gateway_conversations AS c "
        "ON c.deployment_id=t.deployment_id AND c.owner_id=t.owner_id "
        "AND c.conversation_id=t.conversation_id "
        "WHERE t.deployment_id=? AND t.created_at<? "
        "AND t.status NOT IN ('streaming','stop_requested') "
        "AND NOT (" + _recent_feedback_clause("t") + ") "
        "ORDER BY t.created_at LIMIT ?",
        (deployment_id, cutoff, cutoff, cutoff, cutoff,
         _MAX_TURNS_PER_RUN),
    ).fetchall()


def _old_feedback(
    connection: sqlite3.Connection, deployment_id: str, cutoff: str
) -> list[str]:
    rows = connection.execute(
        "SELECT f.trace_id FROM gateway_feedback AS f "
        "JOIN gateway_turns AS t ON t.trace_id=f.trace_id "
        "LEFT JOIN gateway_feedback_reviews AS r ON r.trace_id=f.trace_id "
        "WHERE t.deployment_id=? AND f.created_at<? "
        "AND (r.updated_at IS NULL OR r.updated_at<?) "
        "ORDER BY f.created_at LIMIT ?",
        (deployment_id, cutoff, cutoff, _MAX_FEEDBACK_PER_RUN),
    ).fetchall()
    return [str(row["trace_id"]) for row in rows]


def _delete_turn_children(
    connection: sqlite3.Connection, where: str, parameters: tuple[str, ...]
) -> None:
    for table in _TURN_CHILD_TABLES:
        connection.execute(
            f"DELETE FROM {table} WHERE trace_id IN "
            f"(SELECT trace_id FROM gateway_turns WHERE {where})",
            parameters,
        )


async def prune_history(
    connection: sqlite3.Connection,
    native: NativeHistory | None,
    *,
    deployment_id: str,
    cutoff: str,
    execute: bool,
) -> dict[str, int]:
    """每日滚动删除过期记录；上游失败时保留网关映射供次日重试。"""
    sessions = _old_sessions(connection, deployment_id, cutoff)
    expiring_sessions = {
        (row["deployment_id"], row["owner_id"], row["conversation_id"])
        for row in sessions
    }
    turns = [
        row for row in _old_turns(connection, deployment_id, cutoff)
        if (row["deployment_id"], row["owner_id"], row["conversation_id"])
        not in expiring_sessions
    ]
    feedback = _old_feedback(connection, deployment_id, cutoff)
    eligible = {
        "sessions": len(sessions),
        "turns": len(turns),
        "feedback": len(feedback),
    }
    if not execute:
        return eligible
    if native is None:
        raise ValueError("执行清理必须提供原生客户端")
    counts = {"sessions": 0, "turns": 0, "feedback": 0}

    for session in sessions:
        scope = (
            str(session["deployment_id"]),
            str(session["owner_id"]),
            str(session["conversation_id"]),
        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute(
                "SELECT c.updated_at FROM gateway_conversations AS c "
                "WHERE c.deployment_id=? AND c.owner_id=? "
                "AND c.conversation_id=? AND c.updated_at=? "
                "AND c.updated_at<? AND NOT EXISTS ("
                "SELECT 1 FROM gateway_turns AS t WHERE "
                "t.deployment_id=c.deployment_id AND t.owner_id=c.owner_id "
                "AND t.conversation_id=c.conversation_id "
                "AND (t.created_at>=? OR "
                + _recent_feedback_clause("t")
                + "))",
                (*scope, str(session["updated_at"]), cutoff, cutoff,
                 cutoff, cutoff, cutoff),
            ).fetchone()
            if current is None:
                connection.rollback()
                continue
            await native.delete_session(
                user_id=_user_id(scope[1], deployment_id),
                session_id=str(session["native_session_id"]),
            )
            where = "deployment_id=? AND owner_id=? AND conversation_id=?"
            _delete_turn_children(connection, where, scope)
            connection.execute(f"DELETE FROM gateway_turns WHERE {where}", scope)
            connection.execute(
                f"DELETE FROM gateway_conversations WHERE {where}", scope
            )
            connection.commit()
            counts["sessions"] += 1
        except BaseException:
            connection.rollback()
            raise

    for turn in turns:
        trace_id = str(turn["trace_id"])
        user_id = _user_id(str(turn["owner_id"]), deployment_id)
        session_id = str(turn["native_session_id"])
        request_id = turn["native_request_id"]
        if not request_id:
            raise RuntimeError("过期轮次缺少原生请求 ID，停止清理")
        # 原生全历史读取可能分页较久，放在 SQLite 写锁之外。
        messages = await native.messages(user_id=user_id, session_id=session_id)
        related = [
            item for item in messages
            if item.get("request_id") == request_id
        ]
        if related and not any(
            item.get("id") == turn["native_message_id"]
            for item in related
        ):
            raise RuntimeError("原生消息和网关轮次不匹配，停止清理")
        message_ids = [item.get("id") for item in related]
        if any(not isinstance(value, str) for value in message_ids):
            raise RuntimeError("原生历史缺少消息 ID，停止清理")
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute(
                "SELECT t.trace_id FROM gateway_turns AS t JOIN "
                "gateway_conversations AS c ON "
                "c.deployment_id=t.deployment_id AND c.owner_id=t.owner_id "
                "AND c.conversation_id=t.conversation_id "
                "WHERE t.trace_id=? AND t.deployment_id=? "
                "AND t.created_at<? "
                "AND t.status NOT IN ('streaming','stop_requested') "
                "AND NOT (" + _recent_feedback_clause("t") + ")",
                (trace_id, deployment_id, cutoff, cutoff, cutoff, cutoff),
            ).fetchone()
            if current is None:
                connection.rollback()
                continue
            await asyncio.gather(*(
                native.delete_message(
                    user_id=user_id, session_id=session_id,
                    message_id=message_id,
                )
                for message_id in message_ids
            ))
            _delete_turn_children(connection, "trace_id=?", (trace_id,))
            connection.execute(
                "DELETE FROM gateway_turns WHERE trace_id=?", (trace_id,)
            )
            connection.commit()
            counts["turns"] += 1
        except BaseException:
            connection.rollback()
            raise

    for trace_id in feedback:
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = connection.execute(
                "SELECT f.trace_id FROM gateway_feedback AS f "
                "JOIN gateway_turns AS t ON t.trace_id=f.trace_id "
                "LEFT JOIN gateway_feedback_reviews AS r "
                "ON r.trace_id=f.trace_id "
                "WHERE f.trace_id=? AND t.deployment_id=? "
                "AND f.created_at<? "
                "AND (r.updated_at IS NULL OR r.updated_at<?)",
                (trace_id, deployment_id, cutoff, cutoff),
            ).fetchone()
            if current is None:
                connection.rollback()
                continue
            connection.execute(
                "DELETE FROM gateway_feedback_reviews WHERE trace_id=?",
                (trace_id,),
            )
            connection.execute(
                "DELETE FROM gateway_feedback WHERE trace_id=?",
                (trace_id,),
            )
            connection.commit()
            counts["feedback"] += 1
        except BaseException:
            connection.rollback()
            raise
    return counts


async def _run(execute: bool) -> dict[str, int]:
    auth = GatewayAuthSettings.from_environment()
    engine = EngineSettings.from_environment()
    cutoff = (datetime.now(UTC) - timedelta(days=RETENTION_DAYS)).isoformat()
    connection = _connect(auth.database_path, execute=execute)
    native: NativeHistoryClient | None = None
    try:
        if execute:
            native = NativeHistoryClient(
                base_url=engine.base_url,
                api_key=load_server_secret(engine.api_key_file),
                signer=ExternalPrincipalSigner(
                    secret=load_server_secret(engine.external_signing_key_file),
                    tenant_id=engine.tenant_id,
                    deployment_id=auth.deployment_id,
                ),
            )
        return await prune_history(
            connection,
            native,
            deployment_id=auth.deployment_id,
            cutoff=cutoff,
            execute=execute,
        )
    finally:
        connection.close()
        if native is not None:
            await native.close()


def main() -> None:
    """默认只预览；只有显式 --execute 才删除。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    counts = asyncio.run(_run(arguments.execute))
    mode = "deleted" if arguments.execute else "eligible"
    print(
        f"history_retention mode={mode} days={RETENTION_DAYS} "
        f"sessions={counts['sessions']} turns={counts['turns']} "
        f"feedback={counts['feedback']}"
    )


if __name__ == "__main__":
    main()
