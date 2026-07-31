"""保存有限用户问题和已验证 claim 摘要的 TTL 多轮上下文。"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rag_app.generation.answer import AnswerResult, AnswerStatus
from rag_app.model_contracts import (
    VerifiedClaimContext,
    VerifiedClaimSupport,
)

__all__ = ["ConversationContext", "ConversationStore"]

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

CREATE TABLE IF NOT EXISTS conversation_claims (
    turn_id TEXT NOT NULL,
    claim_ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (turn_id, claim_ordinal),
    FOREIGN KEY (turn_id)
        REFERENCES conversation_questions(turn_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_claim_supports (
    turn_id TEXT NOT NULL,
    claim_ordinal INTEGER NOT NULL,
    support_ordinal INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    locator TEXT NOT NULL,
    PRIMARY KEY (turn_id, claim_ordinal, support_ordinal),
    FOREIGN KEY (turn_id, claim_ordinal)
        REFERENCES conversation_claims(turn_id, claim_ordinal)
        ON DELETE CASCADE
);
"""
_MAX_IDENTIFIER_LENGTH = 128


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """一次改写可读取的有限问题和上一轮已验证 claims。"""

    questions: tuple[str, ...]
    verified_claims: tuple[VerifiedClaimContext, ...]


class ConversationStore:
    """保存有 TTL/轮数上限的问题和最小已验证 claim 摘要。"""

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
        return self._append(
            conversation_id,
            question,
            answer=None,
            now=now,
            turn_id=turn_id,
        )

    def append_turn(
        self,
        conversation_id: str,
        question: str,
        *,
        answer: AnswerResult,
        now: datetime,
        turn_id: str | None = None,
    ) -> str:
        """原子保存问题及从已验证 AnswerResult 投影的最小摘要。

        Args:
            conversation_id: 客户端稳定会话标识。
            question: 当前原始用户问题。
            answer: 已通过回答发布门禁的结果。
            now: 带时区当前时间。
            turn_id: 可选请求幂等标识。

        Returns:
            持久 turn ID。

        Raises:
            ValueError: AnswerResult 内部状态不一致。

        """
        claims = _project_verified_claims(answer)
        return self._append(
            conversation_id,
            question,
            answer=claims,
            now=now,
            turn_id=turn_id,
        )

    def _append(
        self,
        conversation_id: str,
        question: str,
        *,
        answer: tuple[VerifiedClaimContext, ...] | None,
        now: datetime,
        turn_id: str | None,
    ) -> str:
        """在单一事务中保存问题、可选 claims，并执行轮数裁剪。"""
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
            for claim_ordinal, claim in enumerate(answer or (), start=1):
                connection.execute(
                    """
                    INSERT INTO conversation_claims (
                        turn_id, claim_ordinal, text
                    ) VALUES (?, ?, ?)
                    """,
                    (resolved_turn_id, claim_ordinal, claim.text),
                )
                for support_ordinal, support in enumerate(
                    claim.supports,
                    start=1,
                ):
                    connection.execute(
                        """
                        INSERT INTO conversation_claim_supports (
                            turn_id, claim_ordinal, support_ordinal,
                            chunk_id, locator
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            resolved_turn_id,
                            claim_ordinal,
                            support_ordinal,
                            support.chunk_id,
                            support.locator,
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

    def get_rewrite_context(
        self,
        conversation_id: str,
        *,
        now: datetime,
    ) -> ConversationContext:
        """读取有限历史问题及最后一轮已验证 claim 摘要。

        Args:
            conversation_id: 客户端会话标识。
            now: 带时区当前时间。

        Returns:
            按时间顺序的问题及按声明/来源顺序的最小摘要。

        """
        _validate_input(conversation_id, "placeholder", now)
        now_text = now.astimezone(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM conversations WHERE expires_at <= ?",
                (now_text,),
            )
            question_rows = connection.execute(
                """
                SELECT turn_id, question FROM conversation_questions
                WHERE conversation_id = ?
                ORDER BY ordinal
                """,
                (conversation_id,),
            ).fetchall()
            if not question_rows:
                return ConversationContext(questions=(), verified_claims=())
            latest_turn_id = str(question_rows[-1]["turn_id"])
            claim_rows = connection.execute(
                """
                SELECT claim_ordinal, text FROM conversation_claims
                WHERE turn_id = ?
                ORDER BY claim_ordinal
                """,
                (latest_turn_id,),
            ).fetchall()
            claims = tuple(
                _load_claim(connection, latest_turn_id, row)
                for row in claim_rows
            )
        return ConversationContext(
            questions=tuple(str(row["question"]) for row in question_rows),
            verified_claims=claims,
        )

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


def _project_verified_claims(
    answer: AnswerResult,
) -> tuple[VerifiedClaimContext, ...]:
    """把发布结果投影为不含 quote、evidence ID 和原始输出的摘要。"""
    if answer.status is AnswerStatus.REFUSED:
        if (
            answer.answer is not None
            or answer.claims
            or answer.refusal_code is None
        ):
            raise ValueError("拒答 AnswerResult 状态不一致。")
        return ()
    expected_answer = "\n\n".join(claim.text for claim in answer.claims)
    if (
        answer.status is not AnswerStatus.ANSWERED
        or not answer.claims
        or answer.answer != expected_answer
        or answer.refusal_code is not None
    ):
        raise ValueError("可回答 AnswerResult 状态不一致。")
    return tuple(
        VerifiedClaimContext(
            text=claim.text,
            supports=tuple(
                VerifiedClaimSupport(
                    chunk_id=support.chunk_id,
                    locator=support.locator,
                )
                for support in claim.supports
            ),
        )
        for claim in answer.claims
    )


def _load_claim(
    connection: sqlite3.Connection,
    turn_id: str,
    row: sqlite3.Row,
) -> VerifiedClaimContext:
    """按冻结顺序从 SQLite 读取一条 claim 及最小支持摘要。"""
    claim_ordinal = int(row["claim_ordinal"])
    support_rows = connection.execute(
        """
        SELECT chunk_id, locator FROM conversation_claim_supports
        WHERE turn_id = ? AND claim_ordinal = ?
        ORDER BY support_ordinal
        """,
        (turn_id, claim_ordinal),
    ).fetchall()
    return VerifiedClaimContext(
        text=str(row["text"]),
        supports=tuple(
            VerifiedClaimSupport(
                chunk_id=str(support["chunk_id"]),
                locator=str(support["locator"]),
            )
            for support in support_rows
        ),
    )
