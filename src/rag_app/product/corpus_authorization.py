"""把活动知识库语料、模型用途与累计预算绑定为一次管理员批准。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, StrictInt, field_validator, model_validator

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_models import BudgetCampaign
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import RagError
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models.common import FrozenModel
from rag_app.product.control_store import ProductControlStore
from rag_app.product.model_settings import (
    KnowledgeBaseModelSettings,
    ProductModelSettings,
)
from rag_app.product.ocr_adapters import LOCAL_OCR_CONNECTION_ID
from rag_app.product.provider_runtime import ProviderRuntimeRegistry

CorpusOperation = Literal["generation", "query.rewrite", "image.ocr"]
CorpusAuthorizationState = Literal[
    "NOT_REQUIRED",
    "MISSING",
    "APPROVED",
    "STALE_REVISION",
    "STALE_CORPUS",
    "EXPIRED",
]
ModelConfigurationState = Literal["NOT_CONFIGURED", "CONFIGURED", "INVALID"]
ModelAuthorizationState = Literal[
    "NOT_REQUIRED",
    "MISSING",
    "APPROVED",
    "PARTIAL",
    "STALE_MODEL",
    "EXPIRED",
    "BLOCKED",
]
BudgetState = Literal[
    "NOT_REQUIRED", "MISSING", "AVAILABLE", "EXHAUSTED", "BLOCKED"
]

_POLICY_REVISION = "corpus-authorization-v1"
_MAX_AUTHORIZATION_DAYS = 365


class CorpusAuthorizationApproval(FrozenModel):
    """管理员明确批准当前活动语料所需的有界输入。"""

    operations: tuple[CorpusOperation, ...] = Field(min_length=1, max_length=3)
    expires_at: str
    request_limit: StrictInt = Field(ge=1, le=10_000)
    estimated_token_limit: StrictInt = Field(ge=1, le=100_000_000)
    operation_request_limits: dict[CorpusOperation, StrictInt]

    @field_validator("operations")
    @classmethod
    def _unique_operations(
        cls, value: tuple[CorpusOperation, ...]
    ) -> tuple[CorpusOperation, ...]:
        if len(value) != len(set(value)):
            raise ValueError("资料授权 operation 不允许重复。")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _validate_limits_and_expiry(self) -> CorpusAuthorizationApproval:
        if set(self.operation_request_limits) != set(self.operations):
            raise ValueError("每个资料授权 operation 必须具有独立请求上限。")
        if any(value < 1 for value in self.operation_request_limits.values()):
            raise ValueError("资料授权 operation 请求上限必须为正数。")
        if self.request_limit < sum(self.operation_request_limits.values()):
            raise ValueError("累计请求上限不能小于各 operation 上限之和。")
        try:
            expires_at = datetime.fromisoformat(self.expires_at)
        except ValueError:
            raise ValueError("资料授权到期时间格式无效。") from None
        now = datetime.now(UTC)
        if expires_at.tzinfo is None or not (
            now < expires_at <= now + timedelta(days=_MAX_AUTHORIZATION_DAYS)
        ):
            raise ValueError("资料授权必须在未来 365 天内明确到期。")
        return self


class CorpusAuthorizationManifest(FrozenModel):
    """不暴露正文与逐文档哈希的不可变活动语料批准记录。"""

    manifest_id: str = Field(pattern=r"^cauth_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    active_index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    active_document_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    active_document_count: StrictInt = Field(gt=0)
    provider_connection_id: str
    provider_model: str
    operation_binding_identity: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    operations: tuple[CorpusOperation, ...] = Field(min_length=1)
    authorization_id: str
    budget_campaign_id: str
    created_at: str
    expires_at: str
    policy_revision: str = _POLICY_REVISION
    approved_by_session_id: str


class CorpusAuthorizationStatus(FrozenModel):
    """查询与管理员页面共享的动态资料、模型和预算状态。"""

    corpus_authorization_state: CorpusAuthorizationState
    model_configuration_state: ModelConfigurationState
    model_authorization_state: ModelAuthorizationState
    budget_state: BudgetState
    required_operations: tuple[CorpusOperation, ...] = ()
    manifest: CorpusAuthorizationManifest | None = None
    fallback_reason_codes: tuple[str, ...] = ()


class CorpusAuthorizationStore:
    """从当前活动 Revision 计算清单，并追加不可变管理员批准。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        control: ProductControlStore,
        models: ProductModelSettings,
        providers: ProviderRuntimeRegistry,
        ledger_path: Path,
    ) -> None:
        self._connections = connections
        self._control = control
        self._models = models
        self._providers = providers
        self._ledger_path = ledger_path

    def approve(
        self,
        knowledge_base_id: str,
        approval: CorpusAuthorizationApproval,
        *,
        approved_by_session_id: str,
    ) -> CorpusAuthorizationStatus:
        """批准调用瞬间的活动 Revision、文档版本、模型用途和预算。

        Args:
            knowledge_base_id: 由路由与管理员会话确定的知识库。
            approval: 管理员明确选择的用途、有效期与累计上限。
            approved_by_session_id: 认证中间件复核得到的 Session ID。

        Returns:
            新清单保存后的动态状态；不会发送 Provider 请求。

        Raises:
            ValueError: 没有活动语料、模型配置不可用或用途未启用。

        """
        settings = self._models.get(knowledge_base_id)
        snapshot = self._corpus_snapshot(knowledge_base_id)
        if not cast(bool, snapshot["index_aligned"]):
            raise ValueError("当前活动文档与活动 Index Revision 尚未对齐。")
        if cast(int, snapshot["active_document_count"]) == 0:
            raise ValueError("活动知识库没有可批准的文档版本。")
        bindings = self._operation_bindings(settings, approval.operations)
        resolved_bindings = tuple(
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
            for operation, connection_id, model in bindings
        )
        request_identities = tuple(
            sorted({item[3] for item in resolved_bindings})
        )
        operation_binding_identity = canonical_sha256(
            {
                "bindings": [
                    {
                        "operation": operation,
                        "connection_id": connection_id,
                        "model": model,
                        "request_identity": request_identity,
                    }
                    for operation, connection_id, model, request_identity in (
                        resolved_bindings
                    )
                ]
            }
        )
        allowed_models = tuple(sorted({model for _, _, model in bindings}))
        source_hashes = cast(tuple[str, ...], snapshot["source_hashes"])
        manifest_id = f"cauth_{uuid.uuid4().hex}"
        authorization_id = f"corpus-auth-{uuid.uuid4().hex}"
        campaign_id = f"corpus-budget-{uuid.uuid4().hex}"
        project_id = cast(str, snapshot["project_id"])
        campaign = BudgetCampaign(
            campaign_id=campaign_id,
            authorization_id=authorization_id,
            scope=f"{project_id}:{knowledge_base_id}",
            request_limit=approval.request_limit,
            estimated_token_limit=approval.estimated_token_limit,
            approved_request_identities=request_identities,
            provider_request_limits={"aliyun": approval.request_limit},
            provider_token_limits={
                "aliyun": approval.estimated_token_limit
            },
            step_request_limits=dict(approval.operation_request_limits),
            scope_mode="knowledge_base",
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            approved_source_hashes=source_hashes,
            approved_media_hashes=(
                settings.ocr_media_hashes
                if "image.ocr" in approval.operations
                else ()
            ),
            allowed_models=allowed_models,
            allowed_operations=approval.operations,
            expires_at=approval.expires_at,
            operation_request_limits=dict(approval.operation_request_limits),
        )
        ledger = ProviderBudgetLedger(self._ledger_path)
        ledger.create_campaign(campaign)
        first_binding = bindings[0]
        manifest = CorpusAuthorizationManifest(
            manifest_id=manifest_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            active_index_revision_id=cast(
                str, snapshot["active_index_revision_id"]
            ),
            active_document_digest=cast(
                str, snapshot["active_document_digest"]
            ),
            active_document_count=cast(int, snapshot["active_document_count"]),
            provider_connection_id=first_binding[1],
            provider_model=first_binding[2],
            operation_binding_identity=operation_binding_identity,
            operations=approval.operations,
            authorization_id=authorization_id,
            budget_campaign_id=campaign_id,
            created_at=datetime.now(UTC).isoformat(),
            expires_at=approval.expires_at,
            approved_by_session_id=approved_by_session_id,
        )
        bound_settings = settings.model_copy(
            update={"budget_campaign_id": campaign_id}
        )
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO corpus_authorization_manifests("
                "manifest_id,project_id,knowledge_base_id,"
                "active_index_revision_id,active_document_digest,"
                "active_document_count,provider_connection_id,provider_model,"
                "operation_binding_identity,"
                "operations_json,authorization_id,budget_campaign_id,"
                "created_at,expires_at,policy_revision,approved_by_session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    manifest.manifest_id,
                    manifest.project_id,
                    manifest.knowledge_base_id,
                    manifest.active_index_revision_id,
                    manifest.active_document_digest,
                    manifest.active_document_count,
                    manifest.provider_connection_id,
                    manifest.provider_model,
                    manifest.operation_binding_identity,
                    json.dumps(manifest.operations, separators=(",", ":")),
                    manifest.authorization_id,
                    manifest.budget_campaign_id,
                    manifest.created_at,
                    manifest.expires_at,
                    manifest.policy_revision,
                    manifest.approved_by_session_id,
                ),
            )
            connection.execute(
                "INSERT INTO knowledge_base_model_settings VALUES (?, ?, ?) "
                "ON CONFLICT(knowledge_base_id) DO UPDATE SET "
                "configuration=excluded.configuration,updated_at=excluded.updated_at",
                (
                    knowledge_base_id,
                    bound_settings.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return self.status(knowledge_base_id)

    def status(self, knowledge_base_id: str) -> CorpusAuthorizationStatus:
        """把最新清单与当前活动语料、模型和账本重新对账。

        Args:
            knowledge_base_id: 当前已鉴权知识库。

        Returns:
            不会触发网络或自动批准的动态状态。

        """
        settings = self._models.get(knowledge_base_id)
        required = self._required_operations(settings)
        if not required:
            return CorpusAuthorizationStatus(
                corpus_authorization_state="NOT_REQUIRED",
                model_configuration_state="NOT_CONFIGURED",
                model_authorization_state="NOT_REQUIRED",
                budget_state="NOT_REQUIRED",
            )
        try:
            self._operation_bindings(settings, required)
        except (RagError, ValueError, KeyError):
            return CorpusAuthorizationStatus(
                corpus_authorization_state="MISSING",
                model_configuration_state="INVALID",
                model_authorization_state="BLOCKED",
                budget_state="BLOCKED",
                required_operations=required,
                fallback_reason_codes=("MODEL_CONFIGURATION_INVALID",),
            )
        manifest = self._latest_manifest(knowledge_base_id)
        if manifest is None:
            return CorpusAuthorizationStatus(
                corpus_authorization_state="MISSING",
                model_configuration_state="CONFIGURED",
                model_authorization_state="MISSING",
                budget_state="MISSING",
                required_operations=required,
                fallback_reason_codes=("CORPUS_AUTHORIZATION_MISSING",),
            )
        try:
            snapshot = self._corpus_snapshot(knowledge_base_id)
        except ValueError:
            return CorpusAuthorizationStatus(
                corpus_authorization_state="STALE_REVISION",
                model_configuration_state="CONFIGURED",
                model_authorization_state="BLOCKED",
                budget_state="BLOCKED",
                required_operations=required,
                manifest=manifest,
                fallback_reason_codes=("ACTIVE_REVISION_UNAVAILABLE",),
            )
        corpus_state, corpus_reason = self._corpus_state(manifest, snapshot)
        now = datetime.now(UTC)
        if datetime.fromisoformat(manifest.expires_at) <= now:
            return CorpusAuthorizationStatus(
                corpus_authorization_state="EXPIRED",
                model_configuration_state="CONFIGURED",
                model_authorization_state="EXPIRED",
                budget_state="BLOCKED",
                required_operations=required,
                manifest=manifest,
                fallback_reason_codes=("BUSINESS_AUTHORIZATION_EXPIRED",),
            )
        if corpus_state != "APPROVED":
            return CorpusAuthorizationStatus(
                corpus_authorization_state=corpus_state,
                model_configuration_state="CONFIGURED",
                model_authorization_state="BLOCKED",
                budget_state="BLOCKED",
                required_operations=required,
                manifest=manifest,
                fallback_reason_codes=(corpus_reason,),
            )
        try:
            model_state, model_reason = self._model_state(
                settings, required, manifest
            )
        except (RagError, ValueError, KeyError):
            model_state, model_reason = (
                "STALE_MODEL",
                "CORPUS_MODEL_BINDING_CHANGED",
            )
        if model_state != "APPROVED":
            return CorpusAuthorizationStatus(
                corpus_authorization_state="APPROVED",
                model_configuration_state="CONFIGURED",
                model_authorization_state=model_state,
                budget_state="BLOCKED",
                required_operations=required,
                manifest=manifest,
                fallback_reason_codes=(model_reason,),
            )
        budget_state, budget_reason = self._budget_state(manifest)
        return CorpusAuthorizationStatus(
            corpus_authorization_state="APPROVED",
            model_configuration_state="CONFIGURED",
            model_authorization_state="APPROVED",
            budget_state=budget_state,
            required_operations=required,
            manifest=manifest,
            fallback_reason_codes=(
                () if budget_reason is None else (budget_reason,)
            ),
        )

    def _corpus_snapshot(self, knowledge_base_id: str) -> dict[str, object]:
        """在一个只读事务中冻结活动 Revision 与当前活动文档摘要。"""
        with self._connections.transaction() as connection:
            knowledge_base = connection.execute(
                "SELECT project_id,active_revision_id FROM knowledge_bases "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (knowledge_base_id,),
            ).fetchone()
            if knowledge_base is None:
                raise ValueError("知识库不存在。")
            revision_id = knowledge_base["active_revision_id"]
            if revision_id is None:
                raise ValueError("知识库尚无活动 Index Revision。")
            revision = connection.execute(
                "SELECT state FROM index_revisions WHERE index_revision_id=? "
                "AND knowledge_base_id=?",
                (revision_id, knowledge_base_id),
            ).fetchone()
            if revision is None or revision["state"] != "active":
                raise ValueError("知识库活动 Index Revision 不可用。")
            rows = connection.execute(
                "SELECT d.document_id,v.document_version_id,"
                "v.content_sha256,rd.document_version_id AS indexed_version_id "
                "FROM documents d JOIN document_versions v "
                "ON v.document_version_id=d.current_version_id "
                "LEFT JOIN revision_documents rd ON rd.revision_id=? "
                "AND rd.document_id=d.document_id "
                "WHERE d.knowledge_base_id=? AND d.status='active' "
                "AND d.deleted_at IS NULL ORDER BY d.document_id",
                (revision_id, knowledge_base_id),
            ).fetchall()
        hashes = tuple(sorted({str(row["content_sha256"]) for row in rows}))
        return {
            "project_id": str(knowledge_base["project_id"]),
            "active_index_revision_id": str(revision_id),
            "active_document_count": len(rows),
            "active_document_digest": canonical_sha256(
                {"active_document_content_hashes": hashes}
            ),
            "source_hashes": hashes,
            "index_aligned": all(
                row["indexed_version_id"] == row["document_version_id"]
                for row in rows
            ),
        }

    def _latest_manifest(
        self, knowledge_base_id: str
    ) -> CorpusAuthorizationManifest | None:
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM corpus_authorization_manifests "
                "WHERE knowledge_base_id=? ORDER BY created_at DESC,"
                "manifest_id DESC LIMIT 1",
                (knowledge_base_id,),
            ).fetchone()
        if row is None:
            return None
        return CorpusAuthorizationManifest(
            manifest_id=str(row["manifest_id"]),
            project_id=str(row["project_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            active_index_revision_id=str(row["active_index_revision_id"]),
            active_document_digest=str(row["active_document_digest"]),
            active_document_count=int(row["active_document_count"]),
            provider_connection_id=str(row["provider_connection_id"]),
            provider_model=str(row["provider_model"]),
            operation_binding_identity=str(row["operation_binding_identity"]),
            operations=tuple(json.loads(str(row["operations_json"]))),
            authorization_id=str(row["authorization_id"]),
            budget_campaign_id=str(row["budget_campaign_id"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            policy_revision=str(row["policy_revision"]),
            approved_by_session_id=str(row["approved_by_session_id"]),
        )

    @staticmethod
    def _required_operations(
        settings: KnowledgeBaseModelSettings,
    ) -> tuple[CorpusOperation, ...]:
        operations: list[CorpusOperation] = []
        if settings.generation_connection_id:
            operations.append("generation")
            if settings.rewrite_enabled:
                operations.append("query.rewrite")
        if settings.ocr_enabled and settings.ocr_connection_id != (
            LOCAL_OCR_CONNECTION_ID
        ):
            operations.append("image.ocr")
        return tuple(operations)

    def _operation_bindings(
        self,
        settings: KnowledgeBaseModelSettings,
        operations: tuple[CorpusOperation, ...],
    ) -> tuple[tuple[CorpusOperation, str, str], ...]:
        bindings: list[tuple[CorpusOperation, str, str]] = []
        for operation in operations:
            if operation in {"generation", "query.rewrite"}:
                connection_id = settings.generation_connection_id
                model = settings.generation_model
                if operation == "query.rewrite" and not settings.rewrite_enabled:
                    raise ValueError("问题改写尚未启用。")
            else:
                connection_id = settings.ocr_connection_id
                model = settings.ocr_model
                if not settings.ocr_enabled:
                    raise ValueError("图片识别尚未启用。")
                if connection_id == LOCAL_OCR_CONNECTION_ID:
                    raise ValueError("本地 OCR 不需要也不能创建远程资料授权。")
                if not settings.ocr_media_hashes:
                    raise ValueError("图片识别尚未选择可批准的媒体。")
            if not connection_id or not model:
                raise ValueError(f"{operation} 尚未配置模型。")
            connection = self._control.get_connection(connection_id)
            if not connection.enabled:
                raise ValueError("资料授权引用的模型连接已停用。")
            bindings.append((operation, connection_id, model))
        if not bindings:
            raise ValueError("当前设置没有需要批准的远程模型用途。")
        return tuple(bindings)

    @staticmethod
    def _corpus_state(
        manifest: CorpusAuthorizationManifest,
        snapshot: Mapping[str, object],
    ) -> tuple[CorpusAuthorizationState, str]:
        if manifest.active_index_revision_id != snapshot[
            "active_index_revision_id"
        ]:
            return "STALE_REVISION", "CORPUS_AUTHORIZATION_STALE_REVISION"
        if (
            manifest.active_document_digest != snapshot["active_document_digest"]
            or manifest.active_document_count != snapshot["active_document_count"]
            or not snapshot["index_aligned"]
        ):
            return "STALE_CORPUS", "CORPUS_AUTHORIZATION_STALE_CORPUS"
        return "APPROVED", "CORPUS_AUTHORIZATION_APPROVED"

    def _model_state(
        self,
        settings: KnowledgeBaseModelSettings,
        required: tuple[CorpusOperation, ...],
        manifest: CorpusAuthorizationManifest,
    ) -> tuple[ModelAuthorizationState, str]:
        if settings.budget_campaign_id != manifest.budget_campaign_id:
            return "STALE_MODEL", "CORPUS_BUDGET_BINDING_CHANGED"
        if not set(required) <= set(manifest.operations):
            return "PARTIAL", "CORPUS_OPERATION_NOT_APPROVED"
        bindings = self._operation_bindings(settings, required)
        current_binding_identity = canonical_sha256(
            {
                "bindings": [
                    {
                        "operation": operation,
                        "connection_id": connection_id,
                        "model": model,
                        "request_identity": (
                            self._providers.request_authorization_identity(
                                connection_id,
                                operation=operation,
                                model=model,
                            )
                        ),
                    }
                    for operation, connection_id, model in bindings
                ]
            }
        )
        if current_binding_identity != manifest.operation_binding_identity:
            return "STALE_MODEL", "CORPUS_MODEL_BINDING_CHANGED"
        return "APPROVED", "CORPUS_MODEL_APPROVED"

    def _budget_state(
        self, manifest: CorpusAuthorizationManifest
    ) -> tuple[BudgetState, str | None]:
        try:
            ledger = ProviderBudgetLedger(self._ledger_path, read_only=True)
            campaign = ledger.campaign(manifest.budget_campaign_id)
            attempts = ledger.attempts(manifest.budget_campaign_id)
        except (BudgetBlockedError, OSError, ValueError):
            return "BLOCKED", "CORPUS_BUDGET_UNAVAILABLE"
        reserved = [item for item in attempts if item["reserved"]]
        reserved_tokens = sum(
            max(
                int(item["estimated_input_tokens"])
                + int(item.get("estimated_output_tokens", 0)),
                int(item.get("observed_tokens") or 0),
            )
            for item in reserved
        )
        exhausted = len(reserved) >= campaign.request_limit or (
            reserved_tokens >= campaign.estimated_token_limit
        )
        if not exhausted:
            for operation, limit in campaign.operation_request_limits.items():
                if sum(item["operation"] == operation for item in reserved) >= limit:
                    exhausted = True
                    break
        return (
            ("EXHAUSTED", "CORPUS_BUDGET_EXHAUSTED")
            if exhausted
            else ("AVAILABLE", None)
        )


__all__ = [
    "CorpusAuthorizationApproval",
    "CorpusAuthorizationManifest",
    "CorpusAuthorizationStatus",
    "CorpusAuthorizationStore",
]
