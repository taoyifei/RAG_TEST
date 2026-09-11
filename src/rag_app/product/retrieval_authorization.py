"""把真实检索方案、活动文档与累计 Provider 预算绑定为显式批准。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, StrictInt, model_validator

from rag_app.adapters.providers.batching import BatchLimits, batch_texts
from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_models import BudgetCampaign
from rag_app.adapters.providers.budget_transport import (
    provider_budget_scope,
    provider_data_scope,
)
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import PolicyDenied, RagError
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.management import QueuedIngestion
from rag_app.core.tokenization import estimate_provider_input_tokens
from rag_app.product.control_store import ProductControlStore
from rag_app.product.models import RetrievalProfileRevision
from rag_app.product.provider_runtime import ProviderRuntimeRegistry

RetrievalOperation = Literal[
    "embedding.document", "embedding.query", "reranking"
]
RetrievalAuthorizationState = Literal[
    "NOT_REQUIRED",
    "MISSING",
    "APPROVED",
    "STALE_CORPUS",
    "STALE_PROFILE",
    "EXPIRED",
    "BLOCKED",
]
RetrievalIngestionAuthorizationState = Literal[
    "MISSING",
    "APPROVED",
    "STALE_JOB",
    "STALE_PROFILE",
    "EXPIRED",
    "BLOCKED",
]
RetrievalIngestionNextAction = Literal[
    "approve",
    "continue",
    "repair_profile",
    "reauthorize",
    "review_document",
]
RetrievalBudgetState = Literal["MISSING", "AVAILABLE", "EXHAUSTED", "BLOCKED"]
ConnectionBudgetState = Literal["READY", "INSUFFICIENT", "BLOCKED"]

_POLICY_REVISION = "retrieval-authorization-v1"
_INGESTION_POLICY_REVISION = "retrieval-ingestion-authorization-v1"
_MAX_AUTHORIZATION_DAYS = 365
_DOCUMENT_PROVIDER_BATCH_ITEMS = 16


class RetrievalAuthorizationApproval(FrozenModel):
    """管理员对当前真实文档检索用途给出的有界累计预算。"""

    expires_at: str
    request_limit: StrictInt = Field(ge=1, le=10_000)
    estimated_token_limit: StrictInt = Field(ge=1, le=100_000_000)
    operation_request_limits: dict[RetrievalOperation, StrictInt]

    @model_validator(mode="after")
    def _validate_limits_and_expiry(self) -> RetrievalAuthorizationApproval:
        expected_operations = {
            "embedding.document",
            "embedding.query",
        }
        if not expected_operations <= set(self.operation_request_limits):
            raise ValueError("检索授权必须分别限制文档与查询 Embedding 请求。")
        if any(value < 1 for value in self.operation_request_limits.values()):
            raise ValueError("检索授权 operation 请求上限必须为正数。")
        if self.request_limit < sum(self.operation_request_limits.values()):
            raise ValueError("累计请求上限不能小于各 operation 上限之和。")
        try:
            expires_at = datetime.fromisoformat(self.expires_at)
        except ValueError:
            raise ValueError("检索授权到期时间格式无效。") from None
        now = datetime.now(UTC)
        if expires_at.tzinfo is None or not (
            now < expires_at <= now + timedelta(days=_MAX_AUTHORIZATION_DAYS)
        ):
            raise ValueError("检索授权必须在未来 365 天内明确到期。")
        return self


class RetrievalAuthorizationManifest(FrozenModel):
    """不保存正文或逐文档哈希的不可变检索批准记录。"""

    manifest_id: str = Field(pattern=r"^rauth_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    profile_revision_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    source_index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    active_document_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    active_document_count: StrictInt = Field(gt=0)
    profile_binding_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    operations: tuple[RetrievalOperation, ...] = Field(min_length=2)
    authorization_id: str
    budget_campaign_id: str
    created_at: str
    expires_at: str
    policy_revision: str = _POLICY_REVISION
    approved_by_session_id: str


class RetrievalIngestionAuthorizationManifest(FrozenModel):
    """绑定一个待入库 Job 最终刷新快照的不可变批准记录。"""

    manifest_id: str = Field(pattern=r"^riauth_[0-9a-f]{32}$")
    job_id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    profile_revision_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    predecessor_index_revision_id: str | None = Field(
        default=None, pattern=r"^irev_[0-9a-f]{32}$"
    )
    target_index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    source_binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    active_document_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_document_count: StrictInt = Field(gt=0)
    source_size_bytes: StrictInt = Field(gt=0)
    profile_binding_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    operations: tuple[RetrievalOperation, ...] = Field(min_length=2)
    authorization_id: str
    budget_campaign_id: str
    created_at: str
    expires_at: str
    policy_revision: str = _INGESTION_POLICY_REVISION
    approved_by_session_id: str

    @property
    def source_index_revision_id(self) -> str:
        """把成功入库目标作为查询授权的来源 Revision。"""
        return self.target_index_revision_id

    @property
    def active_document_count(self) -> int:
        """兼容活动语料授权投影使用的文档计数名称。"""
        return self.source_document_count


class RetrievalAuthorizationStatus(FrozenModel):
    """草稿、激活与查询共享的实时检索授权投影。"""

    authorization_state: RetrievalAuthorizationState
    budget_state: RetrievalBudgetState
    connection_budget_state: ConnectionBudgetState
    required_operations: tuple[RetrievalOperation, ...]
    estimated_document_chunks: StrictInt = Field(ge=0)
    estimated_document_requests_per_slot: StrictInt = Field(ge=0)
    estimated_document_tokens_per_slot: StrictInt = Field(ge=0)
    embedding_slot_count: StrictInt = Field(ge=1, le=2)
    manifest: RetrievalAuthorizationManifest | None = None
    reason_codes: tuple[str, ...] = ()


class RetrievalIngestionAuthorizationStatus(FrozenModel):
    """待入库 Job 的真实资料、Profile 与硬预算批准状态。"""

    authorization_state: RetrievalIngestionAuthorizationState
    budget_state: RetrievalBudgetState
    connection_budget_state: ConnectionBudgetState
    next_action: RetrievalIngestionNextAction
    approval_allowed: bool
    estimation_state: Literal["UNAVAILABLE_PREBUILD"] = "UNAVAILABLE_PREBUILD"
    job_id: str = Field(pattern=r"^job_[0-9a-f]{32}$")
    profile_revision_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    predecessor_index_revision_id: str | None = Field(
        default=None, pattern=r"^irev_[0-9a-f]{32}$"
    )
    target_index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    source_document_count: StrictInt = Field(gt=0)
    source_size_bytes: StrictInt = Field(gt=0)
    embedding_slot_count: StrictInt = Field(ge=1, le=2)
    required_operations: tuple[RetrievalOperation, ...] = Field(min_length=2)
    recommended_request_limit: StrictInt = Field(ge=0, le=10_000)
    recommended_estimated_token_limit: StrictInt = Field(ge=0, le=100_000_000)
    recommended_operation_request_limits: dict[RetrievalOperation, StrictInt]
    manifest: RetrievalIngestionAuthorizationManifest | None = None
    reason_codes: tuple[str, ...] = ()


class RetrievalAuthorizationStore:
    """只允许管理员把候选方案用于当下活动真实文档。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        control: ProductControlStore,
        providers: ProviderRuntimeRegistry,
        ledger_path: Path,
    ) -> None:
        self._connections = connections
        self._control = control
        self._providers = providers
        self._ledger_path = ledger_path

    def approve(
        self,
        profile_revision_id: str,
        approval: RetrievalAuthorizationApproval,
        *,
        approved_by_session_id: str,
    ) -> RetrievalAuthorizationStatus:
        """批准当前候选方案对当前活动文档的有限真实调用。

        Args:
            profile_revision_id: 已保存且验证通过的候选或活动方案。
            approval: 管理员明确给出的到期时间与累计预算。
            approved_by_session_id: 由认证中间件提供的管理员 Session ID。

        Returns:
            新清单保存后的动态状态；本操作本身不发起 Provider 请求。

        Raises:
            ValueError: 方案、活动文档、验证或预算不能满足安全门禁。

        """
        profile = self._control.get_profile(profile_revision_id)
        if profile.status not in {"draft", "active"}:
            raise ValueError("只能批准候选或活动 Retrieval Profile。")
        validation_issues = self._control.profile_validation_issues(
            profile_revision_id
        )
        if validation_issues:
            raise ValueError("检索方案尚未完成全部当前连接验证。")
        snapshot = self._snapshot(profile.knowledge_base_id)
        if not cast(bool, snapshot["index_aligned"]):
            raise ValueError("当前活动文档与活动 Index Revision 尚未对齐。")
        if cast(int, snapshot["active_document_count"]) == 0:
            raise ValueError("活动知识库没有可批准的文档版本。")
        requirements = self._requirements(profile, snapshot)
        budget_issues = cast(tuple[str, ...], requirements["budget_issues"])
        if budget_issues:
            raise ValueError("；".join(budget_issues))
        operations = cast(
            tuple[RetrievalOperation, ...], requirements["operations"]
        )
        if set(approval.operation_request_limits) != set(operations):
            raise ValueError("每个实际检索 operation 必须具有独立请求上限。")
        required_document_requests = cast(
            int, requirements["document_requests_total"]
        )
        if (
            approval.operation_request_limits["embedding.document"]
            < required_document_requests
        ):
            raise ValueError(
                "embedding.document 请求上限低于当前文档完整重建需求。"
            )
        required_tokens = cast(int, requirements["document_tokens_total"])
        if approval.estimated_token_limit < required_tokens:
            raise ValueError("累计 Token 上限低于当前文档完整重建估算。")

        bindings = cast(
            tuple[tuple[RetrievalOperation, str, str, str], ...],
            requirements["bindings"],
        )
        request_identities = tuple(sorted({item[3] for item in bindings}))
        source_hashes = cast(tuple[str, ...], snapshot["source_hashes"])
        provider_request_limits: dict[str, int] = {}
        provider_token_limits: dict[str, int] = {}
        for _, connection_id, _, _ in bindings:
            connection = self._control.get_connection(connection_id)
            provider = connection.provider_type.replace("-model-studio", "")
            provider_request_limits[provider] = min(
                provider_request_limits.get(
                    provider, connection.request_budget
                ),
                connection.request_budget,
            )
            provider_token_limits[provider] = min(
                provider_token_limits.get(provider, connection.token_budget),
                connection.token_budget,
            )
        manifest_id = f"rauth_{uuid.uuid4().hex}"
        authorization_id = f"retrieval-auth-{uuid.uuid4().hex}"
        campaign_id = f"retrieval-budget-{uuid.uuid4().hex}"
        project_id = cast(str, snapshot["project_id"])
        campaign = BudgetCampaign(
            campaign_id=campaign_id,
            authorization_id=authorization_id,
            scope=f"{project_id}:{profile.knowledge_base_id}",
            request_limit=approval.request_limit,
            estimated_token_limit=approval.estimated_token_limit,
            approved_request_identities=request_identities,
            # 总授权不能绕过每条 Provider Connection 的独立上限。
            provider_request_limits=provider_request_limits,
            provider_token_limits=provider_token_limits,
            scope_mode="knowledge_base",
            project_id=project_id,
            knowledge_base_id=profile.knowledge_base_id,
            approved_source_hashes=source_hashes,
            allowed_models=tuple(sorted({item[2] for item in bindings})),
            allowed_operations=operations,
            expires_at=approval.expires_at,
            operation_request_limits=cast(
                Mapping[str, int], approval.operation_request_limits
            ),
        )
        ProviderBudgetLedger(self._ledger_path).create_campaign(campaign)
        manifest = RetrievalAuthorizationManifest(
            manifest_id=manifest_id,
            project_id=project_id,
            knowledge_base_id=profile.knowledge_base_id,
            profile_revision_id=profile.profile_revision_id,
            source_index_revision_id=cast(
                str, snapshot["active_index_revision_id"]
            ),
            active_document_digest=cast(
                str, snapshot["active_document_digest"]
            ),
            active_document_count=cast(int, snapshot["active_document_count"]),
            profile_binding_identity=cast(
                str, requirements["profile_binding_identity"]
            ),
            operations=operations,
            authorization_id=authorization_id,
            budget_campaign_id=campaign_id,
            created_at=datetime.now(UTC).isoformat(),
            expires_at=approval.expires_at,
            approved_by_session_id=approved_by_session_id,
        )
        with self._connections.transaction(write=True) as db_connection:
            db_connection.execute(
                "INSERT INTO retrieval_authorization_manifests("
                "manifest_id,project_id,knowledge_base_id,profile_revision_id,"
                "source_index_revision_id,active_document_digest,"
                "active_document_count,profile_binding_identity,"
                "operations_json,authorization_id,budget_campaign_id,"
                "created_at,expires_at,policy_revision,approved_by_session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    manifest.manifest_id,
                    manifest.project_id,
                    manifest.knowledge_base_id,
                    manifest.profile_revision_id,
                    manifest.source_index_revision_id,
                    manifest.active_document_digest,
                    manifest.active_document_count,
                    manifest.profile_binding_identity,
                    json.dumps(manifest.operations, separators=(",", ":")),
                    manifest.authorization_id,
                    manifest.budget_campaign_id,
                    manifest.created_at,
                    manifest.expires_at,
                    manifest.policy_revision,
                    manifest.approved_by_session_id,
                ),
            )
        return self.status(profile_revision_id)

    def approve_ingestion(
        self,
        job_id: str,
        approval: RetrievalAuthorizationApproval,
        *,
        approved_by_session_id: str,
    ) -> RetrievalIngestionAuthorizationStatus:
        """批准一个待入库 Job 刷新后的完整资料集合。

        Args:
            job_id: 已持久化且因检索授权阻塞的 Job。
            approval: 管理员明确给出的到期时间与累计预算。
            approved_by_session_id: 由认证中间件提供的管理员 Session ID。

        Returns:
            绑定 Job、目标 Revision、资料与 Profile 的动态状态。

        Raises:
            ValueError: Job 状态、绑定、Profile 或预算不满足安全门禁。

        """
        snapshot = self._prospective_ingestion_snapshot(job_id)
        status = self._ingestion_status(snapshot)
        existing_manifest = status.manifest
        if (
            status.authorization_state == "APPROVED"
            and status.budget_state == "AVAILABLE"
            and status.connection_budget_state == "READY"
            and existing_manifest is not None
            and snapshot["error_code"]
            in {None, "RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED"}
            and self._approval_matches_campaign(existing_manifest, approval)
        ):
            self._mark_ingestion_authorized(job_id)
            return self.ingestion_status(job_id)
        if (
            snapshot["job_state"] != "failed_retryable"
            or snapshot["request_state"] != "failed"
        ):
            raise ValueError("只能批准已稳定等待检索授权的入库任务。")
        error_code = cast(str | None, snapshot["error_code"])
        if error_code is not None and not error_code.startswith(
            "RETRIEVAL_INGESTION_"
        ):
            raise ValueError("任务并非因真实检索授权而暂停。")
        if error_code is None and existing_manifest is None:
            raise ValueError("任务并非因真实检索授权而暂停。")
        supersedes_available_campaign = (
            status.authorization_state == "APPROVED"
            and status.budget_state == "AVAILABLE"
            and status.connection_budget_state == "READY"
            and existing_manifest is not None
            and not self._approval_matches_campaign(existing_manifest, approval)
        )
        if not status.approval_allowed and not supersedes_available_campaign:
            raise ValueError(
                "入库任务的 Retrieval Profile 尚未就绪，请先修复方案或连接。"
            )
        requirements = cast(dict[str, object], snapshot["requirements"])
        operations = cast(
            tuple[RetrievalOperation, ...], requirements["operations"]
        )
        if set(approval.operation_request_limits) != set(operations):
            raise ValueError("每个实际检索 operation 必须具有独立请求上限。")
        if approval.request_limit > status.recommended_request_limit:
            raise ValueError("累计请求上限超过当前连接允许的硬上限。")
        if (
            approval.estimated_token_limit
            > status.recommended_estimated_token_limit
        ):
            raise ValueError("累计 Token 上限超过当前连接允许的硬上限。")
        per_operation_cap = cast(int, requirements["request_limit_cap"])
        if any(
            limit > per_operation_cap
            for limit in approval.operation_request_limits.values()
        ):
            raise ValueError("operation 请求上限超过当前连接允许的硬上限。")

        bindings = cast(
            tuple[tuple[RetrievalOperation, str, str, str], ...],
            requirements["bindings"],
        )
        manifest_id = f"riauth_{uuid.uuid4().hex}"
        authorization_id = f"retrieval-ingestion-{uuid.uuid4().hex}"
        campaign_id = f"retrieval-ingestion-budget-{uuid.uuid4().hex}"
        campaign = BudgetCampaign(
            campaign_id=campaign_id,
            authorization_id=authorization_id,
            scope=(
                f"{snapshot['project_id']}:{snapshot['knowledge_base_id']}:"
                f"{job_id}"
            ),
            request_limit=approval.request_limit,
            estimated_token_limit=approval.estimated_token_limit,
            approved_request_identities=tuple(
                sorted({item[3] for item in bindings})
            ),
            provider_request_limits=cast(
                Mapping[str, int], requirements["provider_request_limits"]
            ),
            provider_token_limits=cast(
                Mapping[str, int], requirements["provider_token_limits"]
            ),
            scope_mode="knowledge_base",
            project_id=cast(str, snapshot["project_id"]),
            knowledge_base_id=cast(str, snapshot["knowledge_base_id"]),
            approved_source_hashes=cast(
                tuple[str, ...], snapshot["source_hashes"]
            ),
            allowed_models=tuple(sorted({item[2] for item in bindings})),
            allowed_operations=operations,
            expires_at=approval.expires_at,
            operation_request_limits=cast(
                Mapping[str, int], approval.operation_request_limits
            ),
        )
        ProviderBudgetLedger(self._ledger_path).create_campaign(campaign)
        manifest = RetrievalIngestionAuthorizationManifest(
            manifest_id=manifest_id,
            job_id=job_id,
            project_id=cast(str, snapshot["project_id"]),
            knowledge_base_id=cast(str, snapshot["knowledge_base_id"]),
            profile_revision_id=cast(str, snapshot["profile_revision_id"]),
            predecessor_index_revision_id=cast(
                str | None, snapshot["predecessor_index_revision_id"]
            ),
            target_index_revision_id=cast(
                str, snapshot["target_index_revision_id"]
            ),
            source_binding_digest=cast(str, snapshot["source_binding_digest"]),
            active_document_digest=cast(
                str, snapshot["active_document_digest"]
            ),
            source_document_count=cast(int, snapshot["source_document_count"]),
            source_size_bytes=cast(int, snapshot["source_size_bytes"]),
            profile_binding_identity=cast(
                str, requirements["profile_binding_identity"]
            ),
            operations=operations,
            authorization_id=authorization_id,
            budget_campaign_id=campaign_id,
            created_at=datetime.now(UTC).isoformat(),
            expires_at=approval.expires_at,
            approved_by_session_id=approved_by_session_id,
        )
        with self._connections.transaction(write=True) as db_connection:
            current = self._prospective_ingestion_snapshot(
                job_id, connection=db_connection
            )
            if self._ingestion_approval_identity(current) != (
                self._ingestion_approval_identity(snapshot)
            ):
                raise ValueError("入库任务资料已变化，请刷新后重新批准。")
            db_connection.execute(
                "INSERT INTO retrieval_ingestion_authorization_manifests("
                "manifest_id,job_id,project_id,knowledge_base_id,"
                "profile_revision_id,predecessor_index_revision_id,"
                "target_index_revision_id,source_binding_digest,"
                "active_document_digest,source_document_count,"
                "source_size_bytes,profile_binding_identity,operations_json,"
                "authorization_id,budget_campaign_id,created_at,expires_at,"
                "policy_revision,approved_by_session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    manifest.manifest_id,
                    manifest.job_id,
                    manifest.project_id,
                    manifest.knowledge_base_id,
                    manifest.profile_revision_id,
                    manifest.predecessor_index_revision_id,
                    manifest.target_index_revision_id,
                    manifest.source_binding_digest,
                    manifest.active_document_digest,
                    manifest.source_document_count,
                    manifest.source_size_bytes,
                    manifest.profile_binding_identity,
                    json.dumps(manifest.operations, separators=(",", ":")),
                    manifest.authorization_id,
                    manifest.budget_campaign_id,
                    manifest.created_at,
                    manifest.expires_at,
                    manifest.policy_revision,
                    manifest.approved_by_session_id,
                ),
            )
            if not self._clear_ingestion_required_action(db_connection, job_id):
                raise ValueError(
                    "入库任务状态已变化，授权没有应用，请刷新后重试。"
                )
        return self.ingestion_status(job_id)

    def ingestion_status(
        self, job_id: str
    ) -> RetrievalIngestionAuthorizationStatus:
        """读取待入库 Job 的精确出网批准与预算状态。

        Args:
            job_id: 目标持久 Job。

        Returns:
            不解析原文、不调用 Provider 的动态授权状态。

        """
        return self._ingestion_status(
            self._prospective_ingestion_snapshot(job_id)
        )

    @contextmanager
    def ingestion_scope(
        self,
        profile_revision_id: str,
        request: QueuedIngestion,
    ) -> Iterator[None]:
        """在 Worker 刷新快照后绑定同一 Job 的预算与数据范围。

        Args:
            profile_revision_id: Worker 实际冻结的 Profile。
            request: 已刷新并持久化的完整入库请求。

        Yields:
            与 Job、目标 Revision、资料和 Profile 完全一致的调用范围。

        Raises:
            PolicyDenied: 授权缺失、失效或任何最终绑定发生漂移。

        """
        current = self._prospective_ingestion_snapshot(request.job_id)
        refreshed = self._ingestion_snapshot_from_request(request)
        if profile_revision_id != current["profile_revision_id"] or (
            self._ingestion_snapshot_identity(current)
            != self._ingestion_snapshot_identity(refreshed)
        ):
            raise self._ingestion_blocked("RETRIEVAL_INGESTION_JOB_CHANGED")
        status = self._ingestion_status(current)
        manifest = status.manifest
        if (
            status.authorization_state != "APPROVED"
            or status.budget_state != "AVAILABLE"
            or status.connection_budget_state != "READY"
            or manifest is None
        ):
            reason = self._ingestion_reason(status)
            raise self._ingestion_blocked(reason)
        requirements = cast(Mapping[str, object], current["requirements"])
        campaign_reason = self._campaign_binding_reason(
            manifest, current, requirements
        )
        if campaign_reason is not None:
            raise self._ingestion_blocked(
                "RETRIEVAL_INGESTION_CAMPAIGN_MISMATCH"
                if campaign_reason
                == "RETRIEVAL_AUTHORIZATION_CAMPAIGN_MISMATCH"
                else "RETRIEVAL_INGESTION_BUDGET_UNAVAILABLE"
            )
        try:
            ledger = ProviderBudgetLedger(self._ledger_path)
            campaign = ledger.campaign(manifest.budget_campaign_id)
        except (
            BudgetBlockedError,
            OSError,
            ValueError,
            sqlite3.Error,
        ) as error:
            raise self._ingestion_blocked(
                "RETRIEVAL_INGESTION_BUDGET_UNAVAILABLE"
            ) from error
        effective_sources = cast(tuple[str, ...], current["source_hashes"])
        if set(effective_sources) != {
            value.removeprefix("sha256:")
            for value in campaign.approved_source_hashes
        }:
            raise self._ingestion_blocked("RETRIEVAL_INGESTION_SOURCE_CHANGED")
        try:
            with (
                provider_budget_scope(
                    ledger,
                    campaign_id=manifest.budget_campaign_id,
                    authorization_id=manifest.authorization_id,
                    scope=campaign.scope,
                    step_id="retrieval.build",
                ),
                provider_data_scope(
                    project_id=manifest.project_id,
                    knowledge_base_id=manifest.knowledge_base_id,
                    source_hashes=effective_sources,
                ),
            ):
                yield
        except BudgetBlockedError as error:
            raise self._ingestion_blocked(
                "RETRIEVAL_INGESTION_BUDGET_BLOCKED"
            ) from error

    def status(  # noqa: PLR0911
        self, profile_revision_id: str
    ) -> RetrievalAuthorizationStatus:
        """重新核对方案、当前活动文档、连接预算和累计账本。

        Args:
            profile_revision_id: 待核对的检索方案修订标识。

        Returns:
            当前授权、连接预算与累计账本的联合状态。

        """
        profile = self._control.get_profile(profile_revision_id)
        try:
            snapshot = self._snapshot(profile.knowledge_base_id)
            requirements = self._requirements(profile, snapshot)
        except (RagError, ValueError, KeyError):
            return RetrievalAuthorizationStatus(
                authorization_state="BLOCKED",
                budget_state="BLOCKED",
                connection_budget_state="BLOCKED",
                required_operations=("embedding.document", "embedding.query"),
                estimated_document_chunks=0,
                estimated_document_requests_per_slot=0,
                estimated_document_tokens_per_slot=0,
                embedding_slot_count=1,
                reason_codes=("RETRIEVAL_CONFIGURATION_INVALID",),
            )
        budget_issues = cast(tuple[str, ...], requirements["budget_issues"])
        if snapshot["active_document_count"] == 0:
            return self._status_result(
                requirements,
                authorization_state="NOT_REQUIRED",
                budget_state="AVAILABLE",
                connection_budget_state="READY",
            )
        manifest = self._matching_authorization_manifest(
            profile_revision_id, snapshot, requirements
        )
        if manifest is None:
            manifest = self._latest_authorization_manifest(profile_revision_id)
        if manifest is None:
            return self._status_result(
                requirements,
                authorization_state="MISSING",
                budget_state="MISSING",
                connection_budget_state=(
                    "INSUFFICIENT" if budget_issues else "READY"
                ),
                reason_codes=budget_issues
                or ("RETRIEVAL_AUTHORIZATION_MISSING",),
            )
        if datetime.fromisoformat(manifest.expires_at) <= datetime.now(UTC):
            return self._status_result(
                requirements,
                authorization_state="EXPIRED",
                budget_state="BLOCKED",
                connection_budget_state=(
                    "INSUFFICIENT" if budget_issues else "READY"
                ),
                manifest=manifest,
                reason_codes=("RETRIEVAL_AUTHORIZATION_EXPIRED",),
            )
        if (
            manifest.active_document_digest
            != snapshot["active_document_digest"]
            or manifest.active_document_count
            != snapshot["active_document_count"]
            or manifest.source_index_revision_id
            != snapshot["active_index_revision_id"]
            or not snapshot["index_aligned"]
            or (
                isinstance(manifest, RetrievalIngestionAuthorizationManifest)
                and (
                    manifest.target_index_revision_id
                    != snapshot["active_index_revision_id"]
                    or manifest.source_binding_digest
                    != snapshot["source_binding_digest"]
                )
            )
        ):
            return self._status_result(
                requirements,
                authorization_state="STALE_CORPUS",
                budget_state="BLOCKED",
                connection_budget_state=(
                    "INSUFFICIENT" if budget_issues else "READY"
                ),
                manifest=manifest,
                reason_codes=("RETRIEVAL_AUTHORIZATION_STALE_CORPUS",),
            )
        if (
            manifest.profile_binding_identity
            != requirements["profile_binding_identity"]
        ):
            return self._status_result(
                requirements,
                authorization_state="STALE_PROFILE",
                budget_state="BLOCKED",
                connection_budget_state=(
                    "INSUFFICIENT" if budget_issues else "READY"
                ),
                manifest=manifest,
                reason_codes=("RETRIEVAL_PROFILE_BINDING_CHANGED",),
            )
        if budget_issues:
            return self._status_result(
                requirements,
                authorization_state="BLOCKED",
                budget_state="BLOCKED",
                connection_budget_state="INSUFFICIENT",
                manifest=manifest,
                reason_codes=budget_issues,
            )
        campaign_reason = self._campaign_binding_reason(
            manifest, snapshot, requirements
        )
        if campaign_reason is not None:
            return self._status_result(
                requirements,
                authorization_state="BLOCKED",
                budget_state="BLOCKED",
                connection_budget_state="READY",
                manifest=manifest,
                reason_codes=(campaign_reason,),
            )
        budget_state, reason = self._budget_state(manifest)
        return self._status_result(
            requirements,
            authorization_state="APPROVED",
            budget_state=budget_state,
            connection_budget_state="READY",
            manifest=manifest,
            reason_codes=() if reason is None else (reason,),
        )

    @contextmanager
    def scope(
        self,
        profile_revision_id: str,
        *,
        step_id: str,
        required_operations: tuple[RetrievalOperation, ...],
        source_hashes: tuple[str, ...] | None = None,
        expected_index_revision_id: str | None = None,
    ) -> Iterator[None]:
        """为一条真实构建或查询调用链绑定已对账的检索批准。

        Args:
            profile_revision_id: 当前构建或查询实际冻结的 Profile。
            step_id: 持久预算账本中的阶段身份。
            required_operations: 本调用链可能使用的远程检索用途。
            source_hashes: 后台构建请求冻结的原件哈希。
            expected_index_revision_id: 查询在 Provider 前冻结的 Revision。

        Yields:
            与当前资料、Profile 和 Revision 一致的预算与数据范围。

        Returns:
            上下文退出后无返回值。

        Raises:
            BudgetBlockedError: 任一授权身份、预算或数据范围不再匹配。

        """
        status = self.status(profile_revision_id)
        public_manifest = status.manifest
        if (
            status.authorization_state != "APPROVED"
            or status.budget_state == "BLOCKED"
            or status.connection_budget_state != "READY"
            or public_manifest is None
        ):
            reason = (
                status.reason_codes[0]
                if status.reason_codes
                else "RETRIEVAL_AUTHORIZATION_REQUIRED"
            )
            raise BudgetBlockedError(reason)
        try:
            profile = self._control.get_profile(profile_revision_id)
            snapshot = self._snapshot(profile.knowledge_base_id)
            requirements = self._requirements(profile, snapshot)
        except (RagError, ValueError, KeyError) as error:
            raise BudgetBlockedError(
                "RETRIEVAL_CONFIGURATION_INVALID"
            ) from error
        manifest = self._matching_authorization_manifest(
            profile_revision_id, snapshot, requirements
        )
        if manifest is None:
            raise BudgetBlockedError("RETRIEVAL_AUTHORIZATION_REQUIRED")
        if not set(required_operations) <= set(manifest.operations):
            raise BudgetBlockedError("RETRIEVAL_OPERATION_NOT_APPROVED")
        operation_budget_state, operation_budget_reason = self._budget_state(
            manifest,
            required_operations=required_operations,
        )
        if operation_budget_state != "AVAILABLE":
            raise BudgetBlockedError(
                operation_budget_reason or "RETRIEVAL_BUDGET_EXHAUSTED"
            )
        campaign_reason = self._campaign_binding_reason(
            manifest, snapshot, requirements
        )
        if campaign_reason is not None:
            raise BudgetBlockedError(campaign_reason)
        ledger = ProviderBudgetLedger(self._ledger_path)
        campaign = ledger.campaign(manifest.budget_campaign_id)
        approved_sources = {
            value.removeprefix("sha256:")
            for value in campaign.approved_source_hashes
        }
        if expected_index_revision_id is not None:
            if (
                snapshot["active_index_revision_id"]
                != expected_index_revision_id
                or manifest.source_index_revision_id
                != expected_index_revision_id
                or snapshot["active_document_digest"]
                != manifest.active_document_digest
                or snapshot["active_document_count"]
                != manifest.active_document_count
                or not snapshot["index_aligned"]
            ):
                raise BudgetBlockedError("RETRIEVAL_REVISION_CHANGED")
            effective_sources = cast(tuple[str, ...], snapshot["source_hashes"])
        else:
            effective_sources = (
                tuple(sorted(approved_sources))
                if source_hashes is None
                else tuple(
                    sorted(
                        value.removeprefix("sha256:") for value in source_hashes
                    )
                )
            )
        if not set(effective_sources) <= approved_sources:
            raise BudgetBlockedError("RETRIEVAL_SOURCE_NOT_APPROVED")
        with (
            provider_budget_scope(
                ledger,
                campaign_id=manifest.budget_campaign_id,
                authorization_id=manifest.authorization_id,
                scope=campaign.scope,
                step_id=step_id,
            ),
            provider_data_scope(
                project_id=manifest.project_id,
                knowledge_base_id=manifest.knowledge_base_id,
                source_hashes=tuple(effective_sources),
            ),
        ):
            yield

    def _ingestion_status(  # noqa: PLR0911
        self, snapshot: Mapping[str, object]
    ) -> RetrievalIngestionAuthorizationStatus:
        """按一个服务端导出的 Job 快照投影授权状态。"""
        requirements = cast(dict[str, object], snapshot["requirements"])
        manifest = self._latest_ingestion_manifest(
            cast(str, snapshot["job_id"])
        )
        base = {
            "job_id": cast(str, snapshot["job_id"]),
            "profile_revision_id": cast(str, snapshot["profile_revision_id"]),
            "predecessor_index_revision_id": cast(
                str | None, snapshot["predecessor_index_revision_id"]
            ),
            "target_index_revision_id": cast(
                str, snapshot["target_index_revision_id"]
            ),
            "source_document_count": cast(
                int, snapshot["source_document_count"]
            ),
            "source_size_bytes": cast(int, snapshot["source_size_bytes"]),
            "embedding_slot_count": cast(
                int, requirements["embedding_slot_count"]
            ),
            "required_operations": cast(
                tuple[RetrievalOperation, ...], requirements["operations"]
            ),
            "recommended_request_limit": cast(
                int, requirements["recommended_request_limit"]
            ),
            "recommended_estimated_token_limit": cast(
                int, requirements["token_limit_cap"]
            ),
            "recommended_operation_request_limits": cast(
                dict[RetrievalOperation, StrictInt],
                requirements["recommended_operation_request_limits"],
            ),
        }
        active_profile_id = snapshot["active_profile_revision_id"]
        if active_profile_id != snapshot["profile_revision_id"]:
            return RetrievalIngestionAuthorizationStatus(
                **base,
                authorization_state="STALE_PROFILE",
                budget_state="BLOCKED",
                connection_budget_state="BLOCKED",
                next_action="review_document",
                approval_allowed=False,
                manifest=manifest,
                reason_codes=("RETRIEVAL_INGESTION_PROFILE_CHANGED",),
            )
        if not snapshot["target_is_current_candidate"]:
            return RetrievalIngestionAuthorizationStatus(
                **base,
                authorization_state="STALE_JOB",
                budget_state="BLOCKED",
                connection_budget_state="READY",
                next_action="review_document",
                approval_allowed=False,
                manifest=manifest,
                reason_codes=("RETRIEVAL_INGESTION_TARGET_VERSION_SUPERSEDED",),
            )
        validation_issues = cast(
            tuple[str, ...], snapshot["profile_validation_issues"]
        )
        if validation_issues or not requirements["configuration_available"]:
            return RetrievalIngestionAuthorizationStatus(
                **base,
                authorization_state="BLOCKED",
                budget_state="BLOCKED",
                connection_budget_state="BLOCKED",
                next_action="repair_profile",
                approval_allowed=False,
                manifest=manifest,
                reason_codes=("RETRIEVAL_INGESTION_PROFILE_INVALID",),
            )
        if manifest is None:
            return RetrievalIngestionAuthorizationStatus(
                **base,
                authorization_state="MISSING",
                budget_state="MISSING",
                connection_budget_state="READY",
                next_action="approve",
                approval_allowed=True,
                reason_codes=("RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED",),
            )
        if datetime.fromisoformat(manifest.expires_at) <= datetime.now(UTC):
            return RetrievalIngestionAuthorizationStatus(
                **base,
                authorization_state="EXPIRED",
                budget_state="BLOCKED",
                connection_budget_state="READY",
                next_action="reauthorize",
                approval_allowed=True,
                manifest=manifest,
                reason_codes=("RETRIEVAL_INGESTION_AUTHORIZATION_EXPIRED",),
            )
        if (
            manifest.project_id != snapshot["project_id"]
            or manifest.knowledge_base_id != snapshot["knowledge_base_id"]
            or manifest.profile_revision_id != snapshot["profile_revision_id"]
            or manifest.predecessor_index_revision_id
            != snapshot["predecessor_index_revision_id"]
            or manifest.target_index_revision_id
            != snapshot["target_index_revision_id"]
            or manifest.source_binding_digest
            != snapshot["source_binding_digest"]
            or manifest.active_document_digest
            != snapshot["active_document_digest"]
            or manifest.source_document_count
            != snapshot["source_document_count"]
            or manifest.source_size_bytes != snapshot["source_size_bytes"]
        ):
            return RetrievalIngestionAuthorizationStatus(
                **base,
                authorization_state="STALE_JOB",
                budget_state="BLOCKED",
                connection_budget_state="READY",
                next_action="reauthorize",
                approval_allowed=True,
                manifest=manifest,
                reason_codes=("RETRIEVAL_INGESTION_JOB_CHANGED",),
            )
        if manifest.profile_binding_identity != requirements[
            "profile_binding_identity"
        ] or set(manifest.operations) != set(requirements["operations"]):
            return RetrievalIngestionAuthorizationStatus(
                **base,
                authorization_state="STALE_PROFILE",
                budget_state="BLOCKED",
                connection_budget_state="READY",
                next_action="reauthorize",
                approval_allowed=True,
                manifest=manifest,
                reason_codes=("RETRIEVAL_INGESTION_PROFILE_CHANGED",),
            )
        campaign_reason = self._campaign_binding_reason(
            manifest, snapshot, requirements
        )
        if campaign_reason is not None:
            ingestion_campaign_reason = (
                "RETRIEVAL_INGESTION_CAMPAIGN_MISMATCH"
                if campaign_reason
                == "RETRIEVAL_AUTHORIZATION_CAMPAIGN_MISMATCH"
                else "RETRIEVAL_INGESTION_BUDGET_UNAVAILABLE"
            )
            return RetrievalIngestionAuthorizationStatus(
                **base,
                authorization_state="BLOCKED",
                budget_state="BLOCKED",
                connection_budget_state="READY",
                next_action="reauthorize",
                approval_allowed=True,
                manifest=manifest,
                reason_codes=(ingestion_campaign_reason,),
            )
        budget_state, reason = self._budget_state(
            manifest,
            required_operations=("embedding.document",),
        )
        return RetrievalIngestionAuthorizationStatus(
            **base,
            authorization_state="APPROVED",
            budget_state=budget_state,
            connection_budget_state="READY",
            next_action=(
                "continue" if budget_state == "AVAILABLE" else "reauthorize"
            ),
            approval_allowed=budget_state != "AVAILABLE",
            manifest=manifest,
            reason_codes=() if reason is None else (reason,),
        )

    def _prospective_ingestion_snapshot(
        self,
        job_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        """以当前 Active 文档加目标版本推导下一次 Worker 刷新结果。"""
        transaction = (
            self._connections.transaction()
            if connection is None
            else nullcontext(connection)
        )
        with transaction as active_connection:
            row = active_connection.execute(
                "SELECT j.project_id,j.knowledge_base_id,j.document_id,"
                "j.document_version_id,j.revision_id,"
                "j.state AS job_state,j.error_code,j.cancel_requested,"
                "r.state AS request_state,r.request_json,k.active_revision_id "
                "FROM ingestion_jobs j JOIN ingestion_requests r "
                "ON r.job_id=j.job_id JOIN knowledge_bases k "
                "ON k.knowledge_base_id=j.knowledge_base_id "
                "WHERE j.job_id=? AND k.deleted_at IS NULL",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError("入库任务不存在或已离开有效知识库。")
            request = QueuedIngestion.model_validate_json(
                str(row["request_json"])
            )
            if (
                request.job_id != job_id
                or request.revision_id != row["revision_id"]
                or request.target_document_id != row["document_id"]
                or request.target_document_version_id
                != row["document_version_id"]
            ):
                raise ValueError("入库任务公开身份与持久请求不一致。")
            if request.activate_profile:
                raise ValueError("Profile 重建任务沿用活动语料授权流程。")
            target = next(
                (
                    item
                    for item in request.documents
                    if item.document.document_id == request.target_document_id
                ),
                None,
            )
            if target is None:
                raise ValueError("入库任务缺少目标文档版本。")
            target_row = active_connection.execute(
                "SELECT d.project_id,d.knowledge_base_id,d.status,"
                "d.deleted_at,d.current_version_id,v.document_version_id,"
                "v.content_sha256,"
                "v.source_artifact_id,v.size_bytes,v.media_type "
                "FROM documents d JOIN document_versions v "
                "ON v.document_id=d.document_id WHERE d.document_id=? "
                "AND v.document_version_id=?",
                (
                    request.target_document_id,
                    request.target_document_version_id,
                ),
            ).fetchone()
            if (
                target_row is None
                or target_row["project_id"] != row["project_id"]
                or target_row["knowledge_base_id"] != row["knowledge_base_id"]
                or target.document.project_id != row["project_id"]
                or target.document.knowledge_base_id != row["knowledge_base_id"]
                or target_row["status"] != "active"
                or target_row["deleted_at"] is not None
                or target_row["content_sha256"] != target.content_sha256
                or target_row["source_artifact_id"] != target.artifact_id
                or int(target_row["size_bytes"]) != target.size_bytes
                or target_row["media_type"] != target.media_type
            ):
                raise ValueError("入库任务目标文档绑定已经失效。")
            active_rows = active_connection.execute(
                "SELECT d.document_id,v.document_version_id,"
                "v.content_sha256,v.size_bytes FROM documents d "
                "JOIN document_versions v "
                "ON v.document_version_id=d.current_version_id "
                "WHERE d.knowledge_base_id=? AND d.status='active' "
                "AND d.deleted_at IS NULL AND d.document_id<>? "
                "ORDER BY d.document_id",
                (row["knowledge_base_id"], request.target_document_id),
            ).fetchall()
            expected_target_row = (
                None
                if request.initial_index_revision_id is None
                else active_connection.execute(
                    "SELECT document_version_id FROM revision_documents "
                    "WHERE revision_id=? AND document_id=?",
                    (
                        request.initial_index_revision_id,
                        request.target_document_id,
                    ),
                ).fetchone()
            )
            active_target_row = (
                None
                if row["active_revision_id"] is None
                else active_connection.execute(
                    "SELECT document_version_id FROM revision_documents "
                    "WHERE revision_id=? AND document_id=?",
                    (row["active_revision_id"], request.target_document_id),
                ).fetchone()
            )
            expected_target_version_id = (
                None
                if expected_target_row is None
                else str(expected_target_row["document_version_id"])
            )
            active_target_version_id = (
                None
                if active_target_row is None
                else str(active_target_row["document_version_id"])
            )
            current_target_version_id = (
                None
                if target_row["current_version_id"] is None
                else str(target_row["current_version_id"])
            )
            target_is_current_candidate = active_target_version_id in {
                None,
                request.target_document_version_id,
                expected_target_version_id,
            } and current_target_version_id in {
                None,
                request.target_document_version_id,
                active_target_version_id,
            }
        documents = [
            {
                "document_id": str(item["document_id"]),
                "document_version_id": str(item["document_version_id"]),
                "content_sha256": str(item["content_sha256"]),
                "size_bytes": int(item["size_bytes"]),
            }
            for item in active_rows
        ]
        documents.append(
            {
                "document_id": request.target_document_id,
                "document_version_id": request.target_document_version_id,
                "content_sha256": target.content_sha256,
                "size_bytes": target.size_bytes,
            }
        )
        profile_id = request.retrieval_profile_revision_id
        if profile_id is None:
            raise ValueError("入库任务没有冻结远程 Retrieval Profile。")
        profile = self._control.get_profile(profile_id)
        snapshot = self._ingestion_snapshot_values(
            request=request,
            project_id=str(row["project_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            predecessor_index_revision_id=(
                None
                if row["active_revision_id"] is None
                else str(row["active_revision_id"])
            ),
            documents=documents,
            profile=profile,
        )
        snapshot.update(
            {
                "job_state": str(row["job_state"]),
                "request_state": str(row["request_state"]),
                "error_code": (
                    None
                    if row["error_code"] is None
                    else str(row["error_code"])
                ),
                "cancel_requested": bool(row["cancel_requested"]),
                "target_document_version_id": (
                    request.target_document_version_id
                ),
                "active_target_document_version_id": (active_target_version_id),
                "target_is_current_candidate": target_is_current_candidate,
            }
        )
        return snapshot

    def _ingestion_snapshot_from_request(
        self, request: QueuedIngestion
    ) -> dict[str, object]:
        """把 Worker 已刷新请求投影为最终授权比较所需的无正文身份。"""
        target = next(
            item
            for item in request.documents
            if item.document.document_id == request.target_document_id
        )
        profile_id = request.retrieval_profile_revision_id
        if profile_id is None:
            raise ValueError("入库任务没有冻结远程 Retrieval Profile。")
        profile = self._control.get_profile(profile_id)
        return self._ingestion_snapshot_values(
            request=request,
            project_id=target.document.project_id,
            knowledge_base_id=target.document.knowledge_base_id,
            predecessor_index_revision_id=request.expected_index_revision_id,
            documents=[
                {
                    "document_id": item.document.document_id,
                    "document_version_id": (
                        request.target_document_version_id
                        if item.document.document_id
                        == request.target_document_id
                        else deterministic_id(
                            "dver",
                            item.document.document_id,
                            item.content_sha256,
                        )
                    ),
                    "content_sha256": item.content_sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in request.documents
            ],
            profile=profile,
            target_index_revision_id=request.revision_id,
        )

    def _ingestion_snapshot_values(  # noqa: PLR0913
        self,
        *,
        request: QueuedIngestion,
        project_id: str,
        knowledge_base_id: str,
        predecessor_index_revision_id: str | None,
        documents: list[dict[str, object]],
        profile: RetrievalProfileRevision,
        target_index_revision_id: str | None = None,
    ) -> dict[str, object]:
        """从可信 DB/Worker 字段形成稳定的首次入库授权身份。"""
        ordered = sorted(documents, key=lambda item: str(item["document_id"]))
        version_ids = tuple(
            sorted(str(item["document_version_id"]) for item in ordered)
        )
        content_fingerprint = (
            canonical_sha256(
                {
                    "index": profile.index_semantic_fingerprint,
                    "content": request.content_identity,
                }
            )
            if request.content_identity is not None
            else profile.index_semantic_fingerprint
        )
        hashes = tuple(
            sorted({str(item["content_sha256"]) for item in ordered})
        )
        active = self._control.active_profile(knowledge_base_id)
        validation_issues = tuple(
            self._control.profile_validation_issues(profile.profile_revision_id)
        )
        try:
            requirements = self._ingestion_requirements(profile)
        except (RagError, ValueError, KeyError):
            requirements = self._unavailable_ingestion_requirements(profile)
            if not validation_issues:
                validation_issues = ("retrieval-profile:configuration",)
        return {
            "job_id": request.job_id,
            "project_id": project_id,
            "knowledge_base_id": knowledge_base_id,
            "profile_revision_id": profile.profile_revision_id,
            "active_profile_revision_id": (
                None if active is None else active.profile_revision_id
            ),
            "profile_validation_issues": validation_issues,
            "predecessor_index_revision_id": predecessor_index_revision_id,
            "target_index_revision_id": (
                target_index_revision_id
                or deterministic_id(
                    "irev",
                    knowledge_base_id,
                    version_ids,
                    content_fingerprint,
                )
            ),
            "source_binding_digest": canonical_sha256(
                {
                    "documents": [
                        {
                            "document_id": item["document_id"],
                            "document_version_id": item["document_version_id"],
                            "content_sha256": item["content_sha256"],
                        }
                        for item in ordered
                    ]
                }
            ),
            "active_document_digest": canonical_sha256(
                {"active_document_content_hashes": hashes}
            ),
            "source_hashes": hashes,
            "source_document_count": len(ordered),
            "source_size_bytes": sum(
                int(item["size_bytes"]) for item in ordered
            ),
            "requirements": requirements,
        }

    def _ingestion_requirements(
        self, profile: RetrievalProfileRevision
    ) -> dict[str, object]:
        """返回待构建资料可用的完整模型用途与连接硬上限。"""
        bindings: list[tuple[RetrievalOperation, str, str, str]] = []
        for connection_id, model in (
            (profile.primary_connection_id, profile.primary_embedding_model),
            (profile.standby_connection_id, profile.standby_embedding_model),
        ):
            if connection_id is None or model is None:
                continue
            bindings.extend(
                (
                    operation,
                    connection_id,
                    model,
                    self._providers.request_authorization_identity(
                        connection_id,
                        operation=operation,
                        model=model,
                    ),
                )
                for operation in ("embedding.document", "embedding.query")
            )
        if profile.reranker_connection_id is not None:
            if profile.reranker_model is None:
                raise ValueError("Reranker Connection 缺少模型。")
            bindings.append(
                (
                    "reranking",
                    profile.reranker_connection_id,
                    profile.reranker_model,
                    self._providers.request_authorization_identity(
                        profile.reranker_connection_id,
                        operation="reranking",
                        model=profile.reranker_model,
                    ),
                )
            )
        operations = tuple(dict.fromkeys(item[0] for item in bindings))
        provider_request_limits: dict[str, int] = {}
        provider_token_limits: dict[str, int] = {}
        for _, connection_id, _, _ in bindings:
            connection = self._control.get_connection(connection_id)
            provider = connection.provider_type.replace("-model-studio", "")
            provider_request_limits[provider] = min(
                provider_request_limits.get(
                    provider, connection.request_budget
                ),
                connection.request_budget,
            )
            provider_token_limits[provider] = min(
                provider_token_limits.get(provider, connection.token_budget),
                connection.token_budget,
            )
        request_cap = sum(provider_request_limits.values())
        token_cap = sum(provider_token_limits.values())
        if not operations or request_cap <= 0 or token_cap <= 0:
            raise ValueError("Retrieval Profile 没有可批准的远程连接。")
        operation_limits = cast(
            dict[RetrievalOperation, StrictInt],
            dict.fromkeys(operations, request_cap),
        )
        return {
            "operations": operations,
            "bindings": tuple(bindings),
            "embedding_slot_count": sum(
                item is not None
                for item in (
                    profile.primary_connection_id,
                    profile.standby_connection_id,
                )
            ),
            "profile_binding_identity": canonical_sha256(
                {
                    "profile_revision_id": profile.profile_revision_id,
                    "index_semantic_fingerprint": (
                        profile.index_semantic_fingerprint
                    ),
                    "bindings": [
                        {
                            "operation": operation,
                            "connection_id": connection_id,
                            "model": model,
                            "request_identity": request_identity,
                        }
                        for (
                            operation,
                            connection_id,
                            model,
                            request_identity,
                        ) in bindings
                    ],
                }
            ),
            "provider_request_limits": provider_request_limits,
            "provider_token_limits": provider_token_limits,
            "request_limit_cap": request_cap,
            "token_limit_cap": token_cap,
            "recommended_operation_request_limits": operation_limits,
            "recommended_request_limit": request_cap * len(operations),
            "configuration_available": True,
        }

    @staticmethod
    def _unavailable_ingestion_requirements(
        profile: RetrievalProfileRevision,
    ) -> dict[str, object]:
        """为不可用 Profile 返回零预算的只读状态投影。"""
        operations: tuple[RetrievalOperation, ...] = (
            "embedding.document",
            "embedding.query",
        )
        if (
            profile.reranker_connection_id is not None
            and profile.reranker_model is not None
        ):
            operations = (*operations, "reranking")
        embedding_slots = sum(
            connection_id is not None and model is not None
            for connection_id, model in (
                (
                    profile.primary_connection_id,
                    profile.primary_embedding_model,
                ),
                (
                    profile.standby_connection_id,
                    profile.standby_embedding_model,
                ),
            )
        )
        return {
            "operations": operations,
            "bindings": (),
            "embedding_slot_count": max(1, embedding_slots),
            "profile_binding_identity": canonical_sha256(
                {
                    "profile_revision_id": profile.profile_revision_id,
                    "index_semantic_fingerprint": (
                        profile.index_semantic_fingerprint
                    ),
                    "configuration_available": False,
                }
            ),
            "provider_request_limits": {},
            "provider_token_limits": {},
            "request_limit_cap": 0,
            "token_limit_cap": 0,
            "recommended_operation_request_limits": dict.fromkeys(
                operations, 0
            ),
            "recommended_request_limit": 0,
            "configuration_available": False,
        }

    @staticmethod
    def _ingestion_snapshot_identity(
        snapshot: Mapping[str, object],
    ) -> tuple[object, ...]:
        """返回 Worker 最终门需要完全相等的无正文身份。"""
        requirements = cast(Mapping[str, object], snapshot["requirements"])
        return (
            snapshot["job_id"],
            snapshot["project_id"],
            snapshot["knowledge_base_id"],
            snapshot["profile_revision_id"],
            snapshot["predecessor_index_revision_id"],
            snapshot["target_index_revision_id"],
            snapshot["source_binding_digest"],
            snapshot["active_document_digest"],
            snapshot["source_document_count"],
            snapshot["source_size_bytes"],
            snapshot["active_profile_revision_id"],
            snapshot["profile_validation_issues"],
            requirements["profile_binding_identity"],
        )

    @classmethod
    def _ingestion_approval_identity(
        cls,
        snapshot: Mapping[str, object],
    ) -> tuple[object, ...]:
        """把授权时必须稳定的 Job 状态加入最终 CAS 身份。"""
        return (
            *cls._ingestion_snapshot_identity(snapshot),
            snapshot["job_state"],
            snapshot["request_state"],
            snapshot["error_code"],
            snapshot["cancel_requested"],
            snapshot["target_document_version_id"],
            snapshot["active_target_document_version_id"],
            snapshot["target_is_current_candidate"],
        )

    @staticmethod
    def _ingestion_reason(
        status: RetrievalIngestionAuthorizationStatus,
    ) -> str:
        """把状态投影为公开 Job 的稳定可恢复错误码。"""
        if status.budget_state == "EXHAUSTED":
            return "RETRIEVAL_INGESTION_BUDGET_EXHAUSTED"
        if status.reason_codes:
            return status.reason_codes[0]
        return "RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED"

    @staticmethod
    def _ingestion_blocked(reason: str) -> PolicyDenied:
        """生成不会泄露正文且允许管理员修复后重试的 Job 错误。"""
        messages = {
            "RETRIEVAL_INGESTION_AUTHORIZATION_REQUIRED": (
                "此文档版本尚未获准发送给远程检索服务。请由管理员批准后重试同一任务。"
            ),
            "RETRIEVAL_INGESTION_AUTHORIZATION_EXPIRED": (
                "此入库任务的真实检索授权已到期，请重新批准。"
            ),
            "RETRIEVAL_INGESTION_BUDGET_EXHAUSTED": (
                "此入库任务的真实检索累计预算已用尽，请重新批准。"
            ),
            "RETRIEVAL_INGESTION_BUDGET_BLOCKED": (
                "真实检索请求在硬预算门禁前停止，请核对预算后重新批准。"
            ),
            "RETRIEVAL_INGESTION_BUDGET_UNAVAILABLE": (
                "真实检索预算账本当前不可用，请修复后重新批准。"
            ),
            "RETRIEVAL_INGESTION_CAMPAIGN_MISMATCH": (
                "入库任务的批准记录与预算账本不一致，请重新批准。"
            ),
            "RETRIEVAL_INGESTION_PROFILE_CHANGED": (
                "入库任务绑定的 Retrieval Profile 已变化，请重新批准。"
            ),
            "RETRIEVAL_INGESTION_PROFILE_INVALID": (
                "入库任务绑定的 Retrieval Profile 尚未就绪，请修复后重新批准。"
            ),
            "RETRIEVAL_INGESTION_TARGET_VERSION_SUPERSEDED": (
                "此任务的文档版本已被更新版本取代，不会回退当前文档。"
            ),
            "RETRIEVAL_INGESTION_SOURCE_CHANGED": (
                "入库任务的资料集合已变化，请刷新后重新批准。"
            ),
            "RETRIEVAL_INGESTION_JOB_CHANGED": (
                "入库任务的资料或目标 Revision 已变化，请刷新后重新批准。"
            ),
        }
        return PolicyDenied(
            messages.get(reason, "入库任务的真实检索授权不可用，请重新批准。"),
            stage="retrieval.ingestion_authorization",
            code=reason,
            retryable=True,
        )

    def _requirements(
        self,
        profile: RetrievalProfileRevision,
        snapshot: Mapping[str, object],
    ) -> dict[str, object]:
        bindings: list[tuple[RetrievalOperation, str, str, str]] = []
        embedding_operations: tuple[RetrievalOperation, ...] = (
            "embedding.document",
            "embedding.query",
        )
        for connection_id, model in (
            (profile.primary_connection_id, profile.primary_embedding_model),
            (profile.standby_connection_id, profile.standby_embedding_model),
        ):
            if connection_id is None or model is None:
                continue
            bindings.extend(
                (
                    operation,
                    connection_id,
                    model,
                    self._providers.request_authorization_identity(
                        connection_id,
                        operation=operation,
                        model=model,
                    ),
                )
                for operation in embedding_operations
            )
        if profile.reranker_connection_id is not None:
            if profile.reranker_model is None:
                raise ValueError("Reranker Connection 缺少模型。")
            bindings.append(
                (
                    "reranking",
                    profile.reranker_connection_id,
                    profile.reranker_model,
                    self._providers.request_authorization_identity(
                        profile.reranker_connection_id,
                        operation="reranking",
                        model=profile.reranker_model,
                    ),
                )
            )
        operations: tuple[RetrievalOperation, ...] = tuple(
            dict.fromkeys(binding[0] for binding in bindings)
        )
        chunks = cast(tuple[str, ...], snapshot["embedding_texts"])
        unique_chunks = tuple(dict.fromkeys(chunks))
        requests_per_slot = 0
        if unique_chunks:
            requests_per_slot = len(
                batch_texts(
                    unique_chunks,
                    BatchLimits(
                        max_items=_DOCUMENT_PROVIDER_BATCH_ITEMS,
                        max_input_tokens=32_768,
                    ),
                )
            )
        tokens_per_slot = sum(
            estimate_provider_input_tokens(text) for text in unique_chunks
        )
        embedding_connections = tuple(
            item
            for item in (
                profile.primary_connection_id,
                profile.standby_connection_id,
            )
            if item is not None
        )
        issues: list[str] = []
        for connection_id in embedding_connections:
            connection = self._control.get_connection(connection_id)
            if connection.request_budget < requests_per_slot:
                issues.append(
                    "RETRIEVAL_CONNECTION_REQUEST_BUDGET_TOO_LOW:"
                    f"{requests_per_slot}"
                )
            if connection.token_budget < tokens_per_slot:
                issues.append(
                    "RETRIEVAL_CONNECTION_TOKEN_BUDGET_TOO_LOW:"
                    f"{tokens_per_slot}"
                )
        binding_identity = canonical_sha256(
            {
                "profile_revision_id": profile.profile_revision_id,
                "index_semantic_fingerprint": (
                    profile.index_semantic_fingerprint
                ),
                "bindings": [
                    {
                        "operation": operation,
                        "connection_id": connection_id,
                        "model": model,
                        "request_identity": request_identity,
                    }
                    for operation, connection_id, model, request_identity in (
                        bindings
                    )
                ],
            }
        )
        return {
            "operations": operations,
            "bindings": tuple(bindings),
            "profile_binding_identity": binding_identity,
            "document_chunks": len(chunks),
            "requests_per_slot": requests_per_slot,
            "tokens_per_slot": tokens_per_slot,
            "embedding_slot_count": len(embedding_connections),
            "document_requests_total": requests_per_slot
            * len(embedding_connections),
            "document_tokens_total": tokens_per_slot
            * len(embedding_connections),
            "budget_issues": tuple(dict.fromkeys(issues)),
        }

    @staticmethod
    def _status_result(  # noqa: PLR0913
        requirements: Mapping[str, object],
        *,
        authorization_state: RetrievalAuthorizationState,
        budget_state: RetrievalBudgetState,
        connection_budget_state: ConnectionBudgetState,
        manifest: (
            RetrievalAuthorizationManifest
            | RetrievalIngestionAuthorizationManifest
            | None
        ) = None,
        reason_codes: tuple[str, ...] = (),
    ) -> RetrievalAuthorizationStatus:
        """以显式类型把内部需求投影为公开状态。"""
        return RetrievalAuthorizationStatus(
            authorization_state=authorization_state,
            budget_state=budget_state,
            connection_budget_state=connection_budget_state,
            required_operations=cast(
                tuple[RetrievalOperation, ...], requirements["operations"]
            ),
            estimated_document_chunks=cast(
                int, requirements["document_chunks"]
            ),
            estimated_document_requests_per_slot=cast(
                int, requirements["requests_per_slot"]
            ),
            estimated_document_tokens_per_slot=cast(
                int, requirements["tokens_per_slot"]
            ),
            embedding_slot_count=cast(
                int, requirements["embedding_slot_count"]
            ),
            manifest=RetrievalAuthorizationStore._public_manifest(manifest),
            reason_codes=reason_codes,
        )

    @staticmethod
    def _public_manifest(
        manifest: (
            RetrievalAuthorizationManifest
            | RetrievalIngestionAuthorizationManifest
            | None
        ),
    ) -> RetrievalAuthorizationManifest | None:
        """把 Job 授权投影为既有活动语料授权的稳定公共形状。"""
        if manifest is None or isinstance(
            manifest, RetrievalAuthorizationManifest
        ):
            return manifest
        projected_id = canonical_sha256(
            {"ingestion_manifest_id": manifest.manifest_id}
        ).removeprefix("sha256:")
        return RetrievalAuthorizationManifest(
            manifest_id=f"rauth_{projected_id[:32]}",
            project_id=manifest.project_id,
            knowledge_base_id=manifest.knowledge_base_id,
            profile_revision_id=manifest.profile_revision_id,
            source_index_revision_id=manifest.target_index_revision_id,
            active_document_digest=manifest.active_document_digest,
            active_document_count=manifest.source_document_count,
            profile_binding_identity=manifest.profile_binding_identity,
            operations=manifest.operations,
            authorization_id=manifest.authorization_id,
            budget_campaign_id=manifest.budget_campaign_id,
            created_at=manifest.created_at,
            expires_at=manifest.expires_at,
            policy_revision=manifest.policy_revision,
            approved_by_session_id=manifest.approved_by_session_id,
        )

    def _snapshot(self, knowledge_base_id: str) -> dict[str, object]:
        with self._connections.transaction() as connection:
            knowledge_base = connection.execute(
                "SELECT project_id,active_revision_id FROM knowledge_bases "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
            if knowledge_base is None:
                raise ValueError("知识库不存在或已删除。")
            if knowledge_base["active_revision_id"] is None:
                active_document = connection.execute(
                    "SELECT 1 FROM documents WHERE knowledge_base_id=? "
                    "AND status='active' AND deleted_at IS NULL LIMIT 1",
                    (knowledge_base_id,),
                ).fetchone()
                if active_document is not None:
                    raise ValueError("知识库尚无活动 Index Revision。")
                return {
                    "project_id": str(knowledge_base["project_id"]),
                    "active_index_revision_id": None,
                    "active_document_count": 0,
                    "active_document_digest": canonical_sha256(
                        {"active_document_content_hashes": ()}
                    ),
                    "source_binding_digest": canonical_sha256(
                        {"documents": ()}
                    ),
                    "source_hashes": (),
                    "index_aligned": True,
                    "embedding_texts": (),
                }
            revision_id = str(knowledge_base["active_revision_id"])
            revision = connection.execute(
                "SELECT state FROM index_revisions WHERE index_revision_id=? "
                "AND knowledge_base_id=?",
                (revision_id, knowledge_base_id),
            ).fetchone()
            if revision is None or revision["state"] != "active":
                raise ValueError("知识库活动 Index Revision 不可用。")
            rows = connection.execute(
                "SELECT d.document_id,v.document_version_id,v.content_sha256,"
                "rd.document_version_id AS indexed_version_id "
                "FROM documents d JOIN document_versions v "
                "ON v.document_version_id=d.current_version_id "
                "LEFT JOIN revision_documents rd ON rd.revision_id=? "
                "AND rd.document_id=d.document_id "
                "WHERE d.knowledge_base_id=? AND d.status='active' "
                "AND d.deleted_at IS NULL ORDER BY d.document_id",
                (revision_id, knowledge_base_id),
            ).fetchall()
            embedding_texts = tuple(
                str(row["embedding_text"])
                for row in connection.execute(
                    "SELECT embedding_text FROM chunks WHERE revision_id=? "
                    "ORDER BY row_id",
                    (revision_id,),
                ).fetchall()
            )
        hashes = tuple(sorted({str(row["content_sha256"]) for row in rows}))
        return {
            "project_id": str(knowledge_base["project_id"]),
            "active_index_revision_id": revision_id,
            "active_document_count": len(rows),
            "active_document_digest": canonical_sha256(
                {"active_document_content_hashes": hashes}
            ),
            "source_binding_digest": canonical_sha256(
                {
                    "documents": [
                        {
                            "document_id": str(row["document_id"]),
                            "document_version_id": str(
                                row["document_version_id"]
                            ),
                            "content_sha256": str(row["content_sha256"]),
                        }
                        for row in rows
                    ]
                }
            ),
            "source_hashes": hashes,
            "index_aligned": all(
                row["indexed_version_id"] == row["document_version_id"]
                for row in rows
            ),
            "embedding_texts": embedding_texts,
        }

    def _latest_manifest(
        self, profile_revision_id: str
    ) -> RetrievalAuthorizationManifest | None:
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_authorization_manifests "
                "WHERE profile_revision_id=? ORDER BY created_at DESC,"
                "manifest_id DESC LIMIT 1",
                (profile_revision_id,),
            ).fetchone()
        if row is None:
            return None
        return self._manifest_from_row(row)

    @staticmethod
    def _manifest_from_row(
        row: Mapping[str, object],
    ) -> RetrievalAuthorizationManifest:
        """把 SQLite 行恢复为既有活动语料授权清单。"""
        return RetrievalAuthorizationManifest(
            manifest_id=str(row["manifest_id"]),
            project_id=str(row["project_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            profile_revision_id=str(row["profile_revision_id"]),
            source_index_revision_id=str(row["source_index_revision_id"]),
            active_document_digest=str(row["active_document_digest"]),
            active_document_count=int(row["active_document_count"]),
            profile_binding_identity=str(row["profile_binding_identity"]),
            operations=tuple(json.loads(str(row["operations_json"]))),
            authorization_id=str(row["authorization_id"]),
            budget_campaign_id=str(row["budget_campaign_id"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            policy_revision=str(row["policy_revision"]),
            approved_by_session_id=str(row["approved_by_session_id"]),
        )

    def _latest_ingestion_manifest(
        self, job_id: str
    ) -> RetrievalIngestionAuthorizationManifest | None:
        """读取一个 Job 最新的不可变首次入库批准。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_ingestion_authorization_manifests "
                "WHERE job_id=? ORDER BY created_at DESC,manifest_id DESC "
                "LIMIT 1",
                (job_id,),
            ).fetchone()
        return None if row is None else self._ingestion_manifest_from_row(row)

    def _mark_ingestion_authorized(self, job_id: str) -> None:
        """幂等清除已获批准 Job 的重试前置动作。"""
        with self._connections.transaction(write=True) as connection:
            self._clear_ingestion_required_action(connection, job_id)

    @staticmethod
    def _clear_ingestion_required_action(
        connection: sqlite3.Connection, job_id: str
    ) -> bool:
        """在同一控制库事务内允许已批准 Job 进入普通重试。"""
        cursor = connection.execute(
            "UPDATE ingestion_jobs SET stage='authorization_approved',"
            "error_code=NULL,safe_message='真实检索授权已批准，可以继续任务。',"
            "updated_at=? WHERE job_id=? AND state='failed_retryable' "
            "AND (error_code GLOB 'RETRIEVAL_INGESTION_*' "
            "OR error_code IS NULL)",
            (datetime.now(UTC).isoformat(), job_id),
        )
        return bool(cursor.rowcount)

    def _latest_ingestion_manifest_for_profile(
        self, profile_revision_id: str
    ) -> RetrievalIngestionAuthorizationManifest | None:
        """读取 Profile 最新的 Job 授权，供失效原因投影。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_ingestion_authorization_manifests "
                "WHERE profile_revision_id=? ORDER BY created_at DESC,"
                "manifest_id DESC LIMIT 1",
                (profile_revision_id,),
            ).fetchone()
        return None if row is None else self._ingestion_manifest_from_row(row)

    def _matching_authorization_manifest(
        self,
        profile_revision_id: str,
        snapshot: Mapping[str, object],
        requirements: Mapping[str, object],
    ) -> (
        RetrievalAuthorizationManifest
        | RetrievalIngestionAuthorizationManifest
        | None
    ):
        """从两类清单选出最新且精确覆盖当前活动 Revision 的批准。"""
        revision_id = snapshot["active_index_revision_id"]
        if revision_id is None:
            return None
        with self._connections.transaction() as connection:
            regular_rows = connection.execute(
                "SELECT * FROM retrieval_authorization_manifests "
                "WHERE profile_revision_id=? AND source_index_revision_id=?",
                (profile_revision_id, revision_id),
            ).fetchall()
            ingestion_rows = connection.execute(
                "SELECT * FROM retrieval_ingestion_authorization_manifests "
                "WHERE profile_revision_id=? AND target_index_revision_id=? "
                "ORDER BY created_at DESC,manifest_id DESC",
                (profile_revision_id, revision_id),
            ).fetchall()
        candidates: list[
            RetrievalAuthorizationManifest
            | RetrievalIngestionAuthorizationManifest
        ] = [self._manifest_from_row(row) for row in regular_rows]
        candidates.extend(
            self._ingestion_manifest_from_row(row) for row in ingestion_rows
        )
        matching = [
            manifest
            for manifest in candidates
            if manifest.active_document_digest
            == snapshot["active_document_digest"]
            and manifest.active_document_count
            == snapshot["active_document_count"]
            and manifest.profile_binding_identity
            == requirements["profile_binding_identity"]
            and set(manifest.operations) == set(requirements["operations"])
            and (
                not isinstance(
                    manifest, RetrievalIngestionAuthorizationManifest
                )
                or manifest.source_binding_digest
                == snapshot["source_binding_digest"]
            )
        ]
        return self._newest_manifest(matching)

    def _latest_authorization_manifest(
        self, profile_revision_id: str
    ) -> (
        RetrievalAuthorizationManifest
        | RetrievalIngestionAuthorizationManifest
        | None
    ):
        """按统一时间顺序选择 Profile 两类清单中的最新记录。"""
        candidates = [
            manifest
            for manifest in (
                self._latest_manifest(profile_revision_id),
                self._latest_ingestion_manifest_for_profile(
                    profile_revision_id
                ),
            )
            if manifest is not None
        ]
        return self._newest_manifest(candidates)

    @staticmethod
    def _newest_manifest(
        manifests: list[
            RetrievalAuthorizationManifest
            | RetrievalIngestionAuthorizationManifest
        ],
    ) -> (
        RetrievalAuthorizationManifest
        | RetrievalIngestionAuthorizationManifest
        | None
    ):
        """以服务端生成时间和不可变 ID 给两类批准建立稳定全序。"""
        return max(
            manifests,
            key=lambda item: (item.created_at, item.manifest_id),
            default=None,
        )

    @staticmethod
    def _ingestion_manifest_from_row(
        row: Mapping[str, object],
    ) -> RetrievalIngestionAuthorizationManifest:
        """把 SQLite 行恢复为公开且无正文的 Job 授权清单。"""
        return RetrievalIngestionAuthorizationManifest(
            manifest_id=str(row["manifest_id"]),
            job_id=str(row["job_id"]),
            project_id=str(row["project_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            profile_revision_id=str(row["profile_revision_id"]),
            predecessor_index_revision_id=(
                None
                if row["predecessor_index_revision_id"] is None
                else str(row["predecessor_index_revision_id"])
            ),
            target_index_revision_id=str(row["target_index_revision_id"]),
            source_binding_digest=str(row["source_binding_digest"]),
            active_document_digest=str(row["active_document_digest"]),
            source_document_count=int(row["source_document_count"]),
            source_size_bytes=int(row["source_size_bytes"]),
            profile_binding_identity=str(row["profile_binding_identity"]),
            operations=tuple(json.loads(str(row["operations_json"]))),
            authorization_id=str(row["authorization_id"]),
            budget_campaign_id=str(row["budget_campaign_id"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            policy_revision=str(row["policy_revision"]),
            approved_by_session_id=str(row["approved_by_session_id"]),
        )

    def _campaign_binding_reason(
        self,
        manifest: (
            RetrievalAuthorizationManifest
            | RetrievalIngestionAuthorizationManifest
        ),
        snapshot: Mapping[str, object],
        requirements: Mapping[str, object],
    ) -> str | None:
        """核对控制库清单与预算库不可变 Campaign 的完整绑定。"""
        try:
            campaign = ProviderBudgetLedger(
                self._ledger_path, read_only=True
            ).campaign(manifest.budget_campaign_id)
        except (BudgetBlockedError, OSError, ValueError, sqlite3.Error):
            return "RETRIEVAL_BUDGET_UNAVAILABLE"
        expected_scope = f"{manifest.project_id}:{manifest.knowledge_base_id}"
        if isinstance(manifest, RetrievalIngestionAuthorizationManifest):
            expected_scope = f"{expected_scope}:{manifest.job_id}"
        bindings = cast(
            tuple[tuple[RetrievalOperation, str, str, str], ...],
            requirements["bindings"],
        )
        expected_sources = {
            value.removeprefix("sha256:")
            for value in cast(tuple[str, ...], snapshot["source_hashes"])
        }
        approved_sources = {
            value.removeprefix("sha256:")
            for value in campaign.approved_source_hashes
        }
        expected_operations = set(
            cast(tuple[RetrievalOperation, ...], requirements["operations"])
        )
        if (
            campaign.authorization_id != manifest.authorization_id
            or campaign.scope != expected_scope
            or campaign.scope_mode != "knowledge_base"
            or campaign.project_id != manifest.project_id
            or campaign.knowledge_base_id != manifest.knowledge_base_id
            or approved_sources != expected_sources
            or set(campaign.allowed_operations) != set(manifest.operations)
            or set(manifest.operations) != expected_operations
            or set(campaign.approved_request_identities)
            != {item[3] for item in bindings}
            or set(campaign.allowed_models) != {item[2] for item in bindings}
            or campaign.expires_at != manifest.expires_at
        ):
            return "RETRIEVAL_AUTHORIZATION_CAMPAIGN_MISMATCH"
        return None

    def _approval_matches_campaign(
        self,
        manifest: RetrievalIngestionAuthorizationManifest,
        approval: RetrievalAuthorizationApproval,
    ) -> bool:
        """判断重复批准请求是否与现有不可变预算完全相同。"""
        try:
            campaign = ProviderBudgetLedger(
                self._ledger_path, read_only=True
            ).campaign(manifest.budget_campaign_id)
        except (BudgetBlockedError, OSError, ValueError, sqlite3.Error):
            return False
        return (
            campaign.authorization_id == manifest.authorization_id
            and campaign.expires_at == approval.expires_at
            and campaign.request_limit == approval.request_limit
            and campaign.estimated_token_limit == approval.estimated_token_limit
            and campaign.operation_request_limits
            == approval.operation_request_limits
        )

    def _budget_state(
        self,
        manifest: (
            RetrievalAuthorizationManifest
            | RetrievalIngestionAuthorizationManifest
        ),
        *,
        required_operations: tuple[RetrievalOperation, ...] | None = None,
    ) -> tuple[RetrievalBudgetState, str | None]:
        try:
            ledger = ProviderBudgetLedger(self._ledger_path, read_only=True)
            campaign = ledger.campaign(manifest.budget_campaign_id)
            attempts = ledger.attempts(manifest.budget_campaign_id)
        except (BudgetBlockedError, OSError, ValueError, sqlite3.Error):
            return "BLOCKED", "RETRIEVAL_BUDGET_UNAVAILABLE"
        reserved = [item for item in attempts if item["reserved"]]
        if any(item["status"] == "BLOCKED_BUDGET" for item in attempts):
            return "EXHAUSTED", "RETRIEVAL_BUDGET_EXHAUSTED"
        reserved_tokens = sum(
            max(
                int(item["estimated_input_tokens"])
                + int(item.get("estimated_output_tokens", 0)),
                int(item.get("observed_tokens") or 0),
            )
            for item in reserved
        )
        if len(reserved) >= campaign.request_limit or (
            reserved_tokens >= campaign.estimated_token_limit
        ):
            return "EXHAUSTED", "RETRIEVAL_BUDGET_EXHAUSTED"
        for provider, request_limit in campaign.provider_request_limits.items():
            provider_attempts = [
                item for item in reserved if item["provider"] == provider
            ]
            provider_tokens = sum(
                max(
                    int(item["estimated_input_tokens"])
                    + int(item.get("estimated_output_tokens", 0)),
                    int(item.get("observed_tokens") or 0),
                )
                for item in provider_attempts
            )
            if len(provider_attempts) >= request_limit or provider_tokens >= (
                campaign.provider_token_limits.get(
                    provider, campaign.estimated_token_limit
                )
            ):
                return "EXHAUSTED", "RETRIEVAL_BUDGET_EXHAUSTED"
        operation_counts = {
            operation: sum(item["operation"] == operation for item in reserved)
            for operation in campaign.operation_request_limits
        }
        checked_operations = (
            tuple(campaign.operation_request_limits)
            if required_operations is None
            else required_operations
        )
        exhausted_operations = tuple(
            operation
            for operation in checked_operations
            if operation not in campaign.operation_request_limits
            or operation_counts.get(operation, 0)
            >= campaign.operation_request_limits[operation]
        )
        if exhausted_operations:
            return "EXHAUSTED", "RETRIEVAL_BUDGET_EXHAUSTED"
        return "AVAILABLE", None


__all__ = [
    "RetrievalAuthorizationApproval",
    "RetrievalAuthorizationManifest",
    "RetrievalAuthorizationStatus",
    "RetrievalAuthorizationStore",
    "RetrievalIngestionAuthorizationManifest",
    "RetrievalIngestionAuthorizationStatus",
    "RetrievalOperation",
]
