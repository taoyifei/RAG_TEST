"""模型连接、验证记录与知识库检索方案的 SQLite 控制面。"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from sqlite3 import Row

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import Conflict, NotFound
from rag_app.core.identifiers import canonical_json, canonical_sha256
from rag_app.core.models.common import freeze_json_object
from rag_app.product.catalog import (
    require_provider,
    validate_model,
)
from rag_app.product.credential_store import CredentialStore
from rag_app.product.models import (
    ImpactKind,
    ImpactPreview,
    ProviderConnection,
    ProviderConnectionDraft,
    ProviderValidationRun,
    RetrievalProfileDraft,
    RetrievalProfileRevision,
)

_MAX_REQUEST_BUDGET = 20
_MAX_TOKEN_BUDGET = 1_000_000
_MAX_VALIDATION_PAGE = 200


class ProductControlStore:
    """提供不泄漏 Secret 的产品配置事务。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        credentials: CredentialStore,
    ) -> None:
        """保存共享产品 SQLite 和 Credential 读边界。

        Args:
            connections: 已完成 0011—0014 migration 的连接工厂。
            credentials: Provider Credential Store。

        Returns:
            无返回值。

        """
        self._connections = connections
        self._credentials = credentials

    def create_connection(
        self, draft: ProviderConnectionDraft
    ) -> ProviderConnection:
        """创建只允许目录端点的 Provider Connection。

        Args:
            draft: 已校验类型的非 Secret 连接输入。

        Returns:
            新 Provider Connection。

        Raises:
            ValueError: 目录、Credential 或预算不匹配。

        """
        display_name = draft.display_name
        provider_type = draft.provider_type
        credential_id = draft.credential_id
        endpoint_profile = draft.endpoint_profile
        workspace_id = draft.workspace_id
        region = draft.region
        request_budget = draft.request_budget
        token_budget = draft.token_budget
        provider = require_provider(provider_type)
        credential = self._credentials.get(credential_id)
        if credential.provider_type != provider_type:
            raise ValueError("Credential 与 Provider 类型不匹配。")
        if endpoint_profile not in provider.endpoint_profiles:
            raise ValueError("Endpoint Profile 不在内置目录中。")
        if provider_type == "aliyun-model-studio":
            if not workspace_id or region not in provider.regions:
                raise ValueError(
                    "阿里云百炼需要受支持的 Region 和 Workspace ID。"
                )
        elif workspace_id is not None or region is not None:
            raise ValueError("Jina 连接不能保存 Region 或 Workspace ID。")
        if not 1 <= request_budget <= _MAX_REQUEST_BUDGET or not (
            1 <= token_budget <= _MAX_TOKEN_BUDGET
        ):
            raise ValueError("Provider 请求或 Token 预算越界。")
        connection_id = _identifier("conn")
        now = _now()
        config = canonical_json(
            {
                "region": region,
                "request_budget": request_budget,
                "token_budget": token_budget,
                "workspace_id": workspace_id,
            }
        )
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO provider_connections("
                "connection_id, display_name, provider_type, credential_id, "
                "endpoint_profile, config_json, enabled, status, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 'configured', ?, ?)",
                (
                    connection_id,
                    display_name,
                    provider_type,
                    credential_id,
                    endpoint_profile,
                    config,
                    now,
                    now,
                ),
            )
        return self.get_connection(connection_id)

    def get_connection(self, connection_id: str) -> ProviderConnection:
        """读取 Provider Connection。

        Args:
            connection_id: 目标连接 ID。

        Returns:
            不含 Credential 值的连接。

        Raises:
            NotFound: 连接不存在。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM provider_connections WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
        if row is None:
            raise NotFound(
                "Provider Connection 不存在。", stage="connection.read"
            )
        return _connection(row)

    def list_connections(self) -> tuple[ProviderConnection, ...]:
        """列出 Provider Connections。

        Args:
            无参数；读取当前数据库。

        Returns:
            按创建时间排序的连接。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_connections "
                "ORDER BY created_at, connection_id"
            ).fetchall()
        return tuple(_connection(row) for row in rows)

    def record_validation(
        self,
        validation: ProviderValidationRun,
    ) -> ProviderValidationRun:
        """持久化安全验证摘要并更新连接状态。

        Args:
            validation: 不含原文、密钥和响应体的验证记录。

        Returns:
            原验证记录。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO provider_validation_runs("
                "validation_id, connection_id, catalog_version, operation, "
                "provider_model, credential_key_version, "
                "request_policy_identity, started_at, "
                "finished_at, status, http_category, dimension, "
                "estimated_tokens, observed_tokens, latency_ms, "
                "safe_error_code, synthetic_payload_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    validation.validation_id,
                    validation.connection_id,
                    validation.catalog_version,
                    validation.operation,
                    validation.provider_model,
                    validation.credential_key_version,
                    validation.request_policy_identity,
                    validation.started_at,
                    validation.finished_at,
                    validation.status,
                    validation.http_category,
                    validation.dimension,
                    validation.estimated_tokens,
                    validation.observed_tokens,
                    validation.latency_ms,
                    validation.safe_error_code,
                    validation.synthetic_payload_hash,
                ),
            )
            connection.execute(
                "UPDATE provider_connections SET last_validation_id=?, "
                "status=?, updated_at=? WHERE connection_id=?",
                (
                    validation.validation_id,
                    "validated"
                    if validation.status == "succeeded"
                    else "degraded",
                    validation.finished_at,
                    validation.connection_id,
                ),
            )
        return validation

    def list_validations(
        self,
        connection_id: str,
        *,
        limit: int = 50,
    ) -> tuple[ProviderValidationRun, ...]:
        """读取连接的最近验证记录。

        Args:
            connection_id: 目标 Provider Connection。
            limit: 有界返回数量。

        Returns:
            按完成时间倒序的安全记录。

        """
        self.get_connection(connection_id)
        if not 1 <= limit <= _MAX_VALIDATION_PAGE:
            raise ValueError("验证记录页大小必须在 1 到 200。")
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_validation_runs "
                "WHERE connection_id=? ORDER BY finished_at DESC LIMIT ?",
                (connection_id, limit),
            ).fetchall()
        return tuple(_validation(row) for row in rows)

    def create_profile(
        self, draft: RetrievalProfileDraft
    ) -> RetrievalProfileRevision:
        """创建不可变 Draft 并计算语义与 Serving 指纹。

        Args:
            draft: 已校验类型的完整 Profile Draft。

        Returns:
            新 Draft Profile Revision。

        """
        knowledge_base_id = draft.knowledge_base_id
        primary_connection_id = draft.primary_connection_id
        primary_embedding_model = draft.primary_embedding_model
        primary_dimension = draft.primary_dimension
        primary_document_policy = draft.primary_document_policy
        primary_query_policy = draft.primary_query_policy
        standby_connection_id = draft.standby_connection_id
        standby_embedding_model = draft.standby_embedding_model
        standby_dimension = draft.standby_dimension
        standby_document_policy = draft.standby_document_policy
        standby_query_policy = draft.standby_query_policy
        reranker_connection_id = draft.reranker_connection_id
        reranker_model = draft.reranker_model
        failover_enabled = draft.failover_enabled
        standby_budget = draft.standby_budget
        retrieval_policy = draft.retrieval_policy
        evidence_policy = draft.evidence_policy
        primary = self.get_connection(primary_connection_id)
        validate_model(
            primary.provider_type,
            primary_embedding_model,
            "embedding.document",
        )
        standby = self._optional_embedding_connection(
            standby_connection_id,
            standby_embedding_model,
        )
        reranker = self._optional_reranker_connection(
            reranker_connection_id,
            reranker_model,
        )
        if failover_enabled and standby is None:
            raise ValueError("启用 Failover 时必须配置备用 Embedding。")
        if primary_dimension <= 0 or (
            standby is not None and (standby_dimension or 0) <= 0
        ):
            raise ValueError("Embedding Dimension 必须为正数。")
        index_payload: dict[str, object] = {
            "chunker": "docx-structural-v3",
            "fts_analyzer": "deterministic-cjk-bigram-v2",
            "primary": {
                "dimension": primary_dimension,
                "document_policy": primary_document_policy,
                "model": primary_embedding_model,
                "provider": primary.provider_type,
                "query_policy": primary_query_policy,
            },
            "standby": None
            if standby is None
            else {
                "dimension": standby_dimension,
                "document_policy": standby_document_policy,
                "model": standby_embedding_model,
                "provider": standby.provider_type,
                "query_policy": standby_query_policy,
            },
            "topology": "hot_standby" if standby is not None else "single",
        }
        index_fingerprint = canonical_sha256(index_payload)
        serving_fingerprint = canonical_sha256(
            {
                "base_index": index_fingerprint,
                "evidence_policy": evidence_policy,
                "reranker": None
                if reranker is None
                else {
                    "model": reranker_model,
                    "provider": reranker.provider_type,
                },
                "retrieval_policy": retrieval_policy,
                "standby_budget": standby_budget,
            }
        )
        profile_id = _identifier("pfr")
        now = _now()
        values = (
            profile_id,
            knowledge_base_id,
            primary_connection_id,
            primary_embedding_model,
            primary_dimension,
            canonical_json(primary_document_policy),
            canonical_json(primary_query_policy),
            standby_connection_id,
            standby_embedding_model,
            standby_dimension,
            canonical_json(standby_document_policy),
            canonical_json(standby_query_policy),
            reranker_connection_id,
            reranker_model,
            int(failover_enabled),
            canonical_json(standby_budget),
            canonical_json(retrieval_policy),
            canonical_json(evidence_policy),
            index_fingerprint,
            serving_fingerprint,
            now,
        )
        with self._connections.transaction(write=True) as connection:
            knowledge_base = connection.execute(
                "SELECT 1 FROM knowledge_bases WHERE knowledge_base_id=? "
                "AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
            if knowledge_base is None:
                raise NotFound("知识库不存在。", stage="profile.create")
            connection.execute(
                "INSERT INTO retrieval_profile_revisions("
                "profile_revision_id, knowledge_base_id, status, "
                "primary_connection_id, primary_embedding_model, "
                "primary_dimension, primary_document_policy_json, "
                "primary_query_policy_json, standby_connection_id, "
                "standby_embedding_model, standby_dimension, "
                "standby_document_policy_json, standby_query_policy_json, "
                "reranker_connection_id, reranker_model, failover_enabled, "
                "standby_budget_json, retrieval_policy_json, "
                "evidence_policy_json, index_semantic_fingerprint, "
                "serving_fingerprint, created_at) "
                "VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?) ",
                values,
            )
        return self.get_profile(profile_id)

    def get_profile(self, profile_revision_id: str) -> RetrievalProfileRevision:
        """读取一个 Retrieval Profile Revision。

        Args:
            profile_revision_id: 目标 Profile Revision ID。

        Returns:
            不可变 Profile Revision。

        Raises:
            NotFound: Profile 不存在。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_profile_revisions "
                "WHERE profile_revision_id=?",
                (profile_revision_id,),
            ).fetchone()
        if row is None:
            raise NotFound("Retrieval Profile 不存在。", stage="profile.read")
        return _profile(row)

    def active_profile(
        self, knowledge_base_id: str
    ) -> RetrievalProfileRevision | None:
        """读取知识库当前 Active Profile。

        Args:
            knowledge_base_id: 目标知识库。

        Returns:
            Active Profile；尚未配置时为 None。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_profile_revisions "
                "WHERE knowledge_base_id=? AND status='active'",
                (knowledge_base_id,),
            ).fetchone()
        return None if row is None else _profile(row)

    def list_profiles(
        self, knowledge_base_id: str
    ) -> tuple[RetrievalProfileRevision, ...]:
        """列出知识库全部 Profile Revision。

        Args:
            knowledge_base_id: 目标知识库。

        Returns:
            按创建时间倒序的 Profile。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM retrieval_profile_revisions "
                "WHERE knowledge_base_id=? ORDER BY created_at DESC",
                (knowledge_base_id,),
            ).fetchall()
        return tuple(_profile(row) for row in rows)

    def preview_impact(self, profile_revision_id: str) -> ImpactPreview:
        """比较 Draft 与当前 Active 指纹。

        Args:
            profile_revision_id: 待应用 Profile Revision。

        Returns:
            三态影响预览。

        """
        proposed = self.get_profile(profile_revision_id)
        current = self.active_profile(proposed.knowledge_base_id)
        index_changed = (
            current is None
            or current.index_semantic_fingerprint
            != proposed.index_semantic_fingerprint
        )
        serving_changed = (
            current is None
            or current.serving_fingerprint != proposed.serving_fingerprint
        )
        if index_changed:
            impact = ImpactKind.NEW_INDEX_REVISION_REQUIRED
        elif serving_changed:
            impact = ImpactKind.SERVING_RELOAD
        else:
            impact = ImpactKind.NO_REINDEX
        return ImpactPreview(
            impact=impact,
            current_profile_revision_id=(
                None if current is None else current.profile_revision_id
            ),
            proposed_profile_revision_id=profile_revision_id,
            index_fingerprint_changed=index_changed,
            serving_fingerprint_changed=serving_changed,
        )

    def activate_profile(
        self,
        profile_revision_id: str,
        *,
        confirmed_impact: ImpactKind,
    ) -> RetrievalProfileRevision:
        """确认影响后原子切换 Active Profile。

        Args:
            profile_revision_id: 待激活 Draft ID。
            confirmed_impact: 用户确认的影响类别。

        Returns:
            新 Active Profile。

        Raises:
            Conflict: 预览已变化或 Profile 不是 Draft。

        """
        proposed = self.get_profile(profile_revision_id)
        preview = self.preview_impact(profile_revision_id)
        if preview.impact is not confirmed_impact:
            raise Conflict("Profile 影响预览已变化。", stage="profile.activate")
        if proposed.status != "draft":
            raise Conflict(
                "只有 Draft Profile 可以激活。", stage="profile.activate"
            )
        missing = self.profile_validation_issues(profile_revision_id)
        if missing:
            raise Conflict(
                "Profile 引用的连接尚未完成全部验证。",
                stage="profile.activate",
                details={"missing_validations": list(missing)},
            )
        now = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE retrieval_profile_revisions SET status='retired' "
                "WHERE knowledge_base_id=? AND status='active'",
                (proposed.knowledge_base_id,),
            )
            connection.execute(
                "UPDATE retrieval_profile_revisions SET status='active', "
                "activated_at=? WHERE profile_revision_id=? AND status='draft'",
                (now, profile_revision_id),
            )
        return self.get_profile(profile_revision_id)

    def profile_validation_issues(
        self, profile_revision_id: str
    ) -> tuple[str, ...]:
        """列出 Profile 激活前缺失或失败的连接验证。

        Args:
            profile_revision_id: 待激活 Profile Revision。

        Returns:
            稳定排序的 connection:operation 标识；空元组表示可激活。

        """
        profile = self.get_profile(profile_revision_id)
        required = [
            (profile.primary_connection_id, "embedding.document"),
            (profile.primary_connection_id, "embedding.query"),
        ]
        if profile.standby_connection_id is not None:
            required.extend(
                (
                    (profile.standby_connection_id, "embedding.document"),
                    (profile.standby_connection_id, "embedding.query"),
                )
            )
        if profile.reranker_connection_id is not None:
            required.append((profile.reranker_connection_id, "reranking"))
        return tuple(
            f"{connection_id}:{operation}"
            for connection_id, operation in required
            if self.latest_status(connection_id, operation) != "succeeded"
        )

    def latest_status(self, connection_id: str, operation: str) -> str:
        """返回指定操作最近一次持久验证状态。

        Args:
            connection_id: Provider Connection ID。
            operation: 目录中的操作 ID。

        Returns:
            succeeded、failed 或 not_verified。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT v.status FROM provider_validation_runs v "
                "JOIN provider_connections c "
                "ON c.connection_id=v.connection_id "
                "JOIN provider_credentials d "
                "ON d.credential_id=c.credential_id "
                "WHERE v.connection_id=? AND v.operation=? "
                "AND v.credential_key_version=d.key_version "
                "ORDER BY v.finished_at DESC LIMIT 1",
                (connection_id, operation),
            ).fetchone()
        return "not_verified" if row is None else str(row["status"])

    def system_evidence(self) -> dict[str, object]:
        """汇总 Active Profile 与最新 Provider 验证证据。

        Args:
            无参数；读取产品控制面。

        Returns:
            不含 Secret 的动态系统状态输入。

        """
        with self._connections.transaction() as connection:
            active = connection.execute(
                "SELECT count(*) AS count FROM retrieval_profile_revisions "
                "WHERE status='active'"
            ).fetchone()
            mismatches = connection.execute(
                "SELECT count(*) AS count FROM retrieval_profile_revisions p "
                "JOIN knowledge_bases k "
                "ON k.knowledge_base_id=p.knowledge_base_id "
                "LEFT JOIN index_revisions r "
                "ON r.index_revision_id=k.active_revision_id "
                "WHERE p.status='active' AND (r.index_revision_id IS NULL "
                "OR r.index_fingerprint<>p.index_semantic_fingerprint)"
            ).fetchone()
            rows = connection.execute(
                "SELECT provider_type, operation, status, http_category FROM ("
                "SELECT c.provider_type, v.operation, v.status, "
                "v.http_category, ROW_NUMBER() OVER ("
                "PARTITION BY c.provider_type, v.operation "
                "ORDER BY v.finished_at DESC, v.validation_id DESC"
                ") AS position FROM provider_connections c "
                "JOIN provider_validation_runs v "
                "ON v.connection_id=c.connection_id "
                "JOIN provider_credentials d "
                "ON d.credential_id=c.credential_id "
                "WHERE c.enabled=1 "
                "AND v.credential_key_version=d.key_version) "
                "WHERE position=1"
            ).fetchall()
        statuses = {
            f"{row['provider_type']}:{row['operation']}": {
                "http_category": str(row["http_category"]),
                "status": str(row["status"]),
            }
            for row in rows
        }
        return {
            "active_profile_count": int(active["count"]) if active else 0,
            "provider_validation_statuses": statuses,
            "reindex_required": bool(mismatches and mismatches["count"]),
        }

    def _optional_embedding_connection(
        self,
        connection_id: str | None,
        model: str | None,
    ) -> ProviderConnection | None:
        if connection_id is None and model is None:
            return None
        if connection_id is None or model is None:
            raise ValueError("备用连接和模型必须同时提供。")
        connection = self.get_connection(connection_id)
        validate_model(connection.provider_type, model, "embedding.document")
        return connection

    def _optional_reranker_connection(
        self,
        connection_id: str | None,
        model: str | None,
    ) -> ProviderConnection | None:
        if connection_id is None and model is None:
            return None
        if connection_id is None or model is None:
            raise ValueError("重排连接和模型必须同时提供。")
        connection = self.get_connection(connection_id)
        validate_model(connection.provider_type, model, "reranking")
        return connection


def _connection(row: Row) -> ProviderConnection:
    config = json.loads(str(row["config_json"]))
    return ProviderConnection(
        connection_id=str(row["connection_id"]),
        display_name=str(row["display_name"]),
        provider_type=str(row["provider_type"]),
        credential_id=str(row["credential_id"]),
        endpoint_profile=str(row["endpoint_profile"]),
        enabled=bool(row["enabled"]),
        status=str(row["status"]),
        last_validation_id=(
            None
            if row["last_validation_id"] is None
            else str(row["last_validation_id"])
        ),
        workspace_id=config.get("workspace_id"),
        region=config.get("region"),
        request_budget=int(config["request_budget"]),
        token_budget=int(config["token_budget"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _validation(row: Row) -> ProviderValidationRun:
    return ProviderValidationRun(
        validation_id=str(row["validation_id"]),
        connection_id=str(row["connection_id"]),
        catalog_version=str(row["catalog_version"]),
        operation=str(row["operation"]),
        provider_model=str(row["provider_model"]),
        credential_key_version=int(row["credential_key_version"]),
        request_policy_identity=str(row["request_policy_identity"]),
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]),
        status=str(row["status"]),
        http_category=str(row["http_category"]),
        dimension=None if row["dimension"] is None else int(row["dimension"]),
        estimated_tokens=int(row["estimated_tokens"]),
        observed_tokens=(
            None
            if row["observed_tokens"] is None
            else int(row["observed_tokens"])
        ),
        latency_ms=int(row["latency_ms"]),
        safe_error_code=(
            None
            if row["safe_error_code"] is None
            else str(row["safe_error_code"])
        ),
        synthetic_payload_hash=str(row["synthetic_payload_hash"]),
    )


def _profile(row: Row) -> RetrievalProfileRevision:
    return RetrievalProfileRevision(
        profile_revision_id=str(row["profile_revision_id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        status=str(row["status"]),
        primary_connection_id=str(row["primary_connection_id"]),
        primary_embedding_model=str(row["primary_embedding_model"]),
        primary_dimension=int(row["primary_dimension"]),
        primary_document_policy=freeze_json_object(
            json.loads(str(row["primary_document_policy_json"]))
        ),
        primary_query_policy=freeze_json_object(
            json.loads(str(row["primary_query_policy_json"]))
        ),
        standby_connection_id=(
            None
            if row["standby_connection_id"] is None
            else str(row["standby_connection_id"])
        ),
        standby_embedding_model=(
            None
            if row["standby_embedding_model"] is None
            else str(row["standby_embedding_model"])
        ),
        standby_dimension=(
            None
            if row["standby_dimension"] is None
            else int(row["standby_dimension"])
        ),
        standby_document_policy=freeze_json_object(
            json.loads(str(row["standby_document_policy_json"]))
        ),
        standby_query_policy=freeze_json_object(
            json.loads(str(row["standby_query_policy_json"]))
        ),
        reranker_connection_id=(
            None
            if row["reranker_connection_id"] is None
            else str(row["reranker_connection_id"])
        ),
        reranker_model=(
            None
            if row["reranker_model"] is None
            else str(row["reranker_model"])
        ),
        failover_enabled=bool(row["failover_enabled"]),
        standby_budget=freeze_json_object(
            json.loads(str(row["standby_budget_json"]))
        ),
        retrieval_policy=freeze_json_object(
            json.loads(str(row["retrieval_policy_json"]))
        ),
        evidence_policy=freeze_json_object(
            json.loads(str(row["evidence_policy_json"]))
        ),
        index_semantic_fingerprint=str(row["index_semantic_fingerprint"]),
        serving_fingerprint=str(row["serving_fingerprint"]),
        created_at=str(row["created_at"]),
        activated_at=(
            None if row["activated_at"] is None else str(row["activated_at"])
        ),
    )


def _identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["ProductControlStore"]
