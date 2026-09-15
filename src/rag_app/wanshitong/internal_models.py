"""用 Universal 控制面幂等配置湾事通内网模型。"""

from __future__ import annotations

from threading import RLock
from typing import cast

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.identifiers import canonical_sha256
from rag_app.product.control_store import validate_connection_metadata
from rag_app.product.model_settings import KnowledgeBaseModelSettings
from rag_app.product.models import (
    ProviderConnection,
    ProviderConnectionDraft,
    ProviderValidationRun,
    RetrievalProfileDraft,
    RetrievalProfileRevision,
)
from rag_app.product.ocr_adapters import LOCAL_OCR_CONNECTION_ID
from rag_app.product.verification import (
    operation_policy_identity,
    validation_is_current,
)
from rag_app.wanshitong.errors import InternalModelConfigurationError
from rag_app.wanshitong.internal_model_settings import InternalModelSettings
from rag_app.wanshitong.model_store import (
    InternalModelBindingStore,
    InternalModelRole,
)
from rag_app.wanshitong.models import InternalModelConfigurationReport
from rag_app.wanshitong.scope_service import FixedScopeService
from rag_app.wanshitong.scope_store import ScopeBindingStore

_DISPLAY_NAMES = {
    "embedding": "湾事通内网 Embedding",
    "reranker": "湾事通内网 Reranker",
    "llm": "湾事通内网 LLM",
}
_ROLES: tuple[InternalModelRole, ...] = ("embedding", "reranker", "llm")
_REQUEST_BUDGET = 500
_TOKEN_BUDGET = 1_000_000


class InternalModelConfigurator:
    """只编排既有 Credential、Connection、Profile 与 Model Settings。"""

    def __init__(
        self,
        runtime: ProductRuntime,
        store: InternalModelBindingStore | None = None,
    ) -> None:
        """保存唯一 Product Runtime 与同库绑定 Store。

        Args:
            runtime: 当前唯一 Universal Product Runtime。
            store: 可选测试或组合根注入的模型绑定 Store。

        """
        self._runtime = runtime
        self._store = store or InternalModelBindingStore(runtime.connections)
        self._lock = RLock()

    def configure(
        self, settings: InternalModelSettings
    ) -> InternalModelConfigurationReport:
        """幂等配置并核对完整内网问答数据面。

        Args:
            settings: 已规范化的三个单 Primary 模型端点。

        Returns:
            不含端点与 Secret 的固定配置摘要。

        Raises:
            InternalModelConfigurationError: 已有绑定或运行结果不一致。

        """
        with self._lock:
            return self._configure_locked(settings)

    def _configure_locked(
        self, settings: InternalModelSettings
    ) -> InternalModelConfigurationReport:
        scope = FixedScopeService(
            self._runtime.sdk,
            ScopeBindingStore(self._runtime.connections),
        ).ensure()
        connections: dict[InternalModelRole, ProviderConnection] = {
            role: self._ensure_connection(role, settings) for role in _ROLES
        }
        profile = self._ensure_profile(
            scope.knowledge_base_id,
            settings,
            embedding=connections["embedding"],
            reranker=connections["reranker"],
        )
        ready_fingerprint = _ready_fingerprint(
            scope.knowledge_base_id,
            settings,
            connections,
            profile,
        )
        ready = self._store.read_ready()
        if (
            ready is not None
            and ready.configuration_fingerprint != ready_fingerprint
        ):
            raise InternalModelConfigurationError(
                "湾事通模型锁与当前引导配置不一致。",
                stage="wanshitong.models.verify",
            )
        self._ensure_profile_validations(profile)
        profile = self._ensure_profile_active(profile)
        self._ensure_generation_validation(connections["llm"], settings)

        desired_models = self._desired_model_settings(
            scope.knowledge_base_id,
            connections["llm"],
            settings,
        )
        current_models = self._runtime.models.get(scope.knowledge_base_id)
        if ready is not None and current_models != desired_models:
            raise InternalModelConfigurationError(
                "湾事通已锁定的 Generation Model Settings 已发生漂移。",
                stage="wanshitong.models.verify",
            )
        if current_models != desired_models:
            self._runtime.models.save(scope.knowledge_base_id, desired_models)
            self._runtime.profiles.invalidate(scope.knowledge_base_id)
        self._store.mark_ready(ready_fingerprint)
        return _report(
            settings,
            connections,
            profile,
            desired_models,
            local_ocr_available=self._runtime.providers.local_ocr_available,
        )

    def _ensure_connection(
        self,
        role: InternalModelRole,
        settings: InternalModelSettings,
    ) -> ProviderConnection:
        draft = _connection_draft(role, settings, credential_id="pending")
        fingerprint = _connection_fingerprint(draft)
        binding = self._store.read_connection(role)
        if binding is not None:
            if binding.configuration_fingerprint != fingerprint:
                raise InternalModelConfigurationError(
                    "湾事通内网模型 Connection 配置已变化。",
                    stage="wanshitong.models.connection.verify",
                    details={"role": role},
                )
            connection = self._runtime.control.get_connection(
                binding.connection_id
            )
            self._require_connection(connection, draft, role)
            return connection

        named = tuple(
            item
            for item in self._runtime.control.list_connections()
            if item.display_name == _DISPLAY_NAMES[role]
        )
        if len(named) > 1:
            raise InternalModelConfigurationError(
                "湾事通模型角色存在多个同名 Connection。",
                stage="wanshitong.models.connection.discover",
                details={"role": role},
            )
        if named:
            connection = named[0]
            self._require_connection(connection, draft, role)
        else:
            credential = self._runtime.credentials.create_encrypted(
                "openai-compatible", ""
            )
            try:
                connection = self._runtime.control.create_connection(
                    draft.model_copy(
                        update={"credential_id": credential.credential_id}
                    )
                )
            except Exception:
                self._runtime.credentials.remove_new_orphan(
                    credential.credential_id
                )
                raise
        self._store.bind_connection(role, connection.connection_id, fingerprint)
        return connection

    def _require_connection(
        self,
        connection: ProviderConnection,
        expected: ProviderConnectionDraft,
        role: InternalModelRole,
    ) -> None:
        actual = validate_connection_metadata(
            ProviderConnectionDraft.model_validate(
                connection.model_dump(
                    include=set(ProviderConnectionDraft.model_fields)
                )
            )
        )
        credential = self._runtime.credentials.get(connection.credential_id)
        if (
            not connection.enabled
            or credential.provider_type != "openai-compatible"
            or not credential.configured
            or _connection_contract(actual) != _connection_contract(expected)
        ):
            raise InternalModelConfigurationError(
                "湾事通模型角色引用的 Connection 与冻结配置不一致。",
                stage="wanshitong.models.connection.verify",
                details={"role": role},
            )

    def _ensure_profile(
        self,
        knowledge_base_id: str,
        settings: InternalModelSettings,
        *,
        embedding: ProviderConnection,
        reranker: ProviderConnection,
    ) -> RetrievalProfileRevision:
        expected = RetrievalProfileDraft(
            knowledge_base_id=knowledge_base_id,
            primary_connection_id=embedding.connection_id,
            primary_embedding_model=settings.embedding_model,
            primary_dimension=settings.embedding_dimension,
            primary_document_policy={
                "encoding_format": "float",
                "normalized": True,
                "role": "document",
            },
            primary_query_policy={
                "encoding_format": "float",
                "normalized": True,
                "role": "query",
            },
            reranker_connection_id=reranker.connection_id,
            reranker_model=settings.reranker_model,
        )
        fingerprint = canonical_sha256(_profile_draft_contract(expected))
        binding = self._store.read_profile()
        if binding is not None:
            if binding.configuration_fingerprint != fingerprint:
                raise InternalModelConfigurationError(
                    "湾事通 Retrieval Profile 绑定与当前配置不一致。",
                    stage="wanshitong.models.profile.verify",
                )
            profile = self._runtime.control.get_profile(
                binding.profile_revision_id
            )
            self._require_profile(profile, expected)
            return profile

        matching = tuple(
            profile
            for profile in self._runtime.control.list_profiles(
                knowledge_base_id
            )
            if profile.status in {"active", "draft"}
            and _profile_contract(profile) == _profile_draft_contract(expected)
        )
        if len(matching) > 1:
            raise InternalModelConfigurationError(
                "固定知识库存在多个相同的 Retrieval Profile。",
                stage="wanshitong.models.profile.discover",
            )
        profile = (
            matching[0]
            if matching
            else self._runtime.control.create_profile(expected)
        )
        self._store.bind_profile(
            profile.profile_revision_id,
            fingerprint,
        )
        return profile

    def _require_profile(
        self,
        profile: RetrievalProfileRevision,
        expected: RetrievalProfileDraft,
    ) -> None:
        if profile.status not in {"active", "draft"} or _profile_contract(
            profile
        ) != _profile_draft_contract(expected):
            raise InternalModelConfigurationError(
                "湾事通 Retrieval Profile 已失效或发生漂移。",
                stage="wanshitong.models.profile.verify",
            )

    def _ensure_profile_validations(
        self, profile: RetrievalProfileRevision
    ) -> None:
        validations = self._runtime.control.profile_validations(
            profile.profile_revision_id
        )
        requirements = (
            (
                profile.primary_connection_id,
                "embedding.document",
                profile.primary_embedding_model,
                profile.primary_dimension,
                cast(
                    dict[str, object],
                    dict(profile.primary_document_policy),
                ),
            ),
            (
                profile.primary_connection_id,
                "embedding.query",
                profile.primary_embedding_model,
                profile.primary_dimension,
                cast(dict[str, object], dict(profile.primary_query_policy)),
            ),
            (
                profile.reranker_connection_id or "",
                "reranking",
                profile.reranker_model or "",
                None,
                None,
            ),
        )
        for connection_id, operation, model, dimension, policy in requirements:
            current = validations.get(f"{connection_id}:{operation}")
            if current is not None and current.status == "succeeded":
                continue
            result = self._runtime.providers.validate(
                connection_id,
                operation=operation,
                model=model,
                expected_dimension=dimension,
                request_policy=policy,
            )
            _require_validation_success(result, operation)
        missing = self._runtime.control.profile_validation_issues(
            profile.profile_revision_id
        )
        if missing:
            raise InternalModelConfigurationError(
                "湾事通 Retrieval Profile 的 Provider 验证不完整。",
                stage="wanshitong.models.profile.validate",
                details={"missing_operations": list(missing)},
            )

    def _ensure_profile_active(
        self, profile: RetrievalProfileRevision
    ) -> RetrievalProfileRevision:
        if profile.status == "active":
            return profile
        preview = self._runtime.control.preview_impact(
            profile.profile_revision_id
        )
        activated = self._runtime.control.activate_profile(
            profile.profile_revision_id,
            confirmed_impact=preview.impact,
        )
        if activated.status != "active":
            raise InternalModelConfigurationError(
                "固定知识库已有文档，需要先完成既有索引切换任务。",
                stage="wanshitong.models.profile.activate",
                details={"activation_job_id": activated.activation_job_id},
            )
        return activated

    def _ensure_generation_validation(
        self,
        connection: ProviderConnection,
        settings: InternalModelSettings,
    ) -> None:
        credential = self._runtime.credentials.get(connection.credential_id)
        policy_identity = operation_policy_identity(
            connection, settings.llm_model, "generation"
        )
        current = next(
            (
                item
                for item in self._runtime.control.list_validations(
                    connection.connection_id
                )
                if item.operation == "generation"
                and item.provider_model == settings.llm_model
                and item.request_policy_identity == policy_identity
                and validation_is_current(
                    item, connection, credential.key_version
                )
                and item.status == "succeeded"
            ),
            None,
        )
        if current is not None:
            return
        result = self._runtime.providers.validate(
            connection.connection_id,
            operation="generation",
            model=settings.llm_model,
        )
        _require_validation_success(result, "generation")

    def _desired_model_settings(
        self,
        knowledge_base_id: str,
        connection: ProviderConnection,
        settings: InternalModelSettings,
    ) -> KnowledgeBaseModelSettings:
        current = self._runtime.models.get(knowledge_base_id)
        return current.model_copy(
            update={
                "generation_connection_id": connection.connection_id,
                "generation_model": settings.llm_model,
                "generation_fallback_models": (),
                "rewrite_enabled": False,
            }
        )


def _connection_draft(
    role: InternalModelRole,
    settings: InternalModelSettings,
    *,
    credential_id: str,
) -> ProviderConnectionDraft:
    base_urls = {
        "embedding": settings.embedding_base_url,
        "reranker": settings.reranker_base_url,
        "llm": settings.llm_base_url,
    }
    return validate_connection_metadata(
        ProviderConnectionDraft(
            display_name=_DISPLAY_NAMES[role],
            provider_type="openai-compatible",
            credential_id=credential_id,
            endpoint_mode="custom",
            api_base_url=base_urls[role],
            rerank_protocol=(
                settings.reranker_protocol if role == "reranker" else None
            ),
            rerank_path=settings.reranker_path if role == "reranker" else None,
            request_budget=_REQUEST_BUDGET,
            token_budget=_TOKEN_BUDGET,
        )
    )


def _connection_contract(draft: ProviderConnectionDraft) -> dict[str, object]:
    return draft.model_dump(exclude={"credential_id"})


def _connection_fingerprint(draft: ProviderConnectionDraft) -> str:
    return canonical_sha256(_connection_contract(draft))


def _profile_draft_contract(
    profile: RetrievalProfileDraft,
) -> dict[str, object]:
    return {
        "knowledge_base_id": profile.knowledge_base_id,
        "primary_connection_id": profile.primary_connection_id,
        "primary_embedding_model": profile.primary_embedding_model,
        "primary_dimension": profile.primary_dimension,
        "primary_document_policy": profile.primary_document_policy,
        "primary_query_policy": profile.primary_query_policy,
        "standby_connection_id": profile.standby_connection_id,
        "standby_embedding_model": profile.standby_embedding_model,
        "standby_dimension": profile.standby_dimension,
        "reranker_connection_id": profile.reranker_connection_id,
        "reranker_model": profile.reranker_model,
        "failover_enabled": profile.failover_enabled,
    }


def _profile_contract(
    profile: RetrievalProfileRevision,
) -> dict[str, object]:
    return {
        "knowledge_base_id": profile.knowledge_base_id,
        "primary_connection_id": profile.primary_connection_id,
        "primary_embedding_model": profile.primary_embedding_model,
        "primary_dimension": profile.primary_dimension,
        "primary_document_policy": dict(profile.primary_document_policy),
        "primary_query_policy": dict(profile.primary_query_policy),
        "standby_connection_id": profile.standby_connection_id,
        "standby_embedding_model": profile.standby_embedding_model,
        "standby_dimension": profile.standby_dimension,
        "reranker_connection_id": profile.reranker_connection_id,
        "reranker_model": profile.reranker_model,
        "failover_enabled": profile.failover_enabled,
    }


def _ready_fingerprint(
    knowledge_base_id: str,
    settings: InternalModelSettings,
    connections: dict[InternalModelRole, ProviderConnection],
    profile: RetrievalProfileRevision,
) -> str:
    return canonical_sha256(
        {
            "knowledge_base_id": knowledge_base_id,
            "connections": {
                role: _connection_fingerprint(
                    _connection_draft(
                        role,
                        settings,
                        credential_id=connection.credential_id,
                    )
                )
                for role, connection in connections.items()
            },
            "profile": _profile_contract(profile),
            "generation": {
                "connection_id": connections["llm"].connection_id,
                "model": settings.llm_model,
                "rewrite_enabled": False,
            },
        }
    )


def _require_validation_success(
    result: ProviderValidationRun, operation: str
) -> None:
    if result.status == "succeeded":
        return
    raise InternalModelConfigurationError(
        "湾事通内网模型连接验证失败。",
        stage="wanshitong.models.validate",
        details={
            "operation": operation,
            "safe_error_code": result.safe_error_code,
        },
    )


def _report(
    settings: InternalModelSettings,
    connections: dict[InternalModelRole, ProviderConnection],
    profile: RetrievalProfileRevision,
    model_settings: KnowledgeBaseModelSettings,
    *,
    local_ocr_available: bool,
) -> InternalModelConfigurationReport:
    local_ocr_selected = (
        model_settings.ocr_connection_id == LOCAL_OCR_CONNECTION_ID
    )
    return InternalModelConfigurationReport(
        knowledge_base_id=profile.knowledge_base_id,
        embedding_connection_id=connections["embedding"].connection_id,
        reranker_connection_id=connections["reranker"].connection_id,
        llm_connection_id=connections["llm"].connection_id,
        retrieval_profile_revision_id=profile.profile_revision_id,
        embedding_model=settings.embedding_model,
        embedding_dimension=settings.embedding_dimension,
        reranker_model=settings.reranker_model,
        llm_model=settings.llm_model,
        ocr_configured=bool(
            model_settings.ocr_connection_id
            and model_settings.ocr_enabled
            and (not local_ocr_selected or local_ocr_available)
        ),
        pdf_layout_parsing_configured=bool(
            model_settings.pdf_parser_connection_id
            and model_settings.pdf_parser_model
            and model_settings.pdf_parser_enabled
        ),
    )


__all__ = ["InternalModelConfigurator"]
