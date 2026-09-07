"""模型连接、验证记录与知识库检索方案的 SQLite 控制面。"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from sqlite3 import Row

from rag_app.adapters.providers.aliyun_endpoint import (
    AliyunEndpointConfig,
    resolve_endpoint,
)
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import Conflict, NotFound, PolicyDenied
from rag_app.core.identifiers import canonical_json, canonical_sha256
from rag_app.core.models import ProviderCall
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
    ProviderUsageDaily,
    ProviderValidationRun,
    RetrievalProfileDraft,
    RetrievalProfileRevision,
)
from rag_app.product.quality import ProductQualityStore
from rag_app.product.resolved_profile import (
    resolve_embedding,
    resolve_retrieval_policy,
)
from rag_app.product.verification import profile_specs, validation_is_current

_MAX_DISPLAY_NAME = 200
_MAX_VALIDATION_PAGE = 200
_MAX_USAGE_PAGE = 1000
_P11_DIMENSION = 1024


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
        self.index_contract: dict[str, object] = {}
        self.queue_profile: (
            Callable[[RetrievalProfileRevision, str | None, str | None], None]
            | None
        ) = None
        self.quality = ProductQualityStore(connections, self)

    def credential_version(self, credential_id: str) -> int:
        """只读取凭据版本以使缓存与验证失效。

        Args:
            credential_id: 已保存凭据引用。

        Returns:
            不含 Secret 的版本号。

        """
        return self._credentials.get(credential_id).key_version

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
        draft = validate_connection_metadata(draft)
        credential = self._credentials.get(draft.credential_id)
        if credential.provider_type != draft.provider_type:
            raise ValueError("Credential 与 Provider 类型不匹配。")
        display_name = draft.display_name
        provider_type = draft.provider_type
        credential_id = draft.credential_id
        endpoint_profile = draft.endpoint_profile
        workspace_id = draft.workspace_id
        region = draft.region
        request_budget = draft.request_budget
        token_budget = draft.token_budget
        connection_id = _identifier("conn")
        now = _now()
        config = canonical_json(
            {
                "endpoint_mode": draft.endpoint_mode,
                "api_host": draft.api_host,
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

    def update_connection(
        self,
        connection_id: str,
        *,
        expected_version: int,
        changes: dict[str, object],
    ) -> ProviderConnection:
        """在同一事务内修改非 Secret 元数据并使旧验证失效。

        Args:
            connection_id: 保持不变的连接 ID。
            expected_version: 客户端读取的配置版本。
            changes: 白名单非敏感字段。

        Returns:
            新配置版本的连接。

        Raises:
            Conflict: 其他管理员已保存配置。
            ValueError: 字段或端点无效。

        """
        allowed = {
            "display_name",
            "workspace_id",
            "endpoint_mode",
            "api_host",
            "region",
            "request_budget",
            "token_budget",
            "enabled",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("连接修改包含不支持的字段。")
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM provider_connections WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
            if row is None:
                raise NotFound("连接不存在。", stage="connection.update")
            current = _connection(row)
            if current.configuration_version != expected_version:
                raise Conflict(
                    "连接已被修改，请刷新后重试。", stage="connection.update"
                )
            draft_fields = ProviderConnectionDraft.model_fields
            values = {
                key: value
                for key, value in current.model_dump().items()
                if key in draft_fields
            }
            values.update(
                {
                    key: value
                    for key, value in changes.items()
                    if key in draft_fields
                }
            )
            draft = validate_connection_metadata(
                ProviderConnectionDraft.model_validate(values)
            )
            enabled = changes.get("enabled", current.enabled)
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须为布尔值。")
            config = draft.model_dump(
                exclude={
                    "display_name",
                    "provider_type",
                    "credential_id",
                    "endpoint_profile",
                }
            )
            connection.execute(
                "UPDATE provider_connections SET display_name=?, "
                "config_json=?, "
                "configuration_version=configuration_version+1, "
                "enabled=?, "
                "last_validation_id=NULL, status='configured', "
                "updated_at=? "
                "WHERE connection_id=?",
                (
                    draft.display_name,
                    canonical_json(config),
                    int(enabled),
                    _now(),
                    connection_id,
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
                "safe_error_code, synthetic_payload_hash, "
                "configuration_version, diagnostics_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?)",
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
                    validation.configuration_version,
                    canonical_json(
                        validation.model_dump(
                            include={
                                "stage",
                                "request_dispatched",
                                "http_status",
                                "provider_code",
                                "provider_request_id",
                                "endpoint_mode",
                                "endpoint_host",
                            }
                        )
                    ),
                ),
            )
            connection.execute(
                "UPDATE provider_connections SET last_validation_id=?, "
                "status=?, updated_at=? WHERE connection_id=? "
                "AND configuration_version=?",
                (
                    validation.validation_id,
                    "validated"
                    if validation.status == "succeeded"
                    else "degraded",
                    validation.finished_at,
                    validation.connection_id,
                    validation.configuration_version,
                ),
            )
            connection.execute(
                "UPDATE provider_validation_runs SET endpoint_identity=?, "
                "validation_mode=? WHERE validation_id=?",
                (
                    validation.endpoint_identity,
                    validation.validation_mode,
                    validation.validation_id,
                ),
            )
            current = connection.execute(
                "SELECT c.provider_type, c.configuration_version, "
                "d.key_version "
                "FROM provider_connections c JOIN provider_credentials d "
                "ON d.credential_id=c.credential_id WHERE c.connection_id=?",
                (validation.connection_id,),
            ).fetchone()
            operations = require_provider(
                str(current["provider_type"])
            ).operations
            rows = connection.execute(
                "SELECT operation, status FROM (SELECT operation, status, "
                "ROW_NUMBER() OVER (PARTITION BY operation ORDER BY "
                "finished_at DESC, validation_id DESC) AS position "
                "FROM provider_validation_runs WHERE connection_id=? AND "
                "configuration_version=? "
                "AND credential_key_version=? AND validation_mode IN "
                "('mock', 'live')) WHERE position=1",
                (
                    validation.connection_id,
                    current["configuration_version"],
                    current["key_version"],
                ),
            ).fetchall()
            statuses = {str(row[0]): str(row[1]) for row in rows}
            connection.execute(
                "UPDATE provider_connections SET status=? WHERE "
                "connection_id=?",
                (
                    "validated"
                    if all(
                        statuses.get(operation) == "succeeded"
                        for operation in operations
                    )
                    else "degraded",
                    validation.connection_id,
                ),
            )
        if validation.request_dispatched is False:
            return validation
        self.record_provider_operation(
            validation.connection_id,
            operation=validation.operation,
            status_category=(
                "SUCCESS"
                if validation.status == "succeeded"
                else validation.http_category
            ),
            latency_ms=validation.latency_ms,
            estimated_tokens=validation.estimated_tokens,
            observed_tokens=validation.observed_tokens,
            retry_count=0,
            rate_limited=validation.http_category == "http_429",
            safe_error_code=validation.safe_error_code,
        )
        return validation

    def record_provider_call(  # noqa: PLR0913
        self,
        connection_id: str,
        call: ProviderCall,
        *,
        selected_slot: str | None = None,
        failover: bool = False,
        reranker_mode: str | None = None,
        cache_hit: bool = False,
    ) -> None:
        """持久化一个不含请求正文、向量或响应体的调用事件。

        Args:
            connection_id: 页面配置的连接 ID。
            call: Provider adapter 生成的脱敏调用摘要。
            selected_slot: 可选 primary/standby 槽位。
            failover: 是否实际选择备用槽。
            reranker_mode: 可选重排模式。
            cache_hit: 是否命中本地缓存。

        Returns:
            无返回值。

        """
        self.record_provider_operation(
            connection_id,
            operation=call.operation,
            status_category=call.status_category or "UNKNOWN",
            latency_ms=call.elapsed_ms,
            estimated_tokens=call.estimated_tokens or 0,
            observed_tokens=call.observed_tokens,
            retry_count=call.retry_count,
            rate_limited=call.rate_limited or call.reason_code == "HTTP_429",
            selected_slot=selected_slot,
            failover=failover,
            reranker_mode=reranker_mode,
            cache_hit=cache_hit,
            safe_error_code=(
                None if call.reason_code in {None, "OK"} else call.reason_code
            ),
        )

    def record_provider_operation(  # noqa: PLR0913
        self,
        connection_id: str,
        *,
        operation: str,
        status_category: str,
        latency_ms: int,
        estimated_tokens: int,
        observed_tokens: int | None,
        retry_count: int,
        rate_limited: bool,
        selected_slot: str | None = None,
        failover: bool = False,
        reranker_mode: str | None = None,
        cache_hit: bool = False,
        safe_error_code: str | None = None,
    ) -> None:
        """写入固定字段的 Provider 可观测事件。

        Args:
            connection_id: 页面连接 ID。
            operation: 固定操作类别。
            status_category: 脱敏状态类别。
            latency_ms: 总耗时毫秒。
            estimated_tokens: 本地估算 Token。
            observed_tokens: Provider 返回的可选 Token。
            retry_count: 重试次数。
            rate_limited: 是否遇到限流。
            selected_slot: 实际选择的向量槽。
            failover: 是否发生备用切换。
            reranker_mode: 可选重排模式。
            cache_hit: 是否命中缓存。
            safe_error_code: 可选稳定错误码。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO provider_operation_events("
                "event_id, connection_id, occurred_at, operation, "
                "status_category, latency_ms, estimated_tokens, "
                "observed_tokens, retry_count, rate_limited, selected_slot, "
                "failover, reranker_mode, cache_hit, safe_error_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _identifier("opevt"),
                    connection_id,
                    _now(),
                    operation,
                    status_category,
                    latency_ms,
                    estimated_tokens,
                    observed_tokens,
                    retry_count,
                    int(rate_limited),
                    selected_slot,
                    int(failover),
                    reranker_mode,
                    int(cache_hit),
                    safe_error_code,
                ),
            )

    def list_daily_provider_usage(
        self,
        *,
        limit: int = 200,
    ) -> tuple[ProviderUsageDaily, ...]:
        """返回 UTC 日、连接和操作三级聚合。

        Args:
            limit: 最多返回的聚合行数。

        Returns:
            不含正文、向量、端点和 Secret 的用量行。

        """
        if not 1 <= limit <= _MAX_USAGE_PAGE:
            raise ValueError("Provider 用量页大小必须在 1 到 1000。")
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT substr(occurred_at, 1, 10) AS usage_date, "
                "connection_id, operation, count(*) AS request_count, "
                "sum(CASE WHEN status_category='SUCCESS' THEN 1 ELSE 0 END) "
                "AS successful_requests, "
                "sum(CASE WHEN status_category='SUCCESS' THEN 0 ELSE 1 END) "
                "AS failed_requests, "
                "sum(estimated_tokens) AS estimated_tokens, "
                "sum(COALESCE(observed_tokens, 0)) AS observed_tokens, "
                "sum(retry_count) AS retry_count, "
                "sum(rate_limited) AS rate_limit_count, "
                "sum(failover) AS failover_count, "
                "sum(cache_hit) AS cache_hit_count, "
                "CAST(round(avg(latency_ms)) AS INTEGER) AS average_latency_ms "
                "FROM provider_operation_events "
                "GROUP BY usage_date, connection_id, operation "
                "ORDER BY usage_date DESC, connection_id, operation LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(ProviderUsageDaily(**dict(row)) for row in rows)

    def reserve_daily_provider_budget(
        self,
        connection_id: str,
        operation: str,
        estimated_tokens: int,
        *,
        request_limit: int,
        token_limit: int,
    ) -> None:
        """原子预留跨进程、跨重启的 UTC 日 Provider 预算。

        Args:
            connection_id: 备用 Provider 连接。
            operation: 固定操作名。
            estimated_tokens: 本次本地估算量。
            request_limit: UTC 日请求上限。
            token_limit: UTC 日 Token 上限。

        Returns:
            无返回值。

        Raises:
            PolicyDenied: 预算无效或本次预留将超限。

        """
        if estimated_tokens < 0 or request_limit <= 0 or token_limit <= 0:
            raise PolicyDenied(
                "Provider 日预算未配置正数限制。",
                stage="provider.budget",
            )
        today = datetime.now(UTC).date().isoformat()
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT requests, estimated_tokens FROM provider_daily_budgets "
                "WHERE usage_date=? AND connection_id=? AND operation=?",
                (today, connection_id, operation),
            ).fetchone()
            requests = 0 if row is None else int(row["requests"])
            tokens = 0 if row is None else int(row["estimated_tokens"])
            if (
                requests + 1 > request_limit
                or tokens + estimated_tokens > token_limit
            ):
                raise PolicyDenied(
                    "Provider 日预算已耗尽。",
                    stage="provider.budget",
                    details={
                        "connection_id": connection_id,
                        "operation": operation,
                    },
                )
            connection.execute(
                "INSERT INTO provider_daily_budgets("
                "usage_date, connection_id, operation, requests, "
                "estimated_tokens, updated_at) VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(usage_date, connection_id, operation) DO UPDATE "
                "SET requests=requests + 1, "
                "estimated_tokens=estimated_tokens + "
                "excluded.estimated_tokens, "
                "updated_at=excluded.updated_at",
                (today, connection_id, operation, estimated_tokens, _now()),
            )

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
        retrieval_policy = resolve_retrieval_policy(
            draft.retrieval_policy, draft.evidence_policy
        )
        evidence_policy: dict[str, object] = {}
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
        primary_spec = resolve_embedding(
            primary,
            primary_embedding_model,
            primary_dimension,
            primary_document_policy,
            primary_query_policy,
        )
        primary_document_policy = dict(primary_spec.document_policy)
        primary_query_policy = dict(primary_spec.query_policy)
        standby_spec = None
        if standby is not None:
            if standby_embedding_model is None or standby_dimension is None:
                raise ValueError("备用模型和维度必须完整。")
            standby_spec = resolve_embedding(
                standby,
                standby_embedding_model,
                standby_dimension,
                standby_document_policy,
                standby_query_policy,
            )
            standby_document_policy = dict(standby_spec.document_policy)
            standby_query_policy = dict(standby_spec.query_policy)
        _validate_v1_profile_contract(
            primary,
            primary_dimension,
            primary_document_policy,
            primary_query_policy,
            standby,
            standby_dimension,
            standby_document_policy,
            standby_query_policy,
            reranker,
        )
        if failover_enabled and standby is None:
            raise ValueError("启用 Failover 时必须配置备用 Embedding。")
        if primary_dimension <= 0 or (
            standby is not None and (standby_dimension or 0) <= 0
        ):
            raise ValueError("Embedding Dimension 必须为正数。")
        index_payload: dict[str, object] = {
            "resolved_index_contract": self.index_contract,
            "chunker": "docx-structural-v3",
            "fts_analyzer": "deterministic-cjk-bigram-v2",
            "primary": primary_spec.semantic_identity(),
            "standby": None
            if standby_spec is None
            else standby_spec.semantic_identity(),
            "topology": "hot_standby" if standby is not None else "single",
        }
        index_fingerprint = canonical_sha256(index_payload)
        serving_fingerprint = canonical_sha256(
            {
                "base_index": index_fingerprint,
                "failover_enabled": failover_enabled,
                "evidence_policy": evidence_policy,
                "reranker": None
                if reranker is None
                else {
                    "model": reranker_model,
                    "provider": reranker.provider_type,
                },
                "retrieval_policy": retrieval_policy,
                "standby_budget": standby_budget,
                "connection_budgets": [
                    {
                        "requests": item.request_budget,
                        "tokens": item.token_budget,
                    }
                    for item in (primary, standby, reranker)
                    if item is not None
                ],
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
            connection.execute(
                "UPDATE retrieval_profile_revisions SET "
                "primary_resolved_json=?, "
                "standby_resolved_json=? WHERE profile_revision_id=?",
                (
                    canonical_json(primary_spec.model_dump(mode="json")),
                    None
                    if standby_spec is None
                    else canonical_json(standby_spec.model_dump(mode="json")),
                    profile_id,
                ),
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
        with self._connections.transaction() as connection:
            kb = connection.execute(
                "SELECT active_revision_id FROM knowledge_bases WHERE "
                "knowledge_base_id=?",
                (proposed.knowledge_base_id,),
            ).fetchone()
            documents = connection.execute(
                "SELECT 1 FROM documents WHERE knowledge_base_id=? "
                "AND lifecycle_status='active' LIMIT 1",
                (proposed.knowledge_base_id,),
            ).fetchone()
        if preview.index_fingerprint_changed and documents is not None:
            if self.queue_profile is None:
                raise Conflict(
                    "持久构建服务尚未绑定。", stage="profile.activate"
                )
            self.queue_profile(
                proposed,
                preview.current_profile_revision_id,
                kb["active_revision_id"],
            )
            return self.get_profile(profile_revision_id)
        now = _now()
        with self._connections.transaction(write=True) as connection:
            current = connection.execute(
                "SELECT profile_revision_id FROM retrieval_profile_revisions "
                "WHERE knowledge_base_id=? AND status='active'",
                (proposed.knowledge_base_id,),
            ).fetchone()
            if (
                None if current is None else current[0]
            ) != preview.current_profile_revision_id:
                raise Conflict("Profile 预览已过期。", stage="profile.activate")
            connection.execute(
                "UPDATE retrieval_profile_revisions SET status='retired' "
                "WHERE knowledge_base_id=? AND status='active'",
                (proposed.knowledge_base_id,),
            )
            changed = connection.execute(
                "UPDATE retrieval_profile_revisions SET status='active', "
                "activated_at=? WHERE profile_revision_id=? AND status='draft'",
                (now, profile_revision_id),
            )
            if changed.rowcount != 1:
                raise Conflict(
                    "Draft 已被其他请求处理。", stage="profile.activate"
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
        return tuple(
            key
            for key, run in self.profile_validations(
                profile_revision_id
            ).items()
            if run is None or run.status != "succeeded"
        )

    def profile_validations(
        self, profile_revision_id: str
    ) -> dict[str, ProviderValidationRun | None]:
        """按精确连接、模型、角色和解析参数读取有效验证。

        Args:
            profile_revision_id: 目标不可变方案。

        Returns:
            每个必需操作的独立记录；未验证为 None。

        """
        profile = self.get_profile(profile_revision_id)
        try:
            specs = profile_specs(profile, self.get_connection)
        except ValueError:
            return {f"{profile.primary_connection_id}:embedding.query": None}
        required: list[tuple[str, str, str, int | None, str]] = [
            (
                spec.connection_id,
                operation,
                spec.model,
                spec.dimension,
                spec.policy_identity(operation),
            )
            for spec in specs
            for operation in ("embedding.document", "embedding.query")
        ]
        if profile.reranker_connection_id is not None:
            required.append(
                (
                    profile.reranker_connection_id,
                    "reranking",
                    profile.reranker_model or "",
                    None,
                    canonical_sha256(
                        {
                            "model": profile.reranker_model,
                            "operation": "reranking",
                        }
                    ),
                )
            )
        results: dict[str, ProviderValidationRun | None] = {}
        for connection_id, operation, model, dimension, policy in required:
            connection = self.get_connection(connection_id)
            key_version = self._credentials.get(
                connection.credential_id
            ).key_version
            results[f"{connection_id}:{operation}"] = next(
                (
                    run
                    for run in self.list_validations(connection_id)
                    if run.operation == operation
                    and run.provider_model == model
                    and run.dimension == dimension
                    and run.request_policy_identity == policy
                    and validation_is_current(run, connection, key_version)
                ),
                None,
            )
        return results

    def latest_status(self, connection_id: str, operation: str) -> str:
        """返回指定操作最近一次持久验证状态。

        Args:
            connection_id: Provider Connection ID。
            operation: 目录中的操作 ID。

        Returns:
            succeeded、failed 或 not_verified。

        """
        try:
            validate_connection_metadata(
                ProviderConnectionDraft.model_validate(
                    self.get_connection(connection_id).model_dump(
                        include=set(ProviderConnectionDraft.model_fields)
                    )
                )
            )
        except ValueError:
            return "not_verified"
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT v.status FROM provider_validation_runs v "
                "JOIN provider_connections c "
                "ON c.connection_id=v.connection_id "
                "JOIN provider_credentials d "
                "ON d.credential_id=c.credential_id "
                "WHERE v.connection_id=? AND v.operation=? "
                "AND c.enabled=1 AND v.validation_mode IN ('mock', 'live') "
                "AND v.credential_key_version=d.key_version "
                "AND v.configuration_version=c.configuration_version "
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
            profiles = connection.execute(
                "SELECT profile_revision_id FROM retrieval_profile_revisions "
                "WHERE status='active' ORDER BY profile_revision_id"
            ).fetchall()
        return {
            "active_profile_count": int(active["count"]) if active else 0,
            "active_profile_ids": [str(row[0]) for row in profiles],
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


def _validate_v1_profile_contract(  # noqa: PLR0913, PLR0917
    primary: ProviderConnection,
    primary_dimension: int,
    primary_document_policy: dict[str, object],
    primary_query_policy: dict[str, object],
    standby: ProviderConnection | None,
    standby_dimension: int | None,
    standby_document_policy: dict[str, object],
    standby_query_policy: dict[str, object],
    reranker: ProviderConnection | None,
) -> None:
    """拒绝页面配置偏离 P11 已实现的固定 Provider 合同。"""
    if primary.provider_type != "jina" or primary_dimension != _P11_DIMENSION:
        raise ValueError("P11 Primary 必须是 1024 维 Jina Embedding。")
    if primary_document_policy.get("task") != "retrieval.passage":
        raise ValueError("Jina document task 必须是 retrieval.passage。")
    if primary_query_policy.get("task") != "retrieval.query":
        raise ValueError("Jina query task 必须是 retrieval.query。")
    if standby is not None:
        if standby.provider_type != "aliyun-model-studio":
            raise ValueError("P11 Standby 必须是阿里云百炼。")
        if standby_dimension != _P11_DIMENSION:
            raise ValueError("P11 Standby 必须是 1024 维。")
        if standby_document_policy.get("text_type") != "document":
            raise ValueError("百炼 document text_type 必须是 document。")
        if standby_query_policy.get("text_type") != "query":
            raise ValueError("百炼 query text_type 必须是 query。")
    if reranker is not None and reranker.provider_type != "jina":
        raise ValueError("P11 Reranker 必须是 Jina。")


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
        configuration_version=int(row["configuration_version"]),
        endpoint_mode=config.get("endpoint_mode") or (
            "workspace_host" if row["provider_type"] == "jina" else ""
        ),
        api_host=config.get("api_host"),
        workspace_id=config.get("workspace_id"),
        region=config.get("region"),
        request_budget=int(config["request_budget"]),
        token_budget=int(config["token_budget"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _validation(row: Row) -> ProviderValidationRun:
    return ProviderValidationRun(
        endpoint_identity=row["endpoint_identity"],
        validation_mode=str(row["validation_mode"]),
        configuration_version=int(row["configuration_version"]),
        **json.loads(str(row["diagnostics_json"])),
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
        primary_resolved=freeze_json_object(
            json.loads(row["primary_resolved_json"] or "{}")
        ),
        standby_resolved=freeze_json_object(
            json.loads(row["standby_resolved_json"] or "{}")
        ),
        activation_job_id=row["activation_job_id"],
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


def validate_connection_metadata(
    draft: ProviderConnectionDraft,
) -> ProviderConnectionDraft:
    """在创建 Credential 之前校验完整连接元数据。

    Args:
        draft: 非 Secret 连接草稿。

    Returns:
        仅 trim 外侧空格并规范化 Origin 的草稿。

    Raises:
        ValueError: 目录、名称、端点或预算无效。

    """
    provider = require_provider(draft.provider_type)
    if (
        not draft.display_name.strip()
        or len(draft.display_name) > _MAX_DISPLAY_NAME
    ):
        raise ValueError("连接名称必须为 1 到 200 个字符。")
    if draft.endpoint_profile not in provider.endpoint_profiles:
        raise ValueError("Endpoint Profile 不在内置目录中。")
    if draft.provider_type == "aliyun-model-studio":
        config = AliyunEndpointConfig.model_validate(
            {
                "workspace_id": draft.workspace_id,
                "region": draft.region,
                "endpoint_mode": draft.endpoint_mode,
                "api_host": draft.api_host,
            }
        )
        return draft.model_copy(
            update={
                "workspace_id": config.workspace_id,
                "api_host": resolve_endpoint(config),
            }
        )
    if (
        draft.workspace_id is not None
        or draft.region is not None
        or draft.api_host is not None
        or draft.endpoint_mode != "workspace_host"
    ):
        raise ValueError("Jina 连接不能保存百炼端点配置。")
    return draft


__all__ = ["ProductControlStore", "validate_connection_metadata"]
