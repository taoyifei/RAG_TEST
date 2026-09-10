"""按 Product scope 保存加密问题与已验证事实摘要。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.core.errors import Conflict, ProviderUnavailable
from rag_app.core.models import (
    ConfidenceStatus,
    EvidenceItem,
    KnowledgeBaseScope,
)
from rag_app.core.models.search import SearchAnswerResult
from rag_app.product.crypto import SecretAad, SecretCipher

_CONVERSATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SUPPORT_REFERENCE = re.compile(r"\[([^\[\]\s]{1,128})\]")
_MAX_ROUNDS: Final = 8
_MAX_QUESTION_CHARS: Final = 2000
_MAX_CLAIM_CHARS: Final = 1400
_MAX_CONTEXT_CHARS: Final = 8000
_MAX_SESSION_CHARS: Final = 16_000
_MAX_CONFIGURED_SESSION_CHARS: Final = 64_000
_MAX_OWNER_ID_CHARS: Final = 256
_MAX_CLAIMS_PER_TURN: Final = 8
_MAX_SUPPORTS_PER_CLAIM: Final = 8
_DEFAULT_TTL_SECONDS: Final = 24 * 60 * 60
_LEASE_TIMEOUT_SECONDS: Final = 30.0


@dataclass(frozen=True, slots=True)
class ConversationClearResult:
    """一个严格 scope 内的会话删除结果。"""

    deleted: bool
    deleted_turns: int


@dataclass(slots=True)
class _LeaseState:
    """进程内同一会话的串行执行锁与引用数。"""

    lock: threading.Lock
    references: int = 0


@dataclass(frozen=True, slots=True)
class _ProjectedSupport:
    """不含引用正文的最小来源身份。"""

    revision_id: str
    document_id: str
    document_version_id: str
    chunk_id: str


@dataclass(frozen=True, slots=True)
class _ProjectedClaim:
    """一个完整短事实及其当前来源身份。"""

    text: str
    supports: tuple[_ProjectedSupport, ...]


class ProductConversationStore:
    """实现 TTL、范围隔离、来源复核和成功 final 幂等提交。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        cipher: SecretCipher,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        max_rounds: int = _MAX_ROUNDS,
        max_session_chars: int = _MAX_SESSION_CHARS,
    ) -> None:
        """冻结会话容量和加密依赖。

        Args:
            connections: 已完成 Product migration 的主库连接工厂。
            cipher: 只在进程内解密会话正文的 AES-GCM 包装器。
            ttl_seconds: 成功提交后会话的空闲保留秒数。
            max_rounds: 单会话保留的成功轮数。
            max_session_chars: 单会话问题与事实摘要总字符上限。

        Raises:
            ValueError: 任一容量不是安全正数。

        """
        if ttl_seconds <= 0:
            raise ValueError("会话 TTL 必须为正数。")
        if not 1 <= max_rounds <= _MAX_ROUNDS:
            raise ValueError("会话轮数上限必须在 1 到 8。")
        if not (
            _MAX_CONTEXT_CHARS
            <= max_session_chars
            <= _MAX_CONFIGURED_SESSION_CHARS
        ):
            raise ValueError("会话字符上限必须在 8000 到 64000。")
        self._connections = connections
        self._cipher = cipher
        self._ttl_seconds = ttl_seconds
        self._max_rounds = max_rounds
        self._max_session_chars = max_session_chars
        self._lease_guard = threading.Lock()
        self._leases: dict[tuple[str, str, str, str], _LeaseState] = {}

    @contextlib.contextmanager
    def lease(
        self,
        scope: KnowledgeBaseScope,
        conversation_id: str,
        *,
        owner_id: str,
    ) -> Iterator[None]:
        """串行化同一会话的 context-read/query/final-commit 生命周期。

        Args:
            scope: 已鉴权 Project 与 KB。
            conversation_id: 客户端提供的有限会话 ID。
            owner_id: 当前鉴权主体，不是 Bearer Token 明文。

        Yields:
            持有同一会话独占执行权的作用域。

        Returns:
            管理同一会话独占执行权的上下文迭代器。

        Raises:
            Conflict: 有界等待后同一会话仍在执行。

        """
        _validate_identity(owner_id, conversation_id)
        key = (
            owner_id,
            scope.project_id,
            scope.knowledge_base_id,
            conversation_id,
        )
        with self._lease_guard:
            state = self._leases.setdefault(key, _LeaseState(threading.Lock()))
            state.references += 1
        acquired = state.lock.acquire(timeout=_LEASE_TIMEOUT_SECONDS)
        if not acquired:
            self._release_reference(key, state, acquired=False)
            raise Conflict(
                "同一会话仍有查询在执行，请稍后重试。",
                stage="conversation.lease",
                code="CONVERSATION_BUSY",
                retryable=True,
            )
        try:
            yield
        finally:
            self._release_reference(key, state, acquired=True)

    def context(
        self,
        scope: KnowledgeBaseScope,
        conversation_id: str,
        *,
        owner_id: str,
    ) -> tuple[str, ...]:
        """只返回当前活动 Revision 仍可读来源支持的有限上下文。

        Args:
            scope: 已鉴权 Project 与 KB。
            conversation_id: 当前会话 ID。
            owner_id: 当前鉴权主体。

        Returns:
            时间顺序排列、每项不超过 2000 字符的会话轮次。

        """
        _validate_identity(owner_id, conversation_id)
        now = datetime.now(UTC).isoformat()
        try:
            with self._connections.transaction(write=True) as connection:
                self._delete_expired(connection, now)
                active_revision_id = _active_revision(
                    connection,
                    scope.project_id,
                    scope.knowledge_base_id,
                )
                if active_revision_id is None:
                    return ()
                rows = connection.execute(
                    "SELECT * FROM product_conversation_turns "
                    "WHERE owner_id=? AND project_id=? AND "
                    "knowledge_base_id=? AND conversation_id=? "
                    "ORDER BY ordinal DESC LIMIT ?",
                    (
                        owner_id,
                        scope.project_id,
                        scope.knowledge_base_id,
                        conversation_id,
                        self._max_rounds,
                    ),
                ).fetchall()
                values = [
                    self._render_turn(connection, row, active_revision_id)
                    for row in reversed(rows)
                ]
        except sqlite3.Error as error:
            raise _unavailable("conversation.context") from error
        bounded: list[str] = []
        chars = 0
        for value in values:
            if value is None or chars + len(value) > _MAX_CONTEXT_CHARS:
                continue
            bounded.append(value)
            chars += len(value)
        return tuple(bounded)

    def commit(
        self,
        scope: KnowledgeBaseScope,
        conversation_id: str,
        question: str,
        result: SearchAnswerResult,
        *,
        owner_id: str,
    ) -> bool:
        """保存成功 final；拒答只保存问题，失败和取消不进入上下文。

        Args:
            scope: 已鉴权 Project 与 KB。
            conversation_id: 当前会话 ID。
            question: 当前用户问题，不进入普通日志或技术 Trace。
            result: 已经完成唯一 final 发布的权威结果。
            owner_id: 当前鉴权主体。

        Returns:
            新增轮次时为 True，重复 trace 或非成功结果时为 False。

        """
        _validate_identity(owner_id, conversation_id)
        if not question.strip() or len(question) > _MAX_QUESTION_CHARS:
            raise ValueError("多轮问题必须为 1 到 2000 个字符。")
        if result.status not in {
            ConfidenceStatus.ANSWERABLE,
            ConfidenceStatus.INSUFFICIENT_EVIDENCE,
            ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION,
        }:
            return False
        terminal_status = "ANSWERED" if result.answer is not None else "REFUSED"
        claims = _project_claims(result)
        payload = {
            "question": question,
            "claims": [claim.text for claim in claims],
        }
        content_chars = len(question) + sum(len(claim.text) for claim in claims)
        ciphertext, nonce = self._cipher.encrypt(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            aad=_aad(result.trace_id),
        )
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        try:
            with self._connections.transaction(write=True) as connection:
                existing = connection.execute(
                    "SELECT owner_id, project_id, knowledge_base_id, "
                    "conversation_id, question_sha256 "
                    "FROM product_conversation_turns WHERE turn_id=?",
                    (result.trace_id,),
                ).fetchone()
                question_sha256 = hashlib.sha256(question.encode()).hexdigest()
                if existing is not None:
                    if tuple(existing) != (
                        owner_id,
                        scope.project_id,
                        scope.knowledge_base_id,
                        conversation_id,
                        question_sha256,
                    ):
                        raise Conflict(
                            "Trace ID 已绑定其他会话轮次。",
                            stage="conversation.commit",
                            code="CONVERSATION_TURN_CONFLICT",
                        )
                    return False
                if (
                    _active_revision(
                        connection,
                        scope.project_id,
                        scope.knowledge_base_id,
                    )
                    != result.active_index_revision_id
                ):
                    return False
                connection.execute(
                    "INSERT INTO product_conversations(owner_id, project_id, "
                    "knowledge_base_id, conversation_id, next_ordinal, "
                    "content_chars, created_at, updated_at, expires_at) "
                    "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?) "
                    "ON CONFLICT(owner_id, project_id, knowledge_base_id, "
                    "conversation_id) DO NOTHING",
                    (
                        owner_id,
                        scope.project_id,
                        scope.knowledge_base_id,
                        conversation_id,
                        now.isoformat(),
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                session = connection.execute(
                    "SELECT next_ordinal FROM product_conversations "
                    "WHERE owner_id=? AND project_id=? AND "
                    "knowledge_base_id=? AND conversation_id=?",
                    (
                        owner_id,
                        scope.project_id,
                        scope.knowledge_base_id,
                        conversation_id,
                    ),
                ).fetchone()
                if session is None:
                    raise RuntimeError("会话行未能在同一事务内创建。")
                ordinal = int(session["next_ordinal"])
                connection.execute(
                    "INSERT INTO product_conversation_turns(turn_id, owner_id, "
                    "project_id, knowledge_base_id, conversation_id, ordinal, "
                    "active_revision_id, terminal_status, question_sha256, "
                    "ciphertext, nonce, content_chars, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.trace_id,
                        owner_id,
                        scope.project_id,
                        scope.knowledge_base_id,
                        conversation_id,
                        ordinal,
                        result.active_index_revision_id,
                        terminal_status,
                        question_sha256,
                        ciphertext,
                        nonce,
                        content_chars,
                        now.isoformat(),
                    ),
                )
                _insert_supports(connection, result.trace_id, claims)
                connection.execute(
                    "UPDATE product_conversations SET next_ordinal=?, "
                    "content_chars=content_chars+?, updated_at=?, expires_at=? "
                    "WHERE owner_id=? AND project_id=? AND "
                    "knowledge_base_id=? AND conversation_id=?",
                    (
                        ordinal + 1,
                        content_chars,
                        now.isoformat(),
                        expires_at.isoformat(),
                        owner_id,
                        scope.project_id,
                        scope.knowledge_base_id,
                        conversation_id,
                    ),
                )
                self._prune_scope(
                    connection,
                    owner_id,
                    scope,
                    conversation_id,
                )
        except sqlite3.Error as error:
            raise _unavailable("conversation.commit") from error
        return True

    def clear(
        self,
        scope: KnowledgeBaseScope,
        conversation_id: str,
        *,
        owner_id: str,
    ) -> ConversationClearResult:
        """在同一严格 scope 内级联清理会话与支持身份。

        Args:
            scope: 已鉴权 Project 与 KB。
            conversation_id: 待清理会话 ID。
            owner_id: 会话所有者。

        Returns:
            是否存在会话以及删除的轮次数量。

        """
        _validate_identity(owner_id, conversation_id)
        try:
            with self._connections.transaction(write=True) as connection:
                row = connection.execute(
                    "SELECT COUNT(*) FROM product_conversation_turns "
                    "WHERE owner_id=? AND project_id=? AND "
                    "knowledge_base_id=? AND conversation_id=?",
                    (
                        owner_id,
                        scope.project_id,
                        scope.knowledge_base_id,
                        conversation_id,
                    ),
                ).fetchone()
                count = 0 if row is None else int(row[0])
                deleted = connection.execute(
                    "DELETE FROM product_conversations WHERE owner_id=? "
                    "AND project_id=? AND knowledge_base_id=? "
                    "AND conversation_id=?",
                    (
                        owner_id,
                        scope.project_id,
                        scope.knowledge_base_id,
                        conversation_id,
                    ),
                ).rowcount
        except sqlite3.Error as error:
            raise _unavailable("conversation.clear") from error
        return ConversationClearResult(bool(deleted), count)

    def _render_turn(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        active_revision_id: str,
    ) -> str | None:
        """解密一轮，并逐 claim 验证当前 Revision 与来源可读性。"""
        payload = cast(
            dict[str, object],
            json.loads(
                self._cipher.decrypt(
                    str(row["ciphertext"]),
                    str(row["nonce"]),
                    aad=_aad(str(row["turn_id"])),
                )
            ),
        )
        question = payload.get("question")
        claims = payload.get("claims")
        if not isinstance(question, str) or not isinstance(claims, list):
            raise ValueError("会话密文结构无效。")
        accepted: list[str] = []
        if str(row["active_revision_id"]) == active_revision_id:
            for index, claim in enumerate(claims):
                if isinstance(claim, str) and _claim_is_current(
                    connection,
                    str(row["turn_id"]),
                    index,
                    active_revision_id,
                ):
                    accepted.append(claim)
        parts = [f"上一问：{question}"]
        if accepted:
            parts.append("已验证事实：" + "；".join(accepted))
        value = "\n".join(parts)
        return value if len(value) <= _MAX_QUESTION_CHARS else None

    def _delete_expired(self, connection: sqlite3.Connection, now: str) -> None:
        connection.execute(
            "DELETE FROM product_conversations WHERE expires_at<=?", (now,)
        )

    def _prune_scope(
        self,
        connection: sqlite3.Connection,
        owner_id: str,
        scope: KnowledgeBaseScope,
        conversation_id: str,
    ) -> None:
        """按轮数和字符数确定性删除最旧成功轮次。"""
        while True:
            session = connection.execute(
                "SELECT content_chars FROM product_conversations "
                "WHERE owner_id=? AND project_id=? AND knowledge_base_id=? "
                "AND conversation_id=?",
                (
                    owner_id,
                    scope.project_id,
                    scope.knowledge_base_id,
                    conversation_id,
                ),
            ).fetchone()
            count_row = connection.execute(
                "SELECT COUNT(*) FROM product_conversation_turns "
                "WHERE owner_id=? AND project_id=? AND knowledge_base_id=? "
                "AND conversation_id=?",
                (
                    owner_id,
                    scope.project_id,
                    scope.knowledge_base_id,
                    conversation_id,
                ),
            ).fetchone()
            if session is None or count_row is None:
                return
            if (
                int(count_row[0]) <= self._max_rounds
                and int(session["content_chars"]) <= self._max_session_chars
            ):
                return
            oldest = connection.execute(
                "SELECT turn_id, content_chars FROM product_conversation_turns "
                "WHERE owner_id=? AND project_id=? AND knowledge_base_id=? "
                "AND conversation_id=? ORDER BY ordinal LIMIT 1",
                (
                    owner_id,
                    scope.project_id,
                    scope.knowledge_base_id,
                    conversation_id,
                ),
            ).fetchone()
            if oldest is None:
                return
            connection.execute(
                "DELETE FROM product_conversation_turns WHERE turn_id=?",
                (oldest["turn_id"],),
            )
            connection.execute(
                "UPDATE product_conversations SET content_chars="
                "max(0, content_chars-?) WHERE owner_id=? AND project_id=? "
                "AND knowledge_base_id=? AND conversation_id=?",
                (
                    oldest["content_chars"],
                    owner_id,
                    scope.project_id,
                    scope.knowledge_base_id,
                    conversation_id,
                ),
            )

    def _release_reference(
        self,
        key: tuple[str, str, str, str],
        state: _LeaseState,
        *,
        acquired: bool,
    ) -> None:
        if acquired:
            state.lock.release()
        with self._lease_guard:
            state.references -= 1
            if state.references == 0 and not state.lock.locked():
                self._leases.pop(key, None)


def _project_claims(result: SearchAnswerResult) -> tuple[_ProjectedClaim, ...]:
    if result.answer is None:
        return ()
    evidence = {item.evidence_id: item for item in result.evidence}
    claims: list[_ProjectedClaim] = []
    if result.generation_mode == "llm":
        for line in result.answer.splitlines():
            support_ids = tuple(dict.fromkeys(_SUPPORT_REFERENCE.findall(line)))
            text = _SUPPORT_REFERENCE.sub("", line).strip()
            supports = _supports_for_ids(
                result.active_index_revision_id,
                evidence,
                support_ids,
            )
            if text and len(text) <= _MAX_CLAIM_CHARS and supports:
                claims.append(_ProjectedClaim(text, supports))
            if len(claims) >= _MAX_CLAIMS_PER_TURN:
                break
        return tuple(claims)
    if len(result.answer) <= _MAX_CLAIM_CHARS:
        text = result.answer
    else:
        text = next(
            (
                item.citation_text
                for item in result.evidence
                if len(item.citation_text) <= _MAX_CLAIM_CHARS
            ),
            "",
        )
    supports = _supports_for_ids(
        result.active_index_revision_id,
        evidence,
        tuple(evidence),
    )
    if not text or not supports:
        return ()
    return (_ProjectedClaim(text, supports),)


def _supports_for_ids(
    revision_id: str,
    evidence: dict[str, EvidenceItem],
    support_ids: tuple[str, ...],
) -> tuple[_ProjectedSupport, ...]:
    values: list[_ProjectedSupport] = []
    for support_id in support_ids:
        item = evidence.get(support_id)
        if (
            item is None
            or item.document_id is None
            or item.document_version_id is None
        ):
            continue
        values.append(
            _ProjectedSupport(
                revision_id=revision_id,
                document_id=item.document_id,
                document_version_id=item.document_version_id,
                chunk_id=item.chunk_id,
            )
        )
        if len(values) >= _MAX_SUPPORTS_PER_CLAIM:
            break
    return tuple(values)


def _insert_supports(
    connection: sqlite3.Connection,
    turn_id: str,
    claims: tuple[_ProjectedClaim, ...],
) -> None:
    for claim_index, claim in enumerate(claims):
        for support_index, support in enumerate(claim.supports):
            connection.execute(
                "INSERT INTO product_conversation_supports(turn_id, "
                "claim_ordinal, support_ordinal, revision_id, document_id, "
                "document_version_id, chunk_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    claim_index,
                    support_index,
                    support.revision_id,
                    support.document_id,
                    support.document_version_id,
                    support.chunk_id,
                ),
            )


def _claim_is_current(
    connection: sqlite3.Connection,
    turn_id: str,
    claim_ordinal: int,
    active_revision_id: str,
) -> bool:
    supports = connection.execute(
        "SELECT revision_id, document_id, document_version_id, chunk_id "
        "FROM product_conversation_supports WHERE turn_id=? AND "
        "claim_ordinal=? ORDER BY support_ordinal",
        (turn_id, claim_ordinal),
    ).fetchall()
    if not supports:
        return False
    for support in supports:
        if str(support["revision_id"]) != active_revision_id:
            return False
        current = connection.execute(
            "SELECT 1 FROM chunks c JOIN documents d "
            "ON d.document_id=c.document_id WHERE c.revision_id=? "
            "AND c.document_id=? AND c.document_version_id=? "
            "AND c.chunk_id=? AND d.deleted_at IS NULL "
            "AND d.lifecycle_status='active'",
            (
                active_revision_id,
                support["document_id"],
                support["document_version_id"],
                support["chunk_id"],
            ),
        ).fetchone()
        if current is None:
            return False
    return True


def _active_revision(
    connection: sqlite3.Connection,
    project_id: str,
    knowledge_base_id: str,
) -> str | None:
    row = connection.execute(
        "SELECT kb.active_revision_id FROM knowledge_bases kb "
        "JOIN projects p ON p.project_id=kb.project_id "
        "JOIN index_revisions r ON r.index_revision_id=kb.active_revision_id "
        "WHERE kb.project_id=? AND kb.knowledge_base_id=? "
        "AND kb.deleted_at IS NULL AND kb.lifecycle_status='active' "
        "AND p.deleted_at IS NULL AND p.lifecycle_status='active' "
        "AND r.state='active'",
        (project_id, knowledge_base_id),
    ).fetchone()
    return None if row is None else str(row["active_revision_id"])


def _validate_identity(owner_id: str, conversation_id: str) -> None:
    if not owner_id or len(owner_id) > _MAX_OWNER_ID_CHARS:
        raise ValueError("会话 owner_id 长度无效。")
    if _CONVERSATION_ID.fullmatch(conversation_id) is None:
        raise ValueError("conversation_id 字符或长度无效。")


def _aad(turn_id: str) -> SecretAad:
    return SecretAad(
        credential_id=turn_id,
        provider_type="local-product-conversation",
        field_name="conversation-turn-v1",
        key_version=1,
    )


def _unavailable(stage: str) -> ProviderUnavailable:
    return ProviderUnavailable(
        "本地会话暂时不可用。",
        stage=stage,
        code="CONVERSATION_STORE_UNAVAILABLE",
        retryable=True,
    )


__all__ = ["ConversationClearResult", "ProductConversationStore"]
