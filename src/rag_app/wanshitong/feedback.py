"""湾事通反馈详情、管理复核与安全导出服务。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, cast

from cryptography.exceptions import InvalidTag

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.core.errors import Conflict, NotFound, ProviderUnavailable
from rag_app.product.crypto import SecretAad, SecretCipher
from rag_app.product.feedback import (
    FeedbackReason,
    ProductFeedback,
    ProductFeedbackStore,
    ProjectionState,
    normalize_trace_id,
)
from rag_app.product.query_history import ProductQueryHistory
from rag_app.product.trace_coordinator import ProductTraceCoordinator
from rag_app.tracing.store import TraceNotFoundError

FeedbackReasonDetail = Literal[
    "INCORRECT",
    "INCOMPLETE",
    "WRONG_SOURCE",
    "FALSE_REFUSAL",
    "UNSAFE_ANSWER",
    "TOO_SLOW",
    "OTHER",
]
FeedbackReviewStatus = Literal[
    "NEW",
    "REVIEWED",
    "FIX_PLANNED",
    "RESOLVED",
    "EXPECTED_BEHAVIOR",
]
FeedbackRootCause = Literal[
    "CONTEXT_RESOLUTION_WRONG",
    "RETRIEVAL_NO_CANDIDATE",
    "CORRECT_SOURCE_NOT_IN_EVIDENCE_PACK",
    "EVIDENCE_HARD_REJECTED_WRONGLY",
    "WRONG_EVIDENCE_SELECTED",
    "GENERATION_EMPTY",
    "GENERATION_INCOMPLETE",
    "CLAIM_ALL_REJECTED",
    "CLAIM_UNSUPPORTED_ACCEPTED",
    "CLAIM_NUMBER_OR_NEGATION_ERROR",
    "STRUCTURAL_SIBLING_CONTAMINATION",
    "TEMPLATE_BOUNDARY_ERROR",
    "FALSE_REFUSAL",
    "WRONG_SOURCE",
    "DEPARTMENT_ROUTE_WRONG",
    "TOO_SLOW",
    "EXPECTED_REFUSAL",
    "OTHER",
]

_REASON_DETAILS: Final = frozenset(
    {
        "INCORRECT",
        "INCOMPLETE",
        "WRONG_SOURCE",
        "FALSE_REFUSAL",
        "UNSAFE_ANSWER",
        "TOO_SLOW",
        "OTHER",
    }
)
_DETAIL_TO_CANONICAL: Final[dict[str, FeedbackReason]] = {
    "INCORRECT": "INCORRECT",
    "INCOMPLETE": "INCOMPLETE",
    "WRONG_SOURCE": "WRONG_SOURCE",
    "FALSE_REFUSAL": "INCORRECT",
    "UNSAFE_ANSWER": "INCORRECT",
    "TOO_SLOW": "TOO_SLOW",
    "OTHER": "OTHER",
}
_REVIEW_STATUSES: Final = frozenset(
    {"NEW", "REVIEWED", "FIX_PLANNED", "RESOLVED", "EXPECTED_BEHAVIOR"}
)
_ROOT_CAUSES: Final = frozenset(
    {
        "CONTEXT_RESOLUTION_WRONG",
        "RETRIEVAL_NO_CANDIDATE",
        "CORRECT_SOURCE_NOT_IN_EVIDENCE_PACK",
        "EVIDENCE_HARD_REJECTED_WRONGLY",
        "WRONG_EVIDENCE_SELECTED",
        "GENERATION_EMPTY",
        "GENERATION_INCOMPLETE",
        "CLAIM_ALL_REJECTED",
        "CLAIM_UNSUPPORTED_ACCEPTED",
        "CLAIM_NUMBER_OR_NEGATION_ERROR",
        "STRUCTURAL_SIBLING_CONTAMINATION",
        "TEMPLATE_BOUNDARY_ERROR",
        "FALSE_REFUSAL",
        "WRONG_SOURCE",
        "DEPARTMENT_ROUTE_WRONG",
        "TOO_SLOW",
        "EXPECTED_REFUSAL",
        "OTHER",
    }
)
_UNREADABLE = object()
_MAX_PAGE_SIZE = 100
_MAX_EXPORT_ITEMS = 1000


@dataclass(frozen=True, slots=True)
class WanshitongFeedback:
    """公共接口可返回的最小反馈状态。"""

    trace_id: str
    useful: bool
    reason_code: FeedbackReason | None
    reason_detail: FeedbackReasonDetail | None
    comment_saved: bool
    feedback_revision: int
    projection_state: ProjectionState
    updated_at: str


class WanshitongFeedbackService:
    """在 canonical 反馈之上增加密文详情与可追踪复核。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        canonical: ProductFeedbackStore,
        cipher: SecretCipher,
        history: ProductQueryHistory,
        traces: ProductTraceCoordinator,
    ) -> None:
        self._connections = connections
        self._canonical = canonical
        self._cipher = cipher
        self._history = history
        self._traces = traces

    def upsert(  # noqa: PLR0913
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
        actor_owner_id: str,
        useful: bool,
        reason_code: FeedbackReason | None,
        reason_detail: FeedbackReasonDetail | None,
        comment: str | None,
    ) -> WanshitongFeedback:
        """原子写入 canonical 反馈与湾事通详情。

        Args:
            trace_id: 已完成的 Query Trace ID。
            project_id: 服务端固定项目 ID。
            knowledge_base_id: 服务端固定知识库 ID。
            actor_owner_id: 当前公共会话的 owner ID。
            useful: 用户是否认为结果有帮助。
            reason_code: 兼容主表原因。
            reason_detail: 湾事通细分原因。
            comment: 可选说明明文，仅在调用栈中短暂存在。

        Returns:
            已提交的最终服务端状态。

        """
        canonical_trace_id = normalize_trace_id(trace_id)
        normalized_reason, normalized_detail, normalized_comment = (
            _normalize_user_feedback(
                useful,
                reason_code=reason_code,
                reason_detail=reason_detail,
                comment=comment,
            )
        )
        try:
            with self._connections.transaction(write=True) as connection:
                previous = connection.execute(
                    "SELECT f.useful, f.reason_code, d.* FROM "
                    "product_feedback f LEFT JOIN "
                    "wanshitong_feedback_details d USING(trace_id) "
                    "WHERE f.trace_id=?",
                    (canonical_trace_id,),
                ).fetchone()
                canonical = self._canonical._upsert_in_transaction(
                    connection,
                    canonical_trace_id,
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                    actor_owner_id=actor_owner_id,
                    actor_is_admin=False,
                    useful=useful,
                    reason_code=normalized_reason,
                )
                revision = self._write_details(
                    connection,
                    canonical,
                    previous=previous,
                    reason_detail=normalized_detail,
                    comment=normalized_comment,
                )
        except sqlite3.Error as error:
            raise _unavailable("wanshitong.feedback.upsert") from error

        # 跨库 Trace 投影在主事务提交后尝试，不回滚已收到的反馈。
        with suppress(ProviderUnavailable):
            self._canonical.reconcile_trace(canonical_trace_id)
        stored = self._canonical.get(
            canonical_trace_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            actor_owner_id=actor_owner_id,
            actor_is_admin=False,
        )
        if stored is None:
            raise RuntimeError("扩展反馈提交后无法回读 canonical 记录。")
        return WanshitongFeedback(
            trace_id=stored.trace_id,
            useful=stored.useful,
            reason_code=stored.reason_code,
            reason_detail=normalized_detail,
            comment_saved=normalized_comment is not None,
            feedback_revision=revision,
            projection_state=stored.projection_state,
            updated_at=stored.updated_at,
        )

    def list_feedback(  # noqa: PLR0913
        self,
        *,
        project_id: str,
        knowledge_base_id: str,
        reason: str | None = None,
        review_status: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        page_size: int = 20,
        offset: int = 0,
    ) -> dict[str, object]:
        """返回固定 Scope 的有界反馈待办列表。"""
        if not 1 <= page_size <= _MAX_PAGE_SIZE or offset < 0:
            raise ValueError("反馈分页越界。")
        if reason is not None and reason not in (
            _REASON_DETAILS | {"OUTDATED"}
        ):
            raise ValueError("反馈原因筛选值无效。")
        if review_status is not None and review_status not in _REVIEW_STATUSES:
            raise ValueError("复核状态筛选值无效。")
        conditions = ["f.project_id=?", "f.knowledge_base_id=?"]
        parameters: list[object] = [project_id, knowledge_base_id]
        if reason is not None:
            conditions.append("COALESCE(d.reason_detail, f.reason_code)=?")
            parameters.append(reason)
        if review_status is not None:
            conditions.append("COALESCE(r.review_status, 'NEW')=?")
            parameters.append(review_status)
        if created_from is not None:
            conditions.append("f.created_at>=?")
            parameters.append(created_from)
        if created_to is not None:
            conditions.append("f.created_at<=?")
            parameters.append(created_to)
        where_clause = " AND ".join(conditions)
        # 条件中的列名与运算符全部来自上方固定集合。
        count_query = (
            "SELECT COUNT(*) FROM product_feedback f LEFT JOIN "  # noqa: S608
            "wanshitong_feedback_details d USING(trace_id) "
            "LEFT JOIN wanshitong_feedback_reviews r "
            "USING(trace_id) WHERE "
            + where_clause
        )
        try:
            with self._connections.transaction() as connection:
                total = int(
                    connection.execute(
                        count_query,
                        parameters,
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    _feedback_select()
                    + " WHERE "
                    + where_clause
                    + " ORDER BY f.created_at DESC, f.trace_id DESC "
                    "LIMIT ? OFFSET ?",
                    (*parameters, page_size, offset),
                ).fetchall()
        except sqlite3.Error as error:
            raise _unavailable("wanshitong.feedback.list") from error
        items = [
            self._list_item(
                row,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
            for row in rows
        ]
        next_offset = (
            offset + len(items) if offset + len(items) < total else None
        )
        return {
            "items": items,
            "total": total,
            "page_size": page_size,
            "offset": offset,
            "next_offset": next_offset,
        }

    def detail(
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
    ) -> dict[str, object]:
        """返回受权 History、Trace 四段投影与复核状态。"""
        canonical = normalize_trace_id(trace_id)
        row = self._feedback_row(
            canonical,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        history, history_error = self._history_detail(
            canonical,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        trace, trace_error = self._trace_detail(canonical)
        projection = project_feedback_trace(history, trace)
        comment, comment_available, comment_error = self._read_secret(
            row,
            ciphertext_name="comment_ciphertext",
            nonce_name="comment_nonce",
            key_id_name="detail_encryption_key_id",
            field_name="comment",
            version=int(row["feedback_revision"] or 1),
        )
        note, note_available, note_error = self._read_secret(
            row,
            ciphertext_name="note_ciphertext",
            nonce_name="note_nonce",
            key_id_name="review_encryption_key_id",
            field_name="review_note",
            version=int(row["review_version"] or 1),
        )
        feedback_revision = int(row["feedback_revision"] or 1)
        reviewed_revision = int(row["reviewed_feedback_revision"] or 0)
        return {
            "trace_id": canonical,
            "feedback": {
                "useful": bool(row["useful"]),
                "reason_code": row["reason_code"],
                "reason_detail": row["reason_detail"],
                "comment": comment,
                "comment_available": comment_available,
                "comment_unavailable_reason": comment_error,
                "feedback_revision": feedback_revision,
                "projection_state": row["projection_state"],
                "created_at": row["feedback_created_at"],
                "updated_at": row["feedback_updated_at"],
            },
            "review": {
                "review_status": row["review_status"] or "NEW",
                "root_cause": row["root_cause"],
                "note": note,
                "note_available": note_available,
                "note_unavailable_reason": note_error,
                "selected_source_document_id": row[
                    "selected_source_document_id"
                ],
                "selected_source_version_id": row[
                    "selected_source_version_id"
                ],
                "reviewed_feedback_revision": reviewed_revision,
                "evaluation_candidate": bool(row["evaluation_candidate"] or 0),
                "fix_reference": row["fix_reference"],
                "verification_references": _json_string_list(
                    row["verification_reference_json"]
                ),
                "review_version": int(row["review_version"] or 0),
                "reviewed_by_admin": row["reviewed_by_admin"],
                "updated_at": row["review_updated_at"],
                "new_feedback_pending": feedback_revision > reviewed_revision,
            },
            "history": _public_history(history, history_error),
            "citations": _history_citations(history),
            "trace_projection": projection,
            "operational_trace": trace,
            "operational_trace_unavailable_reason": trace_error,
        }

    def review(  # noqa: PLR0913
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
        expected_version: int,
        review_status: FeedbackReviewStatus,
        root_cause: FeedbackRootCause | None,
        note: str | None,
        selected_source_document_id: str | None,
        selected_source_version_id: str | None,
        evaluation_candidate: bool,
        fix_reference: str | None,
        verification_references: Sequence[str],
        reviewed_by_admin: str,
    ) -> dict[str, object]:
        """以乐观锁保存管理员复核。"""
        canonical = normalize_trace_id(trace_id)
        normalized_note = _optional_text(note)
        normalized_fix = _optional_text(fix_reference)
        normalized_verifications = tuple(
            value
            for value in (
                _optional_text(item) for item in verification_references
            )
            if value
        )
        _validate_review(
            review_status,
            root_cause=root_cause,
            note=normalized_note,
            fix_reference=normalized_fix,
            verification_references=normalized_verifications,
        )
        self._validate_selected_source(
            canonical,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=selected_source_document_id,
            version_id=selected_source_version_id,
        )
        now = datetime.now(UTC).isoformat()
        try:
            with self._connections.transaction(write=True) as connection:
                row = connection.execute(
                    "SELECT f.trace_id, d.feedback_revision, "
                    "r.review_version FROM product_feedback f JOIN "
                    "wanshitong_feedback_details d USING(trace_id) "
                    "LEFT JOIN wanshitong_feedback_reviews r USING(trace_id) "
                    "WHERE f.trace_id=? AND f.project_id=? "
                    "AND f.knowledge_base_id=?",
                    (canonical, project_id, knowledge_base_id),
                ).fetchone()
                if row is None:
                    raise NotFound(
                        "反馈待办不存在。", stage="wanshitong.feedback.review"
                    )
                current_version = int(row["review_version"] or 0)
                if current_version != expected_version:
                    raise Conflict(
                        "反馈复核已被其他管理员更新，请刷新后重试。",
                        stage="wanshitong.feedback.review",
                        details={"current_version": current_version},
                    )
                review_version = current_version + 1
                ciphertext, nonce = self._encrypt_secret(
                    canonical,
                    normalized_note,
                    field_name="review_note",
                    version=review_version,
                )
                connection.execute(
                    "INSERT INTO wanshitong_feedback_reviews("
                    "trace_id, review_status, root_cause, note_ciphertext, "
                    "note_nonce, encryption_key_id, "
                    "selected_source_document_id, selected_source_version_id, "
                    "reviewed_feedback_revision, evaluation_candidate, "
                    "fix_reference, verification_reference_json, "
                    "review_version, reviewed_by_admin, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(trace_id) DO UPDATE SET "
                    "review_status=excluded.review_status, "
                    "root_cause=excluded.root_cause, "
                    "note_ciphertext=excluded.note_ciphertext, "
                    "note_nonce=excluded.note_nonce, "
                    "encryption_key_id=excluded.encryption_key_id, "
                    "selected_source_document_id="
                    "excluded.selected_source_document_id, "
                    "selected_source_version_id="
                    "excluded.selected_source_version_id, "
                    "reviewed_feedback_revision="
                    "excluded.reviewed_feedback_revision, "
                    "evaluation_candidate=excluded.evaluation_candidate, "
                    "fix_reference=excluded.fix_reference, "
                    "verification_reference_json="
                    "excluded.verification_reference_json, "
                    "review_version=excluded.review_version, "
                    "reviewed_by_admin=excluded.reviewed_by_admin, "
                    "updated_at=excluded.updated_at",
                    (
                        canonical,
                        review_status,
                        root_cause,
                        ciphertext,
                        nonce,
                        self._cipher.key_id if ciphertext is not None else None,
                        selected_source_document_id,
                        selected_source_version_id,
                        int(row["feedback_revision"]),
                        int(evaluation_candidate),
                        normalized_fix,
                        json.dumps(
                            normalized_verifications,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        review_version,
                        reviewed_by_admin,
                        now,
                    ),
                )
        except sqlite3.Error as error:
            raise _unavailable("wanshitong.feedback.review") from error
        return self.detail(
            canonical,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )

    def statistics(
        self, *, project_id: str, knowledge_base_id: str
    ) -> dict[str, object]:
        """返回轻量反馈与实际请求延迟统计。"""
        try:
            with self._connections.transaction() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS evaluated, "
                    "SUM(CASE WHEN f.useful=1 THEN 1 ELSE 0 END) AS helpful, "
                    "SUM(CASE WHEN f.useful=0 AND (r.trace_id IS NULL OR "
                    "r.review_status='NEW' OR d.feedback_revision>"
                    "r.reviewed_feedback_revision) THEN 1 ELSE 0 END) "
                    "AS pending, "
                    "SUM(CASE WHEN r.root_cause IN ('WRONG_SOURCE', "
                    "'WRONG_EVIDENCE_SELECTED') THEN 1 ELSE 0 END) "
                    "AS wrong_source, "
                    "SUM(CASE WHEN r.root_cause='FALSE_REFUSAL' THEN 1 "
                    "ELSE 0 END) AS false_refusal "
                    "FROM product_feedback f JOIN "
                    "wanshitong_feedback_details d USING(trace_id) "
                    "LEFT JOIN wanshitong_feedback_reviews r USING(trace_id) "
                    "WHERE f.project_id=? AND f.knowledge_base_id=?",
                    (project_id, knowledge_base_id),
                ).fetchone()
                latency_rows = connection.execute(
                    "SELECT owner_id, duration_ms FROM query_history "
                    "WHERE project_id=? AND knowledge_base_id=? "
                    "AND duration_ms IS NOT NULL ORDER BY duration_ms",
                    (project_id, knowledge_base_id),
                ).fetchall()
        except sqlite3.Error as error:
            raise _unavailable("wanshitong.feedback.statistics") from error
        evaluated = int(row["evaluated"] or 0)
        helpful = int(row["helpful"] or 0)
        actual = [
            int(item["duration_ms"])
            for item in latency_rows
            if str(item["owner_id"]).startswith("rdms:")
        ]
        non_sso = [
            int(item["duration_ms"])
            for item in latency_rows
            if not str(item["owner_id"]).startswith("rdms:")
        ]
        return {
            "evaluated_count": evaluated,
            "helpful_count": helpful,
            "helpful_rate": None if evaluated == 0 else helpful / evaluated,
            "pending_count": int(row["pending"] or 0),
            "confirmed_wrong_source_count": int(row["wrong_source"] or 0),
            "confirmed_false_refusal_count": int(row["false_refusal"] or 0),
            "latency_ms": {
                "actual_sso_users": _latency_summary(actual),
                "non_sso_or_replay": _latency_summary(non_sso),
            },
        }

    def export_metadata(
        self,
        *,
        project_id: str,
        knowledge_base_id: str,
        trace_ids: Sequence[str] = (),
    ) -> list[dict[str, object]]:
        """导出不含正文、评论、Note 或 Trace 的安全元数据。"""
        if len(trace_ids) > _MAX_EXPORT_ITEMS:
            raise ValueError("反馈导出最多允许 1000 条。")
        canonical_ids = tuple(normalize_trace_id(value) for value in trace_ids)
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("反馈导出不接受重复 Trace ID。")
        conditions = ["f.project_id=?", "f.knowledge_base_id=?"]
        parameters: list[object] = [project_id, knowledge_base_id]
        if canonical_ids:
            placeholders = ",".join("?" for _ in canonical_ids)
            conditions.append(f"f.trace_id IN ({placeholders})")
            parameters.extend(canonical_ids)
        try:
            with self._connections.transaction() as connection:
                rows = connection.execute(
                    _feedback_select()
                    + " WHERE "
                    + " AND ".join(conditions)
                    + " ORDER BY f.created_at, f.trace_id LIMIT ?",
                    (*parameters, _MAX_EXPORT_ITEMS),
                ).fetchall()
        except sqlite3.Error as error:
            raise _unavailable("wanshitong.feedback.export") from error
        return [
            {
                "feedback_id": str(row["trace_id"]),
                "feedback_id_source": "trace_id_alias",
                "trace_id": str(row["trace_id"]),
                "question_sha256": row["question_sha256"],
                "final_status": row["history_status"],
                "answer_path": _answer_path_from_metadata(row["metadata_json"]),
                "user_reason": row["reason_detail"] or row["reason_code"],
                "admin_root_cause": row["root_cause"],
                "selected_source": (
                    None
                    if row["selected_source_document_id"] is None
                    else {
                        "document_id": row["selected_source_document_id"],
                        "document_version_id": row[
                            "selected_source_version_id"
                        ],
                    }
                ),
                "created_at": row["feedback_created_at"],
            }
            for row in rows
        ]

    def _write_details(
        self,
        connection: sqlite3.Connection,
        canonical: ProductFeedback,
        *,
        previous: sqlite3.Row | None,
        reason_detail: FeedbackReasonDetail | None,
        comment: str | None,
    ) -> int:
        previous_revision = (
            0 if previous is None else int(previous["feedback_revision"] or 0)
        )
        previous_comment = self._stored_comment(previous)
        unchanged = bool(
            previous is not None
            and previous_revision > 0
            and bool(previous["useful"]) == canonical.useful
            and previous["reason_code"] == canonical.reason_code
            and previous["reason_detail"] == reason_detail
            and previous_comment is not _UNREADABLE
            and previous_comment == comment
        )
        if unchanged:
            return previous_revision
        revision = previous_revision + 1 if previous_revision else 1
        ciphertext, nonce = self._encrypt_secret(
            canonical.trace_id,
            comment,
            field_name="comment",
            version=revision,
        )
        connection.execute(
            "INSERT INTO wanshitong_feedback_details("
            "trace_id, reason_detail, comment_ciphertext, comment_nonce, "
            "encryption_key_id, feedback_revision, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(trace_id) DO UPDATE "
            "SET reason_detail=excluded.reason_detail, "
            "comment_ciphertext=excluded.comment_ciphertext, "
            "comment_nonce=excluded.comment_nonce, "
            "encryption_key_id=excluded.encryption_key_id, "
            "feedback_revision=excluded.feedback_revision, "
            "updated_at=excluded.updated_at",
            (
                canonical.trace_id,
                reason_detail,
                ciphertext,
                nonce,
                self._cipher.key_id if ciphertext is not None else None,
                revision,
                canonical.updated_at,
            ),
        )
        return revision

    def _stored_comment(self, row: sqlite3.Row | None) -> str | object | None:
        if row is None or row["comment_ciphertext"] is None:
            return None
        if row["encryption_key_id"] != self._cipher.key_id:
            return _UNREADABLE
        try:
            return self._cipher.decrypt(
                str(row["comment_ciphertext"]),
                str(row["comment_nonce"]),
                aad=_secret_aad(
                    str(row["trace_id"]),
                    "comment",
                    int(row["feedback_revision"]),
                ),
            )
        except (InvalidTag, ValueError):
            return _UNREADABLE

    def _encrypt_secret(
        self,
        trace_id: str,
        value: str | None,
        *,
        field_name: str,
        version: int,
    ) -> tuple[str | None, str | None]:
        if value is None:
            return None, None
        return self._cipher.encrypt(
            value,
            aad=_secret_aad(trace_id, field_name, version),
        )

    def _read_secret(  # noqa: PLR0913
        self,
        row: sqlite3.Row,
        *,
        ciphertext_name: str,
        nonce_name: str,
        key_id_name: str,
        field_name: str,
        version: int,
    ) -> tuple[str | None, bool, str | None]:
        ciphertext = row[ciphertext_name]
        if ciphertext is None:
            return None, True, None
        if row[key_id_name] != self._cipher.key_id:
            return None, False, "ENCRYPTION_KEY_UNAVAILABLE"
        try:
            return (
                self._cipher.decrypt(
                    str(ciphertext),
                    str(row[nonce_name]),
                    aad=_secret_aad(str(row["trace_id"]), field_name, version),
                ),
                True,
                None,
            )
        except (InvalidTag, ValueError):
            return None, False, "DECRYPTION_FAILED"

    def _feedback_row(
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
    ) -> sqlite3.Row:
        try:
            with self._connections.transaction() as connection:
                row = connection.execute(
                    _feedback_select()
                    + " WHERE f.trace_id=? AND f.project_id=? "
                    "AND f.knowledge_base_id=?",
                    (trace_id, project_id, knowledge_base_id),
                ).fetchone()
        except sqlite3.Error as error:
            raise _unavailable("wanshitong.feedback.read") from error
        if row is None:
            raise NotFound("反馈待办不存在。", stage="wanshitong.feedback.read")
        return cast(sqlite3.Row, row)

    def _list_item(
        self,
        row: sqlite3.Row,
        *,
        project_id: str,
        knowledge_base_id: str,
    ) -> dict[str, object]:
        history, _ = self._history_detail(
            str(row["trace_id"]),
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        question = None if history is None else history.get("question")
        projection = project_feedback_trace(history, None)
        feedback_revision = int(row["feedback_revision"] or 1)
        reviewed_revision = int(row["reviewed_feedback_revision"] or 0)
        return {
            "trace_id": row["trace_id"],
            "created_at": row["feedback_created_at"],
            "updated_at": row["feedback_updated_at"],
            "question_summary": (
                None if not isinstance(question, str) else question[:160]
            ),
            "question_sha256": row["question_sha256"],
            "final_status": row["history_status"],
            "useful": bool(row["useful"]),
            "reason_code": row["reason_code"],
            "reason_detail": row["reason_detail"],
            "comment_present": row["comment_ciphertext"] is not None,
            "feedback_revision": feedback_revision,
            "answer_path": projection["generation"]["answer_path"],
            "duration_ms": row["duration_ms"],
            "review_status": row["review_status"] or "NEW",
            "root_cause": row["root_cause"],
            "review_version": int(row["review_version"] or 0),
            "new_feedback_pending": feedback_revision > reviewed_revision,
        }

    def _history_detail(
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
    ) -> tuple[dict[str, object] | None, str | None]:
        try:
            return (
                self._history.detail(
                    trace_id,
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                ),
                None,
            )
        except NotFound:
            return None, "HISTORY_NOT_FOUND"
        except ProviderUnavailable:
            return None, "HISTORY_UNAVAILABLE"

    def _trace_detail(
        self, trace_id: str
    ) -> tuple[dict[str, object] | None, str | None]:
        try:
            return self._traces.legacy_detail(trace_id), None
        except TraceNotFoundError:
            return None, "TRACE_NOT_FOUND"
        except ProviderUnavailable:
            return None, "TRACE_UNAVAILABLE"

    def _validate_selected_source(
        self,
        trace_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
        document_id: str | None,
        version_id: str | None,
    ) -> None:
        if (document_id is None) != (version_id is None):
            raise ValueError("选中来源必须同时提供文档和版本 ID。")
        if document_id is None:
            return
        history, _ = self._history_detail(
            trace_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        allowed = {
            (item.get("document_id"), item.get("document_version_id"))
            for item in _history_citations(history)
        }
        if (document_id, version_id) not in allowed:
            raise ValueError("选中来源必须来自该次受权引用列表。")


def project_feedback_trace(
    history: Mapping[str, object] | None,
    trace: Mapping[str, object] | None,
) -> dict[str, dict[str, object]]:
    """将新旧 Trace 字段集中投影为管理页四段视图。"""
    sources = tuple(value for value in (history, trace) if value is not None)
    answer_path = _first_value(sources, "answer_path", "generation_mode")
    fallback = _first_value(sources, "fallback", "fallback_answer_available")
    return {
        "question_understanding": {
            "context_mode": _first_value(sources, "context_mode"),
            "resolved_root_hash": _first_value(sources, "resolved_root_hash"),
            "planner_called": _first_value(sources, "planner_called"),
            "atom_count": _first_value(sources, "atom_count"),
        },
        "retrieval_evidence": {
            "root_candidate_count": _first_value(
                sources, "root_candidate_count", "root_candidates"
            ),
            "atom_candidate_count": _first_value(
                sources, "atom_candidate_count", "atom_candidates"
            ),
            "generation_evidence_count": _generation_evidence_count(history),
            "exclusion_reasons": _first_value(
                sources, "exclusion_reasons", "rejection_reasons"
            ),
            "source_group_status": _first_value(
                sources, "source_group_status", "evidence_group_status"
            ),
        },
        "generation": {
            "answer_path": answer_path,
            "generation_called": _first_value(sources, "generation_called"),
            "generated_claim_count": _first_value(
                sources, "generated_claim_count", "claim_count"
            ),
            "fallback": fallback,
        },
        "validation_coverage": {
            "accepted_claim_count": _first_value(
                sources, "accepted_claim_count", "accepted_count"
            ),
            "published_claim_count": _first_value(
                sources, "published_claim_count", "published_count"
            ),
            "rejection_distribution": _first_value(
                sources, "rejection_distribution"
            ),
            "atom_coverage": _first_value(sources, "atom_coverage"),
            "terminal_reason": _first_value(
                sources, "terminal_reason", "reason_code"
            ),
        },
    }


def _feedback_select() -> str:
    return (
        "SELECT f.trace_id, f.useful, f.reason_code, f.projection_state, "
        "f.created_at AS feedback_created_at, "
        "f.updated_at AS feedback_updated_at, d.reason_detail, "
        "d.comment_ciphertext, d.comment_nonce, "
        "d.encryption_key_id AS detail_encryption_key_id, "
        "COALESCE(d.feedback_revision, 1) AS feedback_revision, "
        "q.question_sha256, q.status AS history_status, q.duration_ms, "
        "q.metadata_json, r.review_status, r.root_cause, "
        "r.note_ciphertext, r.note_nonce, "
        "r.encryption_key_id AS review_encryption_key_id, "
        "r.selected_source_document_id, r.selected_source_version_id, "
        "r.reviewed_feedback_revision, r.evaluation_candidate, "
        "r.fix_reference, r.verification_reference_json, "
        "r.review_version, r.reviewed_by_admin, "
        "r.updated_at AS review_updated_at "
        "FROM product_feedback f LEFT JOIN "
        "wanshitong_feedback_details d USING(trace_id) LEFT JOIN "
        "wanshitong_feedback_reviews r USING(trace_id) LEFT JOIN "
        "query_history q ON q.trace_id=f.trace_id OR "
        "q.trace_id=substr(f.trace_id, 7)"
    )


def _normalize_user_feedback(
    useful: bool,
    *,
    reason_code: FeedbackReason | None,
    reason_detail: FeedbackReasonDetail | None,
    comment: str | None,
) -> tuple[FeedbackReason | None, FeedbackReasonDetail | None, str | None]:
    normalized_comment = _optional_text(comment)
    if useful:
        if (
            reason_code is not None
            or reason_detail is not None
            or normalized_comment
        ):
            raise ValueError("有帮助的反馈不得携带负向原因或说明。")
        return None, None, None
    detail = reason_detail or cast(
        FeedbackReasonDetail,
        reason_code if reason_code in _REASON_DETAILS else "OTHER",
    )
    canonical = reason_code or _DETAIL_TO_CANONICAL[detail]
    if reason_detail is not None and _DETAIL_TO_CANONICAL[detail] != canonical:
        raise ValueError("reason_code 与 reason_detail 不一致。")
    return canonical, detail, normalized_comment


def _validate_review(
    status: FeedbackReviewStatus,
    *,
    root_cause: FeedbackRootCause | None,
    note: str | None,
    fix_reference: str | None,
    verification_references: Sequence[str],
) -> None:
    if status not in _REVIEW_STATUSES:
        raise ValueError("复核状态无效。")
    if root_cause is not None and root_cause not in _ROOT_CAUSES:
        raise ValueError("复核根因无效。")
    if status != "NEW" and root_cause is None:
        raise ValueError("非 NEW 复核必须选择根因。")
    if status == "REVIEWED" and root_cause == "OTHER" and note is None:
        raise ValueError("OTHER 根因必须填写 Note。")
    if status == "FIX_PLANNED" and fix_reference is None:
        raise ValueError("FIX_PLANNED 必须关联修复批次。")
    if status == "RESOLVED" and (
        fix_reference is None or not verification_references
    ):
        raise ValueError("RESOLVED 必须包含修复引用和验证记录。")
    if status == "EXPECTED_BEHAVIOR" and note is None:
        raise ValueError("EXPECTED_BEHAVIOR 必须记录判定依据。")


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _secret_aad(trace_id: str, field_name: str, version: int) -> SecretAad:
    return SecretAad(
        credential_id=trace_id,
        provider_type="wanshitong-feedback",
        field_name=field_name,
        key_version=version,
    )


def _json_string_list(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str)]


def _public_history(
    history: Mapping[str, object] | None, error: str | None
) -> dict[str, object]:
    if history is None:
        return {
            "available": False,
            "unavailable_reason": error,
            "question": None,
            "answer": None,
            "final_status": None,
            "duration_ms": None,
        }
    return {
        "available": True,
        "unavailable_reason": None,
        "question": history.get("question"),
        "answer": history.get("answer"),
        "final_status": history.get("status"),
        "duration_ms": history.get("duration_ms"),
    }


def _history_citations(
    history: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    if history is None:
        return []
    result = history.get("result")
    if not isinstance(result, Mapping):
        return []
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        return []
    citations: list[dict[str, object]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        document_id = item.get("document_id")
        version_id = item.get("document_version_id")
        citations.append(
            {
                "document_id": document_id,
                "document_version_id": version_id,
                "display_name": item.get("display_name"),
                "source_label": item.get("source_label"),
                "selected_source_eligible": isinstance(document_id, str)
                and isinstance(version_id, str),
            }
        )
    return citations


def _first_value(
    sources: Sequence[Mapping[str, object]], *keys: str
) -> object | None:
    for source in sources:
        for key in keys:
            found = _find_value(source, key)
            if found is not None:
                return found
    return None


def _find_value(value: object, key: str) -> object | None:
    if isinstance(value, Mapping):
        if key in value:
            return cast(object, value[key])
        for child in value.values():
            found = _find_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _find_value(child, key)
            if found is not None:
                return found
    return None


def _generation_evidence_count(
    history: Mapping[str, object] | None,
) -> int | None:
    if history is None:
        return None
    result = history.get("result")
    if not isinstance(result, Mapping):
        return None
    evidence = result.get("evidence")
    return len(evidence) if isinstance(evidence, list) else None


def _answer_path_from_metadata(value: object) -> object | None:
    if not isinstance(value, str):
        return None
    try:
        metadata = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, Mapping):
        return None
    return metadata.get("answer_path") or metadata.get("generation_mode")


def _latency_summary(values: Sequence[int]) -> dict[str, int | None]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
    }


def _percentile(values: Sequence[int], quantile: float) -> int | None:
    if not values:
        return None
    index = max(
        0,
        min(len(values) - 1, int((len(values) - 1) * quantile + 0.5)),
    )
    return values[index]


def _unavailable(stage: str) -> ProviderUnavailable:
    return ProviderUnavailable(
        "湾事通反馈服务暂时不可用。",
        stage=stage,
        code="FEEDBACK_STORE_UNAVAILABLE",
        retryable=True,
    )


__all__ = [
    "FeedbackReasonDetail",
    "FeedbackReviewStatus",
    "FeedbackRootCause",
    "WanshitongFeedback",
    "WanshitongFeedbackService",
    "project_feedback_trace",
]
