"""产品控制面的安全持久化读模型。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, StrictInt, field_validator

from rag_app.core.models.common import FrozenModel, JsonObject
from rag_app.core.models.search import RetrievalPolicy


class ImpactKind(StrEnum):
    """Retrieval Profile 变更的最小部署影响。"""

    NO_REINDEX = "NO_REINDEX"
    SERVING_RELOAD = "SERVING_RELOAD"
    NEW_INDEX_REVISION_REQUIRED = "NEW_INDEX_REVISION_REQUIRED"


class CredentialSummary(FrozenModel):
    """永不返回密文、nonce 或环境变量值的 Credential 视图。"""

    credential_id: str
    provider_type: str
    configured: bool
    source: str
    masked_hint: str
    key_version: StrictInt = Field(gt=0)
    status: str
    created_at: str
    updated_at: str


class ProviderConnectionDraft(FrozenModel):
    """创建 Provider Connection 所需的非 Secret 输入。"""

    display_name: str
    provider_type: str
    credential_id: str
    endpoint_profile: str = "default"
    endpoint_mode: str = "workspace_host"
    api_host: str | None = None
    workspace_id: str | None = None
    region: str | None = None
    request_budget: StrictInt = Field(default=5, ge=1, le=20)
    token_budget: StrictInt = Field(default=4096, ge=1, le=1_000_000)


class ProviderConnection(FrozenModel):
    """Provider 连接与非 Secret 配置。"""

    connection_id: str
    display_name: str
    provider_type: str
    credential_id: str
    endpoint_profile: str
    configuration_version: StrictInt = Field(default=1, gt=0)
    enabled: bool
    status: str
    last_validation_id: str | None = None
    endpoint_mode: str = "workspace_host"
    api_host: str | None = None
    workspace_id: str | None = None
    region: str | None = None
    request_budget: StrictInt = Field(default=5, ge=1, le=20)
    token_budget: StrictInt = Field(default=4096, ge=1, le=1_000_000)
    created_at: str
    updated_at: str


class ProviderValidationRun(FrozenModel):
    """不保存请求原文和 Provider Body 的验证记录。"""

    validation_id: str
    connection_id: str
    catalog_version: str
    operation: str
    provider_model: str
    credential_key_version: StrictInt = Field(gt=0)
    request_policy_identity: str
    started_at: str
    finished_at: str
    status: str
    http_category: str
    dimension: StrictInt | None = Field(default=None, gt=0)
    estimated_tokens: StrictInt = Field(ge=0)
    observed_tokens: StrictInt | None = Field(default=None, ge=0)
    latency_ms: StrictInt = Field(ge=0)
    safe_error_code: str | None = None
    configuration_version: StrictInt = Field(default=1, gt=0)
    stage: str = "legacy"
    request_dispatched: bool | None = None
    http_status: int | None = None
    provider_code: str | None = None
    provider_request_id: str | None = None
    endpoint_mode: str | None = None
    endpoint_host: str | None = None
    synthetic_payload_hash: str


class ProviderUsageDaily(FrozenModel):
    """按 UTC 日、连接与操作聚合的脱敏 Provider 用量。"""

    usage_date: str
    connection_id: str
    operation: str
    request_count: StrictInt = Field(ge=0)
    successful_requests: StrictInt = Field(ge=0)
    failed_requests: StrictInt = Field(ge=0)
    estimated_tokens: StrictInt = Field(ge=0)
    observed_tokens: StrictInt = Field(ge=0)
    retry_count: StrictInt = Field(ge=0)
    rate_limit_count: StrictInt = Field(ge=0)
    failover_count: StrictInt = Field(ge=0)
    cache_hit_count: StrictInt = Field(ge=0)
    average_latency_ms: StrictInt = Field(ge=0)


class RetrievalProfileRevision(FrozenModel):
    """知识库级不可变 Retrieval Profile Revision。"""

    profile_revision_id: str
    knowledge_base_id: str
    status: str
    primary_connection_id: str
    primary_embedding_model: str
    primary_dimension: StrictInt = Field(gt=0)
    primary_document_policy: JsonObject
    primary_query_policy: JsonObject
    standby_connection_id: str | None = None
    standby_embedding_model: str | None = None
    standby_dimension: StrictInt | None = Field(default=None, gt=0)
    standby_document_policy: JsonObject = ()
    standby_query_policy: JsonObject = ()
    reranker_connection_id: str | None = None
    reranker_model: str | None = None
    failover_enabled: bool
    standby_budget: JsonObject
    retrieval_policy: JsonObject
    evidence_policy: JsonObject
    index_semantic_fingerprint: str
    serving_fingerprint: str
    created_at: str
    activated_at: str | None = None


class RetrievalProfileDraft(FrozenModel):
    """创建知识库 Retrieval Profile Revision 的完整输入。"""

    knowledge_base_id: str
    primary_connection_id: str
    primary_embedding_model: str
    primary_dimension: StrictInt = Field(gt=0)
    primary_document_policy: dict[str, object]
    primary_query_policy: dict[str, object]
    standby_connection_id: str | None = None
    standby_embedding_model: str | None = None
    standby_dimension: StrictInt | None = Field(default=None, gt=0)
    standby_document_policy: dict[str, object] = Field(default_factory=dict)
    standby_query_policy: dict[str, object] = Field(default_factory=dict)
    reranker_connection_id: str | None = None
    reranker_model: str | None = None
    failover_enabled: bool = False
    standby_budget: dict[str, object] = Field(default_factory=dict)
    retrieval_policy: dict[str, object] = Field(default_factory=dict)
    evidence_policy: dict[str, object] = Field(default_factory=dict)

    @field_validator("retrieval_policy")
    @classmethod
    def _validate_retrieval_policy(
        cls, value: dict[str, object]
    ) -> dict[str, object]:
        """在保存和激活前拒绝运行时不支持的检索参数。"""
        RetrievalPolicy.model_validate(value)
        return value


class ImpactPreview(FrozenModel):
    """激活前必须展示的指纹差异与重建决策。"""

    impact: ImpactKind
    current_profile_revision_id: str | None
    proposed_profile_revision_id: str
    index_fingerprint_changed: bool
    serving_fingerprint_changed: bool


class AccessTokenSummary(FrozenModel):
    """不含完整 Token 的接口访问凭据摘要。"""

    token_id: str
    name: str
    scopes: tuple[str, ...]
    project_id: str | None = None
    knowledge_base_id: str | None = None
    expires_at: str | None = None
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None


class AccessTokenIssue(AccessTokenSummary):
    """仅创建响应返回一次的完整 Token。"""

    token: str


__all__ = [
    "AccessTokenIssue",
    "AccessTokenSummary",
    "CredentialSummary",
    "ImpactKind",
    "ImpactPreview",
    "ProviderConnection",
    "ProviderConnectionDraft",
    "ProviderUsageDaily",
    "ProviderValidationRun",
    "RetrievalProfileDraft",
    "RetrievalProfileRevision",
]
