"""F06 已审核公共题目录、可撤销的同义映射和实时公开过滤。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.core.errors import Conflict, NotFound
from rag_app.wanshitong.admin_models import (
    RecommendationCreateRequest,
    RecommendationUpdateRequest,
)

_FRESHNESS = timedelta(hours=24)
_MIN_USERS = 3
_MIN_REQUESTS = 5
_MAX_PUBLIC = 5
_LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_group_key(recommendation_id: str) -> str:
    """将人工题目录 ID 映射成 F05 可存储的稳定组键。"""
    return hashlib.sha256(
        ("wst-approved-alias:" + recommendation_id).encode("utf-8")
    ).hexdigest()


def approved_alias_snapshot(
    connection: sqlite3.Connection,
    deployment_id: str,
    *,
    project_id: str | None = None,
    knowledge_base_id: str | None = None,
) -> tuple[str, dict[str, str]]:
    """计算全部署批准映射摘要，同时返回指定 Scope 的逐请求映射。"""
    rows = connection.execute(
        "SELECT recommendation_id, project_id, knowledge_base_id, "
        "version, alias_keys_json FROM question_recommendation_catalog "
        "WHERE deployment_id=? AND state='APPROVED' "
        "ORDER BY project_id, knowledge_base_id, recommendation_id",
        (deployment_id,),
    ).fetchall()
    if not rows:
        return "none", {}
    entries: list[tuple[str, str, str, str, int]] = []
    mapping: dict[str, str] = {}
    for row in rows:
        alias_keys = json.loads(str(row["alias_keys_json"]))
        for key in sorted(alias_keys):
            entries.append(
                (
                    str(row["project_id"]),
                    str(row["knowledge_base_id"]),
                    key,
                    str(row["recommendation_id"]),
                    int(row["version"]),
                )
            )
            if (
                row["project_id"] == project_id
                and row["knowledge_base_id"] == knowledge_base_id
            ):
                mapping[key] = canonical_group_key(
                    str(row["recommendation_id"])
                )
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), mapping


class QuestionRecommendationService:
    """固定部署中的审核目录；公共查询只读通过审核和新鲜度门禁的题面。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        *,
        deployment_id: str,
        public_enabled: bool = True,
    ) -> None:
        self._connections = connections
        self.deployment_id = deployment_id
        self.public_enabled = public_enabled

    def list_admin(
        self, *, project_id: str, knowledge_base_id: str
    ) -> dict[str, object]:
        """管理员可查看 DRAFT、失效题及当前映射状态。"""
        scope = (self.deployment_id, project_id, knowledge_base_id)
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM question_recommendation_catalog "
                "WHERE deployment_id=? AND project_id=? "
                "AND knowledge_base_id=? "
                "ORDER BY updated_at DESC, recommendation_id LIMIT 200",
                scope,
            ).fetchall()
            revision, _ = approved_alias_snapshot(
                connection,
                self.deployment_id,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
            items = [self._view(connection, row) for row in rows]
        return {"items": items, "alias_revision": revision}

    def create(
        self,
        *,
        project_id: str,
        knowledge_base_id: str,
        body: RecommendationCreateRequest,
    ) -> dict[str, object]:
        """从仍可用的 F05 精确题组创建待审核候选。"""
        scope = (self.deployment_id, project_id, knowledge_base_id)
        now = _now()
        recommendation_id = "pq_" + uuid.uuid4().hex
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT i.group_kind FROM question_stats_items i "
                "WHERE i.run_id=(SELECT r.run_id FROM question_stats_runs r "
                "WHERE r.deployment_id=? AND r.project_id=? "
                "AND r.knowledge_base_id=? AND r.state='COMPLETE' "
                "AND r.expires_at>? ORDER BY r.created_at DESC LIMIT 1) "
                "AND i.group_key=?",
                (*scope, now, body.source_group_key),
            ).fetchone()
            if row is None or row["group_kind"] != "EXACT":
                raise Conflict(
                    "候选题组已不可用，请刷新问题榜。",
                    stage="wanshitong.recommendation.create",
                )
            connection.execute(
                "INSERT INTO question_recommendation_catalog ("
                "recommendation_id, deployment_id, project_id, "
                "knowledge_base_id, question_text, question_style, topic_key, "
                "state, version, alias_keys_json, "
                "validated_source_versions_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'DRAFT', 1, ?, '[]', ?, ?)",
                (
                    recommendation_id,
                    *scope,
                    body.question_text.strip(),
                    body.question_style,
                    body.topic_key.strip(),
                    json.dumps([body.source_group_key]),
                    now,
                    now,
                ),
            )
            created = self._get(connection, recommendation_id, scope)
            return self._view(connection, created)

    def update(
        self,
        recommendation_id: str,
        *,
        project_id: str,
        knowledge_base_id: str,
        body: RecommendationUpdateRequest,
        approved_by: str,
    ) -> dict[str, object]:
        """同一写事务中完成版本检查、审核、alias 排他和状态切换。"""
        scope = (self.deployment_id, project_id, knowledge_base_id)
        aliases = list(dict.fromkeys(body.alias_keys))
        source_versions = [
            source.model_dump() for source in body.validated_sources
        ]
        now = _now()
        with self._connections.transaction(write=True) as connection:
            current = self._get(connection, recommendation_id, scope)
            same = (
                current["question_text"] == body.question_text.strip()
                and current["question_style"] == body.question_style
                and current["topic_key"] == body.topic_key.strip()
                and current["state"] == body.state
                and json.loads(str(current["alias_keys_json"])) == aliases
                and json.loads(str(current["validated_source_versions_json"]))
                == source_versions
                and current["disabled_reason"] == body.disabled_reason
            )
            if int(current["version"]) != body.expected_version:
                if (
                    int(current["version"]) == body.expected_version + 1
                    and same
                ):
                    return self._view(connection, current)
                raise Conflict(
                    "公共题已被其他管理员修改，请刷新后重试。",
                    stage="wanshitong.recommendation.update",
                    details={"current_version": current["version"]},
                )
            if same:
                return self._view(connection, current)
            question_changed = (
                current["question_text"] != body.question_text.strip()
            )
            if (
                current["state"] == "APPROVED"
                and question_changed
                and body.state == "APPROVED"
            ):
                raise Conflict(
                    "题面改动须先保存待审核，再重新确认等义问题。",
                    stage="wanshitong.recommendation.review",
                )
            if body.state == "APPROVED":
                if (
                    not body.review_confirmed
                    or not aliases
                    or not source_versions
                ):
                    raise ValueError("审核须确认题面、等义键和已核对资料版本。")
                if not self._sources_current(
                    connection, scope, source_versions
                ):
                    raise Conflict(
                        "已核对资料不在当前有效索引中，请重新核查。",
                        stage="wanshitong.recommendation.source",
                    )
                self._check_alias_evidence(connection, scope, current, aliases)
                self._check_alias_conflicts(
                    connection, scope, recommendation_id, aliases
                )
            checked_at = now if body.state == "APPROVED" else None
            approved_at = now if body.state == "APPROVED" else None
            approver = approved_by if body.state == "APPROVED" else None
            connection.execute(
                "UPDATE question_recommendation_catalog SET "
                "question_text=?, question_style=?, topic_key=?, state=?, "
                "version=version+1, alias_keys_json=?, "
                "validated_source_versions_json=?, checked_at=?, "
                "approved_at=?, approved_by=?, disabled_reason=?, updated_at=? "
                "WHERE recommendation_id=?",
                (
                    body.question_text.strip(),
                    body.question_style,
                    body.topic_key.strip(),
                    body.state,
                    json.dumps(aliases),
                    json.dumps(source_versions),
                    checked_at,
                    approved_at,
                    approver,
                    body.disabled_reason,
                    now,
                    recommendation_id,
                ),
            )
            return self._view(
                connection, self._get(connection, recommendation_id, scope)
            )

    @staticmethod
    def _check_alias_evidence(
        connection: sqlite3.Connection,
        scope: tuple[str, str, str],
        current: sqlite3.Row,
        aliases: list[str],
    ) -> None:
        """新关联只接受当前完整 F05 榜上的精确问题键。"""
        existing = set(json.loads(str(current["alias_keys_json"])))
        new_keys = set(aliases) - existing
        if not new_keys:
            return
        run = connection.execute(
            "SELECT run_id FROM question_stats_runs WHERE deployment_id=? "
            "AND project_id=? AND knowledge_base_id=? "
            "AND state='COMPLETE' AND expires_at>? "
            "ORDER BY created_at DESC LIMIT 1",
            (*scope, _now()),
        ).fetchone()
        if run is None:
            raise Conflict(
                "缺少可核对的问题统计，请先刷新榜单。",
                stage="wanshitong.recommendation.alias",
            )
        known = {
            str(row["group_key"])
            for row in connection.execute(
                "SELECT group_key FROM question_stats_items WHERE run_id=? "
                "AND group_kind='EXACT'",
                (run["run_id"],),
            )
        }
        if not new_keys.issubset(known):
            raise Conflict(
                "等义问题键不在当前精确题组中。",
                stage="wanshitong.recommendation.alias",
            )

    @staticmethod
    def _check_alias_conflicts(
        connection: sqlite3.Connection,
        scope: tuple[str, str, str],
        recommendation_id: str,
        aliases: list[str],
    ) -> None:
        rows = connection.execute(
            "SELECT recommendation_id, alias_keys_json "
            "FROM question_recommendation_catalog WHERE deployment_id=? "
            "AND project_id=? AND knowledge_base_id=? AND state='APPROVED' "
            "AND recommendation_id!=?",
            (*scope, recommendation_id),
        ).fetchall()
        for row in rows:
            if set(aliases).intersection(
                json.loads(str(row["alias_keys_json"]))
            ):
                raise Conflict(
                    "问题键已关联另一条有效公共题。",
                    stage="wanshitong.recommendation.alias",
                )

    @staticmethod
    def _sources_current(
        connection: sqlite3.Connection,
        scope: tuple[str, str, str],
        sources: list[dict[str, str]],
    ) -> bool:
        for source in sources:
            row = connection.execute(
                "SELECT 1 FROM documents d JOIN knowledge_bases kb "
                "ON kb.knowledge_base_id=d.knowledge_base_id "
                "JOIN revision_documents rd "
                "ON rd.revision_id=kb.active_revision_id "
                "AND rd.document_id=d.document_id "
                "AND rd.document_version_id=d.current_version_id "
                "WHERE d.document_id=? AND d.current_version_id=? "
                "AND d.project_id=? AND d.knowledge_base_id=? "
                "AND d.status='active' AND d.deleted_at IS NULL "
                "AND kb.deleted_at IS NULL",
                (
                    source["document_id"],
                    source["version_id"],
                    scope[1],
                    scope[2],
                ),
            ).fetchone()
            if row is None:
                return False
        return bool(sources)

    def public_questions(  # noqa: PLR0912
        self, *, project_id: str, knowledge_base_id: str
    ) -> dict[str, object]:
        """每次读取当前状态、资料版本和完整统计；无效时交给 F01 静态题库。"""
        empty: dict[str, object] = {
            "mode": "EMPTY",
            "generated_at": None,
            "window_days": 7,
            "items": [],
        }
        if not self.public_enabled:
            return empty
        scope = (self.deployment_id, project_id, knowledge_base_id)
        now = datetime.now(UTC)
        stale: list[tuple[str, int]] = []
        with self._read_and_mark_stale(stale) as connection:
            catalog = connection.execute(
                "SELECT * FROM question_recommendation_catalog "
                "WHERE deployment_id=? AND project_id=? "
                "AND knowledge_base_id=? "
                "AND state='APPROVED'",
                scope,
            ).fetchall()
            approved: list[sqlite3.Row] = []
            for row in catalog:
                sources = json.loads(str(row["validated_source_versions_json"]))
                if self._sources_current(connection, scope, sources):
                    approved.append(row)
                else:
                    stale.append(
                        (str(row["recommendation_id"]), int(row["version"]))
                    )
            revision, _ = approved_alias_snapshot(
                connection,
                self.deployment_id,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
            run = connection.execute(
                "SELECT * FROM question_stats_runs WHERE deployment_id=? "
                "AND project_id=? AND knowledge_base_id=? "
                "AND state='COMPLETE' AND alias_revision=? "
                "AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                (*scope, revision, now.isoformat()),
            ).fetchone()
            if run is None or run["finished_at"] is None:
                return empty
            finished_at = datetime.fromisoformat(str(run["finished_at"]))
            if now - finished_at > _FRESHNESS:
                return empty
            candidates: list[tuple[sqlite3.Row, sqlite3.Row]] = []
            for row in approved:
                item = connection.execute(
                    "SELECT * FROM question_stats_items WHERE run_id=? "
                    "AND group_key=? AND group_kind='APPROVED_ALIAS'",
                    (
                        run["run_id"],
                        canonical_group_key(str(row["recommendation_id"])),
                    ),
                ).fetchone()
                if (
                    item is not None
                    and item["manual_distinct_users"] >= _MIN_USERS
                    and item["request_count"] >= _MIN_REQUESTS
                    and item["confirmed_open_issue_count"] == 0
                ):
                    candidates.append((row, item))
            candidates.sort(key=lambda pair: str(pair[0]["recommendation_id"]))
            candidates.sort(
                key=lambda pair: (
                    int(pair[1]["manual_distinct_users"]),
                    int(pair[1]["manual_user_day_heat"]),
                    int(pair[1]["request_count"]),
                    str(pair[1]["last_seen_at"]),
                ),
                reverse=True,
            )
            diverse: list[tuple[sqlite3.Row, sqlite3.Row]] = []
            seen_topics: set[str] = set()
            for pair in candidates:
                topic = str(pair[0]["topic_key"])
                if topic not in seen_topics:
                    diverse.append(pair)
                    seen_topics.add(topic)
            for pair in candidates:
                if len(diverse) >= _MAX_PUBLIC:
                    break
                if pair not in diverse:
                    diverse.append(pair)
            items = [
                {
                    "id": str(row["recommendation_id"]),
                    "question": str(row["question_text"]),
                    "topic_key": str(row["topic_key"]),
                }
                for row, _ in diverse[:_MAX_PUBLIC]
            ]
        if not items:
            return empty
        return {
            "mode": "POPULAR",
            "generated_at": str(run["finished_at"]),
            "window_days": 7,
            "items": items,
        }

    @contextmanager
    def _read_and_mark_stale(
        self, stale: list[tuple[str, int]]
    ) -> Iterator[sqlite3.Connection]:
        """正常公共 GET 只读；来源失效时在读事务结束后标记复核。"""
        try:
            with self._connections.transaction() as connection:
                yield connection
        finally:
            if stale:
                try:
                    with self._connections.transaction(
                        write=True
                    ) as connection:
                        connection.executemany(
                            "UPDATE question_recommendation_catalog SET "
                            "state='NEEDS_REVIEW', version=version+1, "
                            "approved_at=NULL, approved_by=NULL, updated_at=? "
                            "WHERE recommendation_id=? AND version=? "
                            "AND state='APPROVED'",
                            (
                                (_now(), recommendation_id, version)
                                for recommendation_id, version in stale
                            ),
                        )
                except sqlite3.Error:
                    _LOGGER.warning("QUESTION_RECOMMENDATION_STALE_MARK_FAILED")

    @staticmethod
    def _get(
        connection: sqlite3.Connection,
        recommendation_id: str,
        scope: tuple[str, str, str],
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM question_recommendation_catalog "
            "WHERE recommendation_id=? AND deployment_id=? "
            "AND project_id=? AND knowledge_base_id=?",
            (recommendation_id, *scope),
        ).fetchone()
        if row is None:
            raise NotFound(
                "公共题不存在。", stage="wanshitong.recommendation.read"
            )
        return cast(sqlite3.Row, row)

    def _view(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, object]:
        value = dict(row)
        value["alias_keys"] = json.loads(str(value.pop("alias_keys_json")))
        sources = json.loads(str(value.pop("validated_source_versions_json")))
        value["validated_sources"] = sources
        value["source_current"] = self._sources_current(
            connection,
            (
                str(row["deployment_id"]),
                str(row["project_id"]),
                str(row["knowledge_base_id"]),
            ),
            sources,
        )
        return value


__all__ = [
    "QuestionRecommendationService",
    "approved_alias_snapshot",
    "canonical_group_key",
]
