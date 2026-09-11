"""把真实检索方案、活动文档与累计 Provider 预算绑定为显式批准。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
from rag_app.core.errors import RagError
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.common import FrozenModel
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
RetrievalBudgetState = Literal["MISSING", "AVAILABLE", "EXHAUSTED", "BLOCKED"]
ConnectionBudgetState = Literal["READY", "INSUFFICIENT", "BLOCKED"]

_POLICY_REVISION = "retrieval-authorization-v1"
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
        manifest = self._latest_manifest(profile_revision_id)
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
            or not snapshot["index_aligned"]
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
        manifest = status.manifest
        if (
            status.authorization_state != "APPROVED"
            or status.budget_state != "AVAILABLE"
            or status.connection_budget_state != "READY"
            or manifest is None
        ):
            reason = (
                status.reason_codes[0]
                if status.reason_codes
                else "RETRIEVAL_AUTHORIZATION_REQUIRED"
            )
            raise BudgetBlockedError(reason)
        if not set(required_operations) <= set(manifest.operations):
            raise BudgetBlockedError("RETRIEVAL_OPERATION_NOT_APPROVED")
        ledger = ProviderBudgetLedger(self._ledger_path)
        campaign = ledger.campaign(manifest.budget_campaign_id)
        approved_sources = {
            value.removeprefix("sha256:")
            for value in campaign.approved_source_hashes
        }
        if expected_index_revision_id is not None:
            snapshot = self._snapshot(manifest.knowledge_base_id)
            if (
                snapshot["active_index_revision_id"]
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
        manifest: RetrievalAuthorizationManifest | None = None,
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
            manifest=manifest,
            reason_codes=reason_codes,
        )

    def _snapshot(self, knowledge_base_id: str) -> dict[str, object]:
        with self._connections.transaction() as connection:
            knowledge_base = connection.execute(
                "SELECT project_id,active_revision_id FROM knowledge_bases "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
            if (
                knowledge_base is None
                or knowledge_base["active_revision_id"] is None
            ):
                raise ValueError("知识库尚无活动 Index Revision。")
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

    def _budget_state(
        self, manifest: RetrievalAuthorizationManifest
    ) -> tuple[RetrievalBudgetState, str | None]:
        try:
            ledger = ProviderBudgetLedger(self._ledger_path, read_only=True)
            campaign = ledger.campaign(manifest.budget_campaign_id)
            attempts = ledger.attempts(manifest.budget_campaign_id)
        except (BudgetBlockedError, OSError, ValueError):
            return "BLOCKED", "RETRIEVAL_BUDGET_UNAVAILABLE"
        reserved = [item for item in attempts if item["reserved"]]
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
        for operation, limit in campaign.operation_request_limits.items():
            if (
                sum(item["operation"] == operation for item in reserved)
                >= limit
            ):
                return "EXHAUSTED", "RETRIEVAL_BUDGET_EXHAUSTED"
        return "AVAILABLE", None


__all__ = [
    "RetrievalAuthorizationApproval",
    "RetrievalAuthorizationManifest",
    "RetrievalAuthorizationStatus",
    "RetrievalAuthorizationStore",
    "RetrievalOperation",
]
