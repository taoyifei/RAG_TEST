"""在调用方 SQLite 事务内绑定并发布 Profile 与索引。"""

from __future__ import annotations

import json
from sqlite3 import Connection, Row
from typing import cast

from rag_app.core.errors import Conflict, RevisionStateError
from rag_app.core.models.management import QueuedIngestion


def bind_publication(
    connection: Connection, request: QueuedIngestion, now: str
) -> None:
    """在作业入队事务内冻结发布 CAS 前置。

    Args:
        connection: 已取得写锁的事务。
        request: 候选方案和文档快照。
        now: UTC 时间。

    Returns:
        无返回值。

    """
    if not request.activate_profile:
        return
    _assert_validations(connection, request.activation_validation_ids)
    kb_id = request.documents[0].document.knowledge_base_id
    current = _active_profile(connection, kb_id)
    kb = connection.execute(
        "SELECT active_revision_id FROM knowledge_bases WHERE "
        "knowledge_base_id=?",
        (kb_id,),
    ).fetchone()
    if (
        (None if current is None else current["profile_revision_id"])
        != request.expected_profile_revision_id
        or kb is None
        or kb[0] != request.expected_index_revision_id
    ):
        raise Conflict("发布快照已过期，请重新预览。", stage="profile.queue")
    existing = connection.execute(
        "SELECT request_json FROM ingestion_requests WHERE job_id=?",
        (request.job_id,),
    ).fetchone()
    if (
        existing is not None
        and QueuedIngestion.model_validate_json(existing[0]) != request
    ):
        raise Conflict("该索引构建已绑定其他草稿。", stage="profile.queue")
    candidate = connection.execute(
        "SELECT status FROM retrieval_profile_revisions WHERE "
        "profile_revision_id=? AND knowledge_base_id=?",
        (request.retrieval_profile_revision_id, kb_id),
    ).fetchone()
    if candidate is None or candidate[0] != "draft":
        raise Conflict("候选 Profile 已失效。", stage="profile.queue")
    connection.execute(
        "INSERT OR IGNORE INTO profile_publications VALUES (?, ?, ?, ?, ?, ?)",
        (
            request.retrieval_profile_revision_id,
            request.job_id,
            request.revision_id,
            request.expected_profile_revision_id,
            request.expected_index_revision_id,
            now,
        ),
    )
    connection.execute(
        "UPDATE retrieval_profile_revisions SET activation_job_id=? WHERE "
        "profile_revision_id=?",
        (request.job_id, request.retrieval_profile_revision_id),
    )


def activate_bound_profile(
    connection: Connection, revision_id: str, kb_id: str, now: str
) -> None:
    """在索引激活事务内同时验证并切换 Profile。

    Args:
        connection: 已通过 Writer Lease 和完整性验证的写事务。
        revision_id: 即将激活的索引。
        kb_id: 目标知识库。
        now: UTC 时间。

    Returns:
        无返回值；任意异常由调用方回滚整个事务。

    """
    # 旧阶段独立 Store 没有产品表，保持其离线生命周期合同。
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='profile_publications'"
        ).fetchone()
        is None
    ):
        return
    publication = connection.execute(
        "SELECT * FROM profile_publications WHERE revision_id=?",
        (revision_id,),
    ).fetchone()
    current = _active_profile(connection, kb_id)
    revision = connection.execute(
        "SELECT index_fingerprint FROM index_revisions WHERE "
        "index_revision_id=?",
        (revision_id,),
    ).fetchone()
    if publication is None:
        if (
            current is not None
            and current["index_semantic_fingerprint"] != revision[0]
        ):
            raise RevisionStateError(
                "旧 Writer 的 Profile 语义已失效。", stage="profile.activate"
            )
        return
    kb = connection.execute(
        "SELECT active_revision_id FROM knowledge_bases WHERE "
        "knowledge_base_id=?",
        (kb_id,),
    ).fetchone()
    if (
        None if current is None else current["profile_revision_id"]
    ) != publication["expected_profile_revision_id"] or kb[0] != publication[
        "expected_index_revision_id"
    ]:
        raise Conflict("其他发布已改变 Active 指针。", stage="profile.activate")
    candidate = connection.execute(
        "SELECT * FROM retrieval_profile_revisions WHERE profile_revision_id=?",
        (publication["profile_revision_id"],),
    ).fetchone()
    if (
        candidate["status"] != "draft"
        or candidate["index_semantic_fingerprint"] != revision[0]
    ):
        raise RevisionStateError(
            "候选方案和索引语义不兼容。", stage="profile.activate"
        )
    request_row = connection.execute(
        "SELECT r.request_json, j.cancel_requested FROM ingestion_requests r "
        "JOIN ingestion_jobs j ON j.job_id=r.job_id WHERE r.job_id=?",
        (publication["job_id"],),
    ).fetchone()
    if (
        request_row is None
        or request_row["cancel_requested"]
        or json.loads(request_row["request_json"])[
            "retrieval_profile_revision_id"
        ]
        != candidate["profile_revision_id"]
    ):
        raise Conflict(
            "候选发布已取消或请求绑定失效。", stage="profile.activate"
        )
    request = QueuedIngestion.model_validate_json(request_row["request_json"])
    _assert_validations(connection, request.activation_validation_ids)
    connection.execute(
        "UPDATE retrieval_profile_revisions SET status='retired' WHERE "
        "knowledge_base_id=? AND status='active'",
        (kb_id,),
    )
    connection.execute(
        "UPDATE retrieval_profile_revisions SET status='active', "
        "activated_at=? WHERE profile_revision_id=?",
        (now, candidate["profile_revision_id"]),
    )


def _active_profile(connection: Connection, kb_id: str) -> Row | None:
    return cast(
        Row | None,
        connection.execute(
            "SELECT * FROM retrieval_profile_revisions WHERE "
            "knowledge_base_id=? "
            "AND status='active'",
            (kb_id,),
        ).fetchone(),
    )


def _assert_validations(
    connection: Connection, validation_ids: tuple[str, ...]
) -> None:
    row = connection.execute(
        "SELECT count(*) FROM provider_validation_runs v "
        "JOIN provider_connections c ON c.connection_id=v.connection_id "
        "JOIN provider_credentials d ON d.credential_id=c.credential_id "
        "WHERE v.validation_id IN (SELECT value FROM json_each(?)) "
        "AND c.enabled=1 AND v.status='succeeded' "
        "AND v.configuration_version=c.configuration_version "
        "AND v.credential_key_version=d.key_version "
        "AND v.validation_mode IN ('live', 'mock') "
        "AND julianday(v.finished_at) BETWEEN julianday('now')-1 "
        "AND julianday('now')",
        (json.dumps(validation_ids),),
    ).fetchone()
    if not validation_ids or row[0] != len(set(validation_ids)):
        raise Conflict(
            "发布所需的 Provider 验证已失效。", stage="profile.activate"
        )
