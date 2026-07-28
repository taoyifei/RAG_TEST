"""只暂存用户问题的 TTL 多轮上下文。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = ["ConversationStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_questions (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    question TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, ordinal),
    FOREIGN KEY (conversation_id)
        REFERENCES conversations(conversation_id)
        ON DELETE CASCADE
);
"""
_MAX_IDENTIFIER_LENGTH = 128


class ConversationStore:
    """保存有 TTL/轮数上限的用户问题，不保存历史答案。"""

    def __init__(
        self,
        database_path: Path,
        *,
        ttl_seconds: int,
        max_rounds: int,
    ) -> None:
        """冻结存储路径与多轮边界。

        Args:
            database_path: SQLite WAL 数据库。
            ttl_seconds: 会话无活动后的保留秒数。
            max_rounds: 每会话最多保留的问题数。

        Raises:
            ValueError: TTL 或轮数不为正数。

        """
        if ttl_seconds <= 0 or max_rounds <= 0:
            raise ValueError("会话 TTL 与轮数上限必须为正数。")
        self._database_path = database_path
        self._ttl_seconds = ttl_seconds
        self._max_rounds = max_rounds

    def initialize(self) -> None:
        """初始化 TTL 会话表。

        Args:
            无参数；初始化当前数据库路径。

        Returns:
            无返回值。

        """
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    def append_question(
        self,
        conversation_id: str,
        question: str,
        *,
        now: datetime,
        turn_id: str | None = None,
    ) -> str:
        """幂等追加用户问题，并裁掉超出轮数的最旧问题。

        Args:
            conversation_id: 客户端稳定会话标识。
            question: 当前原始用户问题。
            now: 带时区当前时间。
            turn_id: 可选请求幂等标识；缺失时本地生成。

        Returns:
            持久 turn ID。

        Raises:
            ValueError: 标识、问题或时间无效。

        """
        _validate_input(conversation_id, question, now)
        resolved_turn_id = turn_id or f"turn_{uuid.uuid4().hex}"
        if (
            not resolved_turn_id
            or len(resolved_turn_id) > _MAX_IDENTIFIER_LENGTH
        ):
            raise ValueError("turn_id 长度无效。")
        now_utc = now.astimezone(UTC)
        now_text = now_utc.isoformat()
        expires_text = (
            now_utc + timedelta(seconds=self._ttl_seconds)
        ).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_turn = connection.execute(
                """
                SELECT turn_id FROM conversation_questions
                WHERE turn_id = ?
                """,
                (resolved_turn_id,),
            ).fetchone()
            if existing_turn is not None:
                connection.commit()
                return resolved_turn_id
            connection.execute(
                """
                DELETE FROM conversations
                WHERE conversation_id = ? AND expires_at <= ?
                """,
                (conversation_id, now_text),
            )
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, expires_at, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, expires_text, now_text),
            )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) + 1
                FROM conversation_questions
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            ordinal = int(_require_row(row)[0])
            connection.execute(
                """
                INSERT INTO conversation_questions (
                    turn_id, conversation_id, ordinal, question, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    resolved_turn_id,
                    conversation_id,
                    ordinal,
                    question.strip(),
                    now_text,
                ),
            )
            connection.execute(
                """
                DELETE FROM conversation_questions
                WHERE conversation_id = ? AND turn_id NOT IN (
                    SELECT turn_id FROM conversation_questions
                    WHERE conversation_id = ?
                    ORDER BY ordinal DESC
                    LIMIT ?
                )
                """,
                (conversation_id, conversation_id, self._max_rounds),
            )
            connection.commit()
        return resolved_turn_id

    def get_questions(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """读取未过期的历史用户问题。

        Args:
            conversation_id: 客户端会话标识。
            now: 带时区当前时间。

        Returns:
            按时间正序的最近问题；过期时为空。

        """
        _validate_input(conversation_id, "placeholder", now)
        now_text = now.astimezone(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM conversations WHERE expires_at <= ?",
                (now_text,),
            )
            rows = connection.execute(
                """
                SELECT question FROM conversation_questions
                WHERE conversation_id = ?
                ORDER BY ordinal
                """,
                (conversation_id,),
            ).fetchall()
        return tuple(str(row["question"]) for row in rows)

    def clear(self, conversation_id: str) -> None:
        """立即删除会话及其全部问题。

        Args:
            conversation_id: 客户端会话标识。

        Returns:
            无返回值。

        """
        if (
            not conversation_id
            or len(conversation_id) > _MAX_IDENTIFIER_LENGTH
        ):
            raise ValueError("conversation_id 长度无效。")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


def _validate_input(
    conversation_id: str,
    question: str,
    now: datetime,
) -> None:
    if (
        not conversation_id
        or len(conversation_id) > _MAX_IDENTIFIER_LENGTH
    ):
        raise ValueError("conversation_id 长度无效。")
    if not question.strip():
        raise ValueError("question 不能为空。")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必须包含时区。")


def _require_row(row: sqlite3.Row | None) -> sqlite3.Row:
    if row is None:
        raise RuntimeError("SQLite 未返回预期会话行。")
    return row
