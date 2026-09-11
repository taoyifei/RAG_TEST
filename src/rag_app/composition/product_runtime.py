"""P10.5 唯一 Product Runtime 组合根。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Generic, TypeVar, cast
from urllib.parse import urlparse

import httpx

from rag_app._build_revision import SOURCE_REVISION
from rag_app.adapters.stores import (
    InMemoryRetrievalCache,
    MigrationRunner,
    SqliteConnectionFactory,
)
from rag_app.application.artifact_lifecycle import (
    ArtifactLifecycleService,
    BlobLocatorPort,
)
from rag_app.application.durable_jobs import DurableJobRunner
from rag_app.application.embedding_indexing import DocumentEmbeddingService
from rag_app.application.embedding_router import QueryEmbeddingRouter
from rag_app.application.lifecycle import LifecycleService
from rag_app.application.provider_health import (
    LocalUsageBudget,
    ProviderCircuitBreaker,
)
from rag_app.application.retrieval import (
    QueryDataPlaneContext,
    RetrievalService,
)
from rag_app.application.revision_builder import RevisionBuilder
from rag_app.application.revision_validator import RevisionValidator
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.composition.p06_runtime import resolved_contracts
from rag_app.composition.p07_runtime import P07Runtime
from rag_app.composition.p09_runtime import (
    P09Runtime,
    P09RuntimeHooks,
    build_p09_runtime,
)
from rag_app.composition.profiles import (
    ComponentsProfile,
    LocalDataProfile,
    RagProfile,
    default_offline_profile,
)
from rag_app.core.errors import RagError
from rag_app.core.events import TraceEvent
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerClaim,
    DocumentEmbeddingBudget,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    EmbeddingTopology,
    Job,
    KnowledgeBaseScope,
    ParseResult,
    RetrievalPolicy,
    SearchAnswerResult,
    SearchRequest,
    SystemStatus,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import (
    CancellationPort,
    ChunkValidationPort,
    ExactStorePort,
)
from rag_app.ocr import OcrClient
from rag_app.product.auth import (
    AuthStore,
    ConsoleSessionService,
    load_bootstrap_token,
)
from rag_app.product.compatibility import CompatibilityManifest, load_manifest
from rag_app.product.control_store import ProductControlStore
from rag_app.product.conversations import ProductConversationStore
from rag_app.product.corpus_authorization import (
    CorpusAuthorizationStatus,
    CorpusAuthorizationStore,
)
from rag_app.product.credential_store import CredentialStore
from rag_app.product.crypto import MasterKey, SecretCipher, load_master_key
from rag_app.product.diagram_relations import ProductDiagramRelations
from rag_app.product.feedback import ProductFeedbackStore
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.product.model_settings import (
    KnowledgeBaseModelSettings,
    ProductModelSettings,
)
from rag_app.product.models import (
    ProviderValidationRun,
    RetrievalProfileRevision,
)
from rag_app.product.ocr_adapters import (
    LocalOcrAdapterConfig,
    LocalProductOcrAdapter,
)
from rag_app.product.ocr_enrichment import ProductOcrEnrichment
from rag_app.product.provider_runtime import (
    ProviderRuntimeRegistry,
    TransportFactory,
    build_offline_mock_transport,
)
from rag_app.product.query_history import ProductQueryHistory
from rag_app.product.resolved_profile import (
    ResolvedEmbeddingSpec,
    resolve_retrieval_policy,
)
from rag_app.product.retrieval_authorization import (
    RetrievalAuthorizationStore,
    RetrievalOperation,
)
from rag_app.product.singleflight import (
    ProductQuerySingleflight,
    SingleflightMetrics,
)
from rag_app.product.trace_coordinator import ProductTraceCoordinator
from rag_app.product.verification import profile_specs
from rag_app.sdk import RagSdk
from rag_app.tracing import TraceRecorder, TraceStore

_MIN_LOCAL_OCR_TOKEN_LENGTH = 32
_MAX_LOCAL_OCR_TOKEN_LENGTH = 4096
_ResourceT = TypeVar("_ResourceT")


def _can_reuse_generation_cache(
    status: CorpusAuthorizationStatus | None,
) -> bool:
    """判断预算耗尽是否只允许复用同授权身份的已验证回答。

    Args:
        status: 当前语料、模型和预算的动态对账结果。

    Returns:
        仅当语料与模型授权仍批准、且唯一阻断是预算耗尽时返回 True。

    """
    return bool(
        status is not None
        and status.corpus_authorization_state == "APPROVED"
        and status.model_configuration_state == "CONFIGURED"
        and status.model_authorization_state == "APPROVED"
        and status.budget_state == "EXHAUSTED"
    )


@dataclass(frozen=True, slots=True)
class ProductRuntimeSettings:
    """普通产品启动所需的最小配置。"""

    data_dir: Path
    frontend_dir: Path
    bootstrap_token_file: Path
    host: str = "127.0.0.1"
    port: int = 8088
    master_key_file: Path | None = None
    qdrant_mode: str = "memory"
    qdrant_url: str | None = None
    qdrant_api_key_file: Path | None = None
    compatibility_manifest: Path | None = None
    migrations_dir: Path | None = None
    debug_enabled: bool = False
    trusted_origins: tuple[str, ...] = (
        "http://127.0.0.1:8088",
        "http://localhost:8088",
    )
    trusted_proxies: frozenset[str] = frozenset()
    trust_loopback_host_proxy: bool = False
    history_save_body: bool = True
    history_retention_days: int = 7
    local_ocr_endpoints: tuple[str, ...] = ()
    local_ocr_token_file: Path | None = None
    local_ocr_revision: str = (
        "paddleocr-3.5.0-ppocrv5-server-det-rec-paddle-static"
    )
    local_ocr_model: str = "pp-ocrv5-server"
    local_ocr_timeout_seconds: float = 35.0

    @classmethod
    def from_environment(cls) -> ProductRuntimeSettings:
        """从 P10.5 最小环境变量构造设置。

        Args:
            无参数；读取当前进程环境。

        Returns:
            已完成基本类型转换的设置。

        Raises:
            ValueError: Bootstrap Token 文件未配置。

        """
        bootstrap = os.environ.get("RAG_ADMIN_BOOTSTRAP_TOKEN_FILE")
        if not bootstrap:
            raise ValueError("必须配置 RAG_ADMIN_BOOTSTRAP_TOKEN_FILE。")
        repository_root = Path(__file__).resolve().parents[3]
        frontend = Path(
            os.environ.get(
                "RAG_FRONTEND_DIR",
                str(_discover_frontend(repository_root)),
            )
        )
        master = os.environ.get("RAG_MASTER_KEY_FILE")
        manifest = os.environ.get("RAG_COMPATIBILITY_MANIFEST")
        migrations = os.environ.get("RAG_MIGRATIONS_DIR")
        local_ocr_token = os.environ.get("RAG_OCR_API_TOKEN_FILE")
        return cls(
            data_dir=Path(os.environ.get("RAG_DATA_DIR", ".data/product")),
            frontend_dir=frontend,
            bootstrap_token_file=Path(bootstrap),
            host=os.environ.get("RAG_HOST", "127.0.0.1"),
            port=int(os.environ.get("RAG_PORT", "8088")),
            master_key_file=None if master is None else Path(master),
            qdrant_mode=os.environ.get("RAG_QDRANT_MODE", "memory"),
            qdrant_url=os.environ.get("RAG_QDRANT_URL"),
            qdrant_api_key_file=(
                None
                if os.environ.get("RAG_QDRANT_API_KEY_FILE") is None
                else Path(os.environ["RAG_QDRANT_API_KEY_FILE"])
            ),
            compatibility_manifest=(
                _discover_compatibility_manifest(repository_root)
                if manifest is None
                else Path(manifest)
            ),
            migrations_dir=(
                _discover_migrations(repository_root)
                if migrations is None
                else Path(migrations)
            ),
            debug_enabled=os.environ.get("RAG_DEBUG_ENABLED") == "true",
            trusted_origins=_parse_trusted_origins(
                os.environ.get(
                    "RAG_TRUSTED_ORIGINS",
                    "http://127.0.0.1:8088,http://localhost:8088",
                )
            ),
            trusted_proxies=_parse_trusted_proxies(
                os.environ.get("RAG_TRUSTED_PROXIES", "")
            ),
            trust_loopback_host_proxy=(
                os.environ.get("RAG_TRUST_LOOPBACK_HOST_PROXY") == "true"
            ),
            history_save_body=(
                os.environ.get("RAG_HISTORY_SAVE_BODY", "true") == "true"
            ),
            history_retention_days=int(
                os.environ.get("RAG_HISTORY_RETENTION_DAYS", "7")
            ),
            local_ocr_endpoints=_parse_local_ocr_endpoints(
                os.environ.get("RAG_OCR_ENDPOINTS")
            ),
            local_ocr_token_file=(
                None if local_ocr_token is None else Path(local_ocr_token)
            ),
            local_ocr_revision=os.environ.get(
                "RAG_OCR_REVISION",
                "paddleocr-3.5.0-ppocrv5-server-det-rec-paddle-static",
            ),
            local_ocr_model=os.environ.get("RAG_OCR_MODEL", "pp-ocrv5-server"),
            local_ocr_timeout_seconds=float(
                os.environ.get("RAG_OCR_TIMEOUT_SECONDS", "35")
            ),
        )


@dataclass(slots=True)
class _ResolvedProductServices:
    """一个不可变 Product Profile 对应的数据面资源。"""

    lifecycle: LifecycleService
    retrieval: RetrievalService
    cache: InMemoryRetrievalCache
    remote_resources: tuple[object, ...]

    def close(self) -> None:
        """关闭当前 Profile 独占的缓存与远程连接池。

        Args:
            无参数；关闭当前资源集合。

        Returns:
            无返回值。

        """
        closers: list[Callable[[], None]] = [self.cache.close]
        for resource in reversed(self.remote_resources):
            closer = getattr(resource, "close", None)
            if callable(closer):
                closers.append(closer)
        _close_callbacks(tuple(closers))


@dataclass(frozen=True, slots=True)
class _ResolvedServiceGenerationContract:
    """一次 Profile 资源代际冻结后的安全服务合同。"""

    policy: RetrievalPolicy
    egress: EgressPolicy
    serving_fingerprint: str
    resource_identity: str


@dataclass(slots=True)
class _ResourceGeneration(Generic[_ResourceT]):
    """一个可退役资源及其受 resolver 锁保护的租约状态。"""

    knowledge_base_id: str
    resource: _ResourceT
    close_callback: Callable[[], None]
    lease_count: int = 0
    retired: bool = False
    closed: bool = False


class _RetrievalLeaseProxy:
    """把旧 resolver 回调适配为覆盖完整查询调用的 lease。"""

    def __init__(
        self,
        resolver: ProductProfileResolver,
        knowledge_base_id: str,
        fallback: RetrievalService,
    ) -> None:
        self._resolver = resolver
        self._knowledge_base_id = knowledge_base_id
        self._fallback = fallback

    def search_and_answer(  # noqa: PLR0913
        self,
        request: SearchRequest,
        *,
        on_stage: Callable[[str, dict[str, object]], None] | None = None,
        on_claim: Callable[[AnswerClaim, str], None] | None = None,
        on_final: Callable[[SearchAnswerResult], None] | None = None,
        cancellation: CancellationPort | None = None,
        cache_result: bool = True,
    ) -> SearchAnswerResult:
        """在整个同步或流式检索调用期间持有当前 generation。

        Args:
            request: 已绑定 scope、owner 和回答行为的查询请求。
            on_stage: 可选的安全阶段事件回调。
            on_claim: 可选的已验证 claim 回调。
            on_final: 可选的唯一 final 回调。
            cancellation: 可选协作取消端口。
            cache_result: 是否在权威完成边界写入结果缓存。

        Returns:
            当前请求独立 Trace ID 对应的最终查询结果。

        """
        with self._resolver.retrieval_service_lease(
            self._knowledge_base_id,
            self._fallback,
        ) as service:
            identity = service.execution_identity(request)
            frozen_request = request.model_copy(
                update={
                    "expected_active_revision_id": (
                        identity.active_revision_id
                    ),
                    "expected_serving_fingerprint": (
                        identity.serving_fingerprint
                    ),
                }
            )
            with self._resolver.query_retrieval_scope(
                self._knowledge_base_id,
                identity.active_revision_id,
            ):
                if (
                    not request.singleflight_enabled
                    or on_stage is not None
                    or on_claim is not None
                    or on_final is not None
                ):
                    return service.search_and_answer(
                        frozen_request,
                        on_stage=on_stage,
                        on_claim=on_claim,
                        on_final=on_final,
                        cancellation=cancellation,
                        cache_result=cache_result,
                    )
                trace_id = request.trace_id or f"trace_{uuid.uuid4().hex}"
                frozen_request = frozen_request.model_copy(
                    update={"trace_id": trace_id}
                )
                result = self._resolver.singleflight.execute(
                    identity.key_hash,
                    request_trace_id=trace_id,
                    cancellation=cancellation,
                    compute=lambda group_cancellation: (
                        service.search_and_answer(
                            frozen_request,
                            cancellation=group_cancellation,
                            cache_result=cache_result,
                        )
                    ),
                )
                if result.singleflight_role == "follower":
                    service.validate_shared_result(
                        result,
                        frozen_request,
                        identity,
                    )
                service.record_singleflight_observation(result, identity)
                return result


class _LifecycleLeaseProxy:
    """把旧 Lifecycle resolver 回调适配为单次操作 lease。"""

    def __init__(
        self,
        lease_factory: Callable[[], AbstractContextManager[LifecycleService]],
    ) -> None:
        self._lease_factory = lease_factory

    def create_document(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        *,
        display_name: str,
        content: bytes,
        media_type: str,
        idempotency_key: str,
    ) -> Job:
        """在冻结并入队新文档期间持有当前 generation。

        Args:
            project_id: 目标项目 ID。
            knowledge_base_id: 目标知识库 ID。
            display_name: 用户可见文档名。
            content: 待上传文档字节。
            media_type: 已验证媒体类型。
            idempotency_key: 调用方幂等键。

        Returns:
            已持久化并绑定 Profile 的入库 Job。

        """
        with self._lease_factory() as service:
            return service.create_document(
                project_id,
                knowledge_base_id,
                display_name=display_name,
                content=content,
                media_type=media_type,
                idempotency_key=idempotency_key,
            )

    def create_document_version(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        *,
        content: bytes,
        media_type: str,
        idempotency_key: str,
    ) -> Job:
        """在冻结并入队文档新版本期间持有当前 generation。

        Args:
            project_id: 目标项目 ID。
            knowledge_base_id: 目标知识库 ID。
            document_id: 既有逻辑文档 ID。
            content: 待上传的新版本字节。
            media_type: 已验证媒体类型。
            idempotency_key: 调用方幂等键。

        Returns:
            已持久化并绑定 Profile 的新版本入库 Job。

        """
        with self._lease_factory() as service:
            return service.create_document_version(
                project_id,
                knowledge_base_id,
                document_id,
                content=content,
                media_type=media_type,
                idempotency_key=idempotency_key,
            )

    def run_ingestion(self, job_id: str) -> None:
        """在后台构建与激活的完整调用期间持有冻结 generation。

        Args:
            job_id: 已冻结 Profile 的入库 Job ID。

        Returns:
            无返回值。

        """
        with self._lease_factory() as service:
            service.run_ingestion(job_id)


@dataclass(frozen=True, slots=True)
class _PilotProfileBinding:
    """仅当前验收上下文使用的实际方案、索引和向量空间快照。"""

    project_id: str
    knowledge_base_id: str
    profile_revision_id: str
    index_fingerprint: str
    serving_fingerprint: str
    binding_identity: str
    index_revision_id: str
    vector_spaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ControlledPilotScope:
    """不持久化、不向普通 API 暴露的 pilot 查询准入。"""

    source_profile_id: str
    source_binding_identity: str
    profiles: tuple[_PilotProfileBinding, ...]


class _PersistentUsageBudget(LocalUsageBudget):
    """把备用查询预算预留写入 Product SQLite。"""

    def __init__(
        self,
        control: ProductControlStore,
        connection_id: str,
    ) -> None:
        self._control = control
        self._connection_id = connection_id

    def reserve(
        self,
        provider_id: str,
        operation: str,
        estimated_tokens: int,
        *,
        daily_request_limit: int,
        daily_estimated_token_limit: int,
    ) -> None:
        """跨重启原子预留备用 Provider 日预算。

        Args:
            provider_id: Provider 标识；连接已由实例冻结。
            operation: 预算操作类别。
            estimated_tokens: 本次调用的估算 Token 数。
            daily_request_limit: 每日请求上限。
            daily_estimated_token_limit: 每日估算 Token 上限。

        Returns:
            无返回值。

        """
        del provider_id
        self._control.reserve_daily_provider_budget(
            self._connection_id,
            operation,
            estimated_tokens,
            request_limit=daily_request_limit,
            token_limit=daily_estimated_token_limit,
        )


class ProductProfileResolver:
    """每次请求从 SQLite 解析知识库 Active Profile。"""

    def __init__(  # noqa: PLR0913
        self,
        control: ProductControlStore,
        providers: ProviderRuntimeRegistry,
        *,
        models: ProductModelSettings | None = None,
        corpus_authorizations: CorpusAuthorizationStore | None = None,
        retrieval_authorizations: RetrievalAuthorizationStore | None = None,
        ocr: ProductOcrEnrichment | None = None,
        circuit_factory: Callable[[], ProviderCircuitBreaker] | None = None,
        acceptance_egress_resolver: Callable[
            [RetrievalProfileRevision, EgressPolicy], EgressPolicy
        ]
        | None = None,
    ) -> None:
        """保存产品控制面。

        Args:
            control: Retrieval Profile Store。
            providers: 页面托管 Credential 的 Provider 工厂。
            models: 可选的知识库回答和 OCR 配置存储。
            corpus_authorizations: 可选的活动语料批准与预算状态存储。
            retrieval_authorizations: 可选的真实检索资料与预算批准存储。
            ocr: 可选的同库图片增补服务。
            circuit_factory: 仅测试可注入的 Circuit 工厂。
            acceptance_egress_resolver: 受信任验收入口的有效累计授权解析器。

        Returns:
            无返回值。

        """
        self._control = control
        self._providers = providers
        self._models = models
        self._corpus_authorizations = corpus_authorizations
        self._retrieval_authorizations = retrieval_authorizations
        self._ocr = ocr
        self._grounded_models: dict[
            str, _ResourceGeneration[ProductGroundedModel]
        ] = {}
        self._retired_models: dict[
            int, _ResourceGeneration[ProductGroundedModel]
        ] = {}
        self._circuit_factory = circuit_factory
        self._acceptance_egress_resolver = acceptance_egress_resolver
        self._controlled_scope: ContextVar[_ControlledPilotScope | None] = (
            ContextVar("product_controlled_pilot", default=None)
        )
        self._last_profile: dict[str, str | None] = {}
        self._runtime: P09Runtime | None = None
        self._services: dict[
            str, _ResourceGeneration[_ResolvedProductServices]
        ] = {}
        self._retired_services: dict[
            int, _ResourceGeneration[_ResolvedProductServices]
        ] = {}
        self.singleflight = ProductQuerySingleflight()
        self._lock = RLock()
        self._closed = False

    def singleflight_metrics(self) -> SingleflightMetrics:
        """返回不含查询、scope 或 key 的 singleflight 安全计数。

        Args:
            无参数；读取当前 resolver 计数。

        Returns:
            有界 singleflight 协调器的安全指标快照。

        """
        return self.singleflight.metrics()

    def bind_runtime(self, runtime: P09Runtime) -> None:
        """在基础持久运行时构造后绑定共享 Store。

        Args:
            runtime: Product Runtime 唯一拥有的 P09 基础运行时。

        Returns:
            无返回值。

        """
        if self._runtime is not None:
            raise RuntimeError("Product Profile Resolver 不允许重复绑定。")
        self._runtime = runtime
        contracts = resolved_contracts(
            runtime.retrieval_runtime.persistence.components
        )
        self._control.index_contract = {
            key: contracts[key]
            for key in (
                "parser_identity",
                "parsing_policy",
                "chunker_identity",
                "chunking_policy",
                "lexical_schema",
                "chunk_payload_schema",
            )
        }
        self._control.queue_profile = self._queue_profile

    def _queue_profile(
        self,
        profile: RetrievalProfileRevision,
        expected_profile: str | None,
        expected_index: str | None,
    ) -> None:
        validations = self._control.profile_validations(
            profile.profile_revision_id
        )
        if any(
            run is None or run.status != "succeeded"
            for run in validations.values()
        ):
            raise ValueError("候选方案的验证已失效，请重新验证。")
        if (
            not self._providers.test_only_transport
            and self._retrieval_authorizations is not None
        ):
            status = self._retrieval_authorizations.status(
                profile.profile_revision_id
            )
            if (
                status.authorization_state not in {"APPROVED", "NOT_REQUIRED"}
                or status.budget_state != "AVAILABLE"
                or status.connection_budget_state != "READY"
            ):
                reason = (
                    status.reason_codes[0]
                    if status.reason_codes
                    else "RETRIEVAL_AUTHORIZATION_REQUIRED"
                )
                raise ValueError(f"真实检索授权未就绪：{reason}")
        with self._profile_services_lease(profile) as services:
            services.lifecycle.queue_profile_rebuild(
                profile.knowledge_base_id,
                expected_profile,
                expected_index,
                tuple(
                    run.validation_id
                    for run in validations.values()
                    if run is not None
                ),
            )

    def active_profile(
        self, knowledge_base_id: str
    ) -> RetrievalProfileRevision | None:
        """读取当前 Active Profile，不在页面或进程中复制 Secret。

        Args:
            knowledge_base_id: 当前请求或作业的知识库。

        Returns:
            Active Profile；未配置时为 None。

        """
        with self._lock:
            profile = self._control.active_profile(knowledge_base_id)
            self._last_profile[knowledge_base_id] = (
                None if profile is None else profile.profile_revision_id
            )
            return profile

    def retrieval_service(
        self,
        knowledge_base_id: str,
        fallback: RetrievalService,
    ) -> RetrievalService:
        """为单次 Query 解析页面激活的数据面服务。

        Args:
            knowledge_base_id: 当前 Query 的知识库。
            fallback: P08.5 已验证的本地检索服务。

        Returns:
            当前 Profile 对应的检索服务；未配置时返回离线基线。

        """
        return cast(
            RetrievalService,
            _RetrievalLeaseProxy(self, knowledge_base_id, fallback),
        )

    def _query_generation_locked(
        self,
        knowledge_base_id: str,
        settings: KnowledgeBaseModelSettings,
        authorization_status: CorpusAuthorizationStatus | None,
    ) -> tuple[
        str | None,
        _ResourceGeneration[ProductGroundedModel] | None,
        bool,
        bool,
    ]:
        """在 resolver 锁内解析可调用模型或仅缓存身份。

        Args:
            knowledge_base_id: 当前 Query 的知识库。
            settings: 当前冻结的模型设置。
            authorization_status: 当前语料、模型与预算状态。

        Returns:
            serving identity、模型 generation、是否仅允许缓存，以及配置
            构造是否失败。

        """
        if self._models is None:
            raise RuntimeError("Product Model Settings 尚未绑定。")
        model_authorized = authorization_status is None or (
            authorization_status.corpus_authorization_state == "APPROVED"
            and authorization_status.model_authorization_state == "APPROVED"
            and authorization_status.budget_state == "AVAILABLE"
        )
        cache_identity_only = _can_reuse_generation_cache(authorization_status)
        if not settings.generation_connection_id or not (
            model_authorized or cache_identity_only
        ):
            return None, None, False, False
        try:
            generation_identity = self._models.serving_identity(settings)
            model_generation = (
                self._model_generation_locked(
                    knowledge_base_id,
                    generation_identity,
                    settings,
                )
                if model_authorized
                else None
            )
        except (RagError, ValueError, KeyError):
            return None, None, False, True
        return (
            generation_identity,
            model_generation,
            cache_identity_only,
            False,
        )

    @contextmanager
    def retrieval_service_lease(
        self,
        knowledge_base_id: str,
        fallback: RetrievalService,
    ) -> Iterator[RetrievalService]:
        """冻结并租用一次查询所需的检索与生成资源。

        Args:
            knowledge_base_id: 当前 Query 的知识库。
            fallback: 未配置 Product Profile 时的离线检索服务。

        Yields:
            在上下文退出前不会被 invalidate 提前关闭的查询服务。

        Returns:
            管理查询与回答资源 generation lease 的上下文迭代器。

        Raises:
            RuntimeError: Resolver 已关闭。

        """
        service_generation: (
            _ResourceGeneration[_ResolvedProductServices] | None
        ) = None
        model_generation: _ResourceGeneration[ProductGroundedModel] | None = (
            None
        )
        generation_identity: str | None = None
        cache_identity_only = False
        settings = KnowledgeBaseModelSettings()
        model_configuration_failed = False
        authorization_status: CorpusAuthorizationStatus | None = None
        with self._lock:
            self._ensure_open_locked()
            profile = self.active_profile(knowledge_base_id)
            service = fallback
            if profile is not None:
                service_generation = self._service_generation_locked(profile)
                service = service_generation.resource.retrieval
            if self._models is not None:
                settings = self._models.get(knowledge_base_id)
                if self._corpus_authorizations is not None:
                    authorization_status = self._corpus_authorizations.status(
                        knowledge_base_id
                    )
                (
                    generation_identity,
                    model_generation,
                    cache_identity_only,
                    model_configuration_failed,
                ) = self._query_generation_locked(
                    knowledge_base_id,
                    settings,
                    authorization_status,
                )
            base_data_plane_context = getattr(
                service, "data_plane_context", None
            )
            data_plane_context = (
                self._query_data_plane_context(
                    base_data_plane_context,
                    profile,
                    settings,
                    authorization_status=authorization_status,
                    model_configuration_failed=model_configuration_failed,
                )
                if isinstance(base_data_plane_context, QueryDataPlaneContext)
                else None
            )
            if service_generation is not None:
                self._acquire_generation_locked(service_generation)
            if model_generation is not None:
                self._acquire_generation_locked(model_generation)
        try:
            if model_generation is not None:
                if generation_identity is None:
                    raise RuntimeError(
                        "回答模型 generation 缺少 serving identity。"
                    )
                model = model_generation.resource
                rewrite_enabled = bool(
                    getattr(settings, "rewrite_enabled", False)
                )
                service = service.with_generation(
                    model,
                    serving_identity=generation_identity,
                    interpreter=model if rewrite_enabled else None,
                    rewriter=model if rewrite_enabled else None,
                )
            elif cache_identity_only and generation_identity is not None:
                service = service.with_generation_cache_identity(
                    serving_identity=generation_identity
                )
            with_data_plane = getattr(service, "with_data_plane", None)
            if data_plane_context is not None and callable(with_data_plane):
                service = with_data_plane(data_plane_context)
            yield service
        finally:
            self._release_query_generations(
                service_generation,
                model_generation,
            )

    def _query_data_plane_context(
        self,
        base: QueryDataPlaneContext,
        profile: RetrievalProfileRevision | None,
        settings: KnowledgeBaseModelSettings,
        *,
        authorization_status: CorpusAuthorizationStatus | None,
        model_configuration_failed: bool,
    ) -> QueryDataPlaneContext:
        """从当前 Profile、模型设置与批准账本解析单次查询状态。

        Args:
            base: 实际 RetrievalService 记录的组件身份。
            profile: 本请求冻结的活动 Profile；未配置时为空。
            settings: 本请求读取的知识库模型设置。
            authorization_status: 当前清单与账本的动态对账结果。
            model_configuration_failed: 模型适配器是否无法安全构造。

        Returns:
            不含 Secret 且不会触发 Provider 调用的数据面上下文。

        """
        fallback_reasons = list(base.fallback_reason_codes)
        if profile is not None:
            fallback_reasons = [
                reason
                for reason in fallback_reasons
                if reason != "NO_ACTIVE_RETRIEVAL_PROFILE"
            ]
        configuration_state = (
            "CONFIGURED"
            if settings.generation_connection_id
            else "NOT_CONFIGURED"
        )
        authorization_state = (
            "MISSING" if settings.generation_connection_id else "NOT_REQUIRED"
        )
        corpus_state = authorization_state
        budget_state = authorization_state
        if authorization_status is not None:
            configuration_state = authorization_status.model_configuration_state
            authorization_state = authorization_status.model_authorization_state
            corpus_state = authorization_status.corpus_authorization_state
            budget_state = authorization_status.budget_state
            fallback_reasons.extend(authorization_status.fallback_reason_codes)
        generation_provider_id = None
        if settings.generation_connection_id:
            try:
                generation_provider_id = self._control.get_connection(
                    settings.generation_connection_id
                ).provider_type
            except RagError:
                model_configuration_failed = True
        if model_configuration_failed:
            configuration_state = "INVALID"
            authorization_state = "BLOCKED"
            fallback_reasons.append("MODEL_RUNTIME_CONFIGURATION_INVALID")
        return replace(
            base,
            retrieval_data_plane=(
                "active_remote_profile"
                if profile is not None
                else "default_local_fallback"
            ),
            active_retrieval_profile_revision_id=(
                None if profile is None else profile.profile_revision_id
            ),
            generation_provider_id=generation_provider_id,
            generation_model=settings.generation_model,
            interpret_provider_id=(
                generation_provider_id if settings.rewrite_enabled else None
            ),
            interpret_model=(
                settings.generation_model if settings.rewrite_enabled else None
            ),
            rewrite_provider_id=(
                generation_provider_id if settings.rewrite_enabled else None
            ),
            rewrite_model=(
                settings.generation_model if settings.rewrite_enabled else None
            ),
            model_configuration_state=configuration_state,
            model_authorization_state=authorization_state,
            corpus_authorization_state=corpus_state,
            budget_state=budget_state,
            fallback_reason_codes=tuple(dict.fromkeys(fallback_reasons)),
            report_model_capability_blockers=True,
        )

    def revision_lifecycle(
        self,
        knowledge_base_id: str,
        fallback: LifecycleService,
    ) -> LifecycleService:
        """为单次入队冻结 Active Profile 对应的构建服务。

        Args:
            knowledge_base_id: 新 Revision 所属知识库。
            fallback: P09 已验证的本地 Revision 构建服务。

        Returns:
            当前 Profile 对应的构建服务；未配置时返回离线基线。

        """
        return cast(
            LifecycleService,
            _LifecycleLeaseProxy(
                lambda: self.revision_lifecycle_lease(
                    knowledge_base_id,
                    fallback,
                )
            ),
        )

    @contextmanager
    def revision_lifecycle_lease(
        self,
        knowledge_base_id: str,
        fallback: LifecycleService,
    ) -> Iterator[LifecycleService]:
        """租用新文档或版本入队时对应的 Active Profile 服务。

        Args:
            knowledge_base_id: 新 Revision 所属知识库。
            fallback: 未激活 Product Profile 时的离线 Lifecycle。

        Yields:
            当前入队操作使用且不会被提前关闭的 Lifecycle。

        Returns:
            管理入库排队资源 generation lease 的上下文迭代器。

        """
        generation: _ResourceGeneration[_ResolvedProductServices] | None = None
        with self._lock:
            self._ensure_open_locked()
            profile = self.active_profile(knowledge_base_id)
            service = fallback
            if profile is not None:
                generation = self._service_generation_locked(profile)
                self._acquire_generation_locked(generation)
                service = generation.resource.lifecycle
        try:
            yield service
        finally:
            if generation is not None:
                self._release_service_generation(generation)

    def job_lifecycle(
        self,
        job_id: str,
        fallback: LifecycleService,
    ) -> LifecycleService:
        """按持久请求冻结的 Profile 解析后台作业构建服务。

        Args:
            job_id: 即将被 Worker 领取的持久 Job。
            fallback: 离线基线 Lifecycle。

        Returns:
            入队时选择的精确 Profile 服务。

        """
        return cast(
            LifecycleService,
            _LifecycleLeaseProxy(
                lambda: self.job_lifecycle_lease(job_id, fallback)
            ),
        )

    @contextmanager
    def job_lifecycle_lease(
        self,
        job_id: str,
        fallback: LifecycleService,
    ) -> Iterator[LifecycleService]:
        """按持久 Job 冻结的 Profile 租用完整后台构建资源。

        Args:
            job_id: 即将被 Worker 领取的持久 Job。
            fallback: 未绑定 Product Profile 时的离线 Lifecycle。

        Yields:
            覆盖完整 ingestion 调用且不会被提前关闭的 Lifecycle。

        Returns:
            管理后台入库资源 generation lease 的上下文迭代器。

        """
        generation: _ResourceGeneration[_ResolvedProductServices] | None = None
        profile: RetrievalProfileRevision | None = None
        with self._lock:
            self._ensure_open_locked()
            runtime = self._require_runtime()
            profile_id = runtime.store.ingestion_profile_revision_id(job_id)
            service = fallback
            if profile_id is not None:
                profile = self._control.get_profile(profile_id)
                generation = self._service_generation_locked(profile)
                self._acquire_generation_locked(generation)
                service = generation.resource.lifecycle
        try:
            source_hashes: tuple[str, ...] | None = None
            if (
                profile is not None
                and self._retrieval_authorizations is not None
                and not self._providers.test_only_transport
            ):
                request = runtime.store.ingestion_request(job_id)
                source_hashes = tuple(
                    sorted({item.content_sha256 for item in request.documents})
                )
            with self._retrieval_scope(
                profile,
                step_id="retrieval.build",
                required_operations=("embedding.document",),
                source_hashes=source_hashes,
            ):
                yield service
        finally:
            if generation is not None:
                self._release_service_generation(generation)

    @contextmanager
    def _retrieval_scope(
        self,
        profile: RetrievalProfileRevision | None,
        *,
        step_id: str,
        required_operations: tuple[RetrievalOperation, ...],
        source_hashes: tuple[str, ...] | None = None,
        expected_index_revision_id: str | None = None,
    ) -> Iterator[None]:
        """生产远程 Profile 的完整调用链必须绑定真实资料批准。

        Args:
            profile: 本次请求或作业冻结的 Profile；本地路径为空。
            step_id: 累计预算账本中的稳定阶段身份。
            required_operations: 当前调用链可能执行的远程检索用途。
            source_hashes: 后台作业实际冻结的文档版本 SHA256。
            expected_index_revision_id: 查询已冻结的活动 Revision。

        Yields:
            已绑定预算与资料范围的执行上下文；测试 Transport 保持隔离。

        """
        if (
            profile is None
            or self._retrieval_authorizations is None
            or self._providers.test_only_transport
        ):
            yield
            return
        with self._retrieval_authorizations.scope(
            profile.profile_revision_id,
            step_id=step_id,
            required_operations=required_operations,
            source_hashes=source_hashes,
            expected_index_revision_id=expected_index_revision_id,
        ):
            yield

    @contextmanager
    def query_retrieval_scope(
        self,
        knowledge_base_id: str,
        expected_index_revision_id: str,
    ) -> Iterator[None]:
        """把查询冻结的 Revision 与当前远程检索批准绑定。

        Args:
            knowledge_base_id: 当前查询的知识库。
            expected_index_revision_id: Provider 前读取的活动 Revision。

        Yields:
            本次查询使用的资料与预算范围。

        Returns:
            上下文退出后无返回值。

        """
        profile = self.active_profile(knowledge_base_id)
        required_operations: tuple[RetrievalOperation, ...] = (
            ("embedding.query", "reranking")
            if profile is not None
            and profile.reranker_connection_id is not None
            else ("embedding.query",)
        )
        with self._retrieval_scope(
            profile,
            step_id="retrieval.query",
            required_operations=required_operations,
            expected_index_revision_id=expected_index_revision_id,
        ):
            yield

    def invalidate(self, knowledge_base_id: str | None = None) -> None:
        """退役指定知识库或全部 Profile 与回答模型 generation。

        Args:
            knowledge_base_id: 只失效该知识库；None 表示全部失效。

        Returns:
            无返回值；零租约资源已在返回前关闭。

        """
        with self._lock:
            if self._closed:
                return
            closers = self._retire_resources_locked(knowledge_base_id)
        _close_callbacks(closers)

    def close(self) -> None:
        """在 Runtime 请求生命周期结束后关闭当前和退役服务。

        Args:
            无参数；由 Runtime shutdown 调用。

        Returns:
            无返回值。

        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            closers = self._retire_resources_locked(None)
        _close_callbacks(closers)

    def _resolve(
        self, profile: RetrievalProfileRevision
    ) -> _ResolvedProductServices:
        """保留组合层检查使用的未租约服务快照。

        实际 Query、入队和 Worker 不调用此兼容入口，统一经 lease 代理执行。
        """
        with self._lock:
            self._ensure_open_locked()
            return self._service_generation_locked(profile).resource

    @contextmanager
    def _profile_services_lease(
        self, profile: RetrievalProfileRevision
    ) -> Iterator[_ResolvedProductServices]:
        """租用调用方已经冻结的 Profile generation。"""
        with self._lock:
            self._ensure_open_locked()
            generation = self._service_generation_locked(profile)
            self._acquire_generation_locked(generation)
        try:
            yield generation.resource
        finally:
            self._release_service_generation(generation)

    def _service_generation_locked(
        self, profile: RetrievalProfileRevision
    ) -> _ResourceGeneration[_ResolvedProductServices]:
        """在 resolver 锁内读取或构造一个可租用服务 generation。"""
        contract = self._service_generation_contract(profile)
        existing = self._services.get(contract.resource_identity)
        if existing is not None:
            return existing
        resolved = self._build(profile, contract)
        generation = _ResourceGeneration(
            knowledge_base_id=profile.knowledge_base_id,
            resource=resolved,
            close_callback=resolved.close,
        )
        self._services[contract.resource_identity] = generation
        return generation

    def _service_generation_contract(
        self,
        profile: RetrievalProfileRevision,
    ) -> _ResolvedServiceGenerationContract:
        """冻结连接、凭据、校准和授权共同决定的资源代际。

        Args:
            profile: 当前请求已经读取的不可变 Profile Revision。

        Returns:
            不含 Secret 字节、但会随任一资源绑定变化的服务合同。

        """
        policy, egress, base_serving = self.serving_contract(profile)
        binding_identity = self._control.quality.binding_identity(
            profile.profile_revision_id
        )
        quality_identity = canonical_sha256(
            self._control.quality.states(profile.profile_revision_id)
        )
        resource_identity = canonical_sha256(
            {
                "profile_revision_id": profile.profile_revision_id,
                "binding_identity": binding_identity,
                "quality_identity": quality_identity,
                "serving_contract": base_serving,
            }
        )
        return _ResolvedServiceGenerationContract(
            policy=policy,
            egress=egress,
            serving_fingerprint=canonical_sha256(
                {
                    "serving_contract": base_serving,
                    "resource_generation": resource_identity,
                }
            ),
            resource_identity=resource_identity,
        )

    def _model_generation_locked(
        self,
        knowledge_base_id: str,
        identity: str,
        settings: KnowledgeBaseModelSettings,
    ) -> _ResourceGeneration[ProductGroundedModel]:
        """在 resolver 锁内读取或构造回答模型 generation。"""
        if self._models is None:
            raise RuntimeError("Product Model Settings 尚未绑定。")
        key = knowledge_base_id + identity
        existing = self._grounded_models.get(key)
        if existing is not None:
            return existing
        model = ProductGroundedModel(
            settings,
            knowledge_base_id,
            self._models.connections,
            self._providers,
        )
        generation = _ResourceGeneration(
            knowledge_base_id=knowledge_base_id,
            resource=model,
            close_callback=model.close,
        )
        self._grounded_models[key] = generation
        return generation

    def _acquire_generation_locked(
        self, generation: _ResourceGeneration[_ResourceT]
    ) -> None:
        """在同一把 resolver 锁内增加有效 generation 的引用。"""
        if generation.retired or generation.closed:
            raise RuntimeError("不能租用已经退役的 Product generation。")
        generation.lease_count += 1

    def _release_service_generation(
        self,
        generation: _ResourceGeneration[_ResolvedProductServices],
    ) -> None:
        """释放服务 generation，最后一个退役引用负责锁外关闭。"""
        with self._lock:
            closer = self._release_generation_locked(
                generation,
                self._retired_services,
            )
        if closer is not None:
            closer()

    def _release_query_generations(
        self,
        services: _ResourceGeneration[_ResolvedProductServices] | None,
        model: _ResourceGeneration[ProductGroundedModel] | None,
    ) -> None:
        """原子结算查询的两类租约，再在锁外关闭退役资源。"""
        closers: list[Callable[[], None]] = []
        with self._lock:
            if model is not None:
                closer = self._release_generation_locked(
                    model,
                    self._retired_models,
                )
                if closer is not None:
                    closers.append(closer)
            if services is not None:
                closer = self._release_generation_locked(
                    services,
                    self._retired_services,
                )
                if closer is not None:
                    closers.append(closer)
        _close_callbacks(tuple(closers))

    def _release_generation_locked(
        self,
        generation: _ResourceGeneration[_ResourceT],
        retired: dict[int, _ResourceGeneration[_ResourceT]],
    ) -> Callable[[], None] | None:
        """在 resolver 锁内结算一次租约并转移 close 所有权。"""
        if generation.lease_count <= 0:
            raise RuntimeError("Product generation lease 发生重复释放。")
        generation.lease_count -= 1
        if not generation.retired or generation.lease_count:
            return None
        retired.pop(id(generation), None)
        if generation.closed:
            return None
        generation.closed = True
        return generation.close_callback

    def _retire_resources_locked(
        self, knowledge_base_id: str | None
    ) -> tuple[Callable[[], None], ...]:
        """在 resolver 锁内退役匹配资源并收集锁外 closer。"""
        closers: list[Callable[[], None]] = []
        for key, generation in tuple(self._services.items()):
            if (
                knowledge_base_id is not None
                and generation.knowledge_base_id != knowledge_base_id
            ):
                continue
            self._services.pop(key)
            closer = self._retire_generation_locked(
                generation,
                self._retired_services,
            )
            if closer is not None:
                closers.append(closer)
        for key, model_generation in tuple(self._grounded_models.items()):
            if (
                knowledge_base_id is not None
                and model_generation.knowledge_base_id != knowledge_base_id
            ):
                continue
            self._grounded_models.pop(key)
            closer = self._retire_generation_locked(
                model_generation,
                self._retired_models,
            )
            if closer is not None:
                closers.append(closer)
        if knowledge_base_id is None:
            self._last_profile.clear()
        else:
            self._last_profile.pop(knowledge_base_id, None)
        return tuple(closers)

    def _retire_generation_locked(
        self,
        generation: _ResourceGeneration[_ResourceT],
        retired: dict[int, _ResourceGeneration[_ResourceT]],
    ) -> Callable[[], None] | None:
        """标记 generation；无在途引用时立即转移 close 所有权。"""
        if generation.retired:
            return None
        generation.retired = True
        if generation.lease_count:
            retired[id(generation)] = generation
            return None
        generation.closed = True
        return generation.close_callback

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Product Profile Resolver 已关闭。")

    @contextmanager
    def _controlled_pilot(
        self,
        *,
        source_profile_id: str,
        project_id: str,
        profile_ids: tuple[str, ...],
    ) -> Iterator[None]:
        """仅对独立 pilot 索引临时启用待测语义，不写质量通过记录。

        Args:
            source_profile_id: 已授权验收的源方案。
            project_id: 本次 pilot 独立项目。
            profile_ids: 已完成库存检查的独立 pilot 方案。

        Yields:
            当前调用上下文中的待测准入；退出和异常都会恢复。

        Raises:
            ValueError: 范围不独立、方案不符或嵌套准入。

        """
        if self._controlled_scope.get() is not None:
            raise ValueError("PILOT_SCOPE_NESTED")
        source = self._control.get_profile(source_profile_id)
        if not profile_ids or len(set(profile_ids)) != len(profile_ids):
            raise ValueError("PILOT_SCOPE_INVALID")
        profiles = tuple(self._control.get_profile(key) for key in profile_ids)
        if any(
            profile.knowledge_base_id == source.knowledge_base_id
            or profile.index_semantic_fingerprint
            != source.index_semantic_fingerprint
            or profile.serving_fingerprint != source.serving_fingerprint
            for profile in profiles
        ):
            raise ValueError("PILOT_SCOPE_PROFILE_MISMATCH")
        scope = _ControlledPilotScope(
            source_profile_id=source_profile_id,
            source_binding_identity=self._control.quality.binding_identity(
                source_profile_id
            ),
            profiles=tuple(
                self._pilot_profile_binding(profile, project_id)
                for profile in profiles
            ),
        )
        token = self._controlled_scope.set(scope)
        try:
            yield
        finally:
            self._controlled_scope.reset(token)

    def _pilot_profile_binding(
        self, profile: RetrievalProfileRevision, project_id: str
    ) -> _PilotProfileBinding:
        """从实际 Active Revision 校验方案、scope 与完整向量空间。"""
        persistence = self._require_runtime().retrieval_runtime.persistence
        snapshot = persistence.control.active_query_snapshot(
            KnowledgeBaseScope(
                project_id=project_id,
                knowledge_base_id=profile.knowledge_base_id,
            ),
            serving_fingerprint=profile.serving_fingerprint,
            retrieval_policy=RetrievalPolicy.model_validate(
                dict(profile.retrieval_policy)
            ),
        )
        topology = _product_topology(
            profile_specs(profile, self._control.get_connection)
        )
        if (
            snapshot.profile_revision_id != profile.profile_revision_id
            or snapshot.revision.index_fingerprint
            != profile.index_semantic_fingerprint
            or snapshot.topology != topology
        ):
            raise ValueError("PILOT_SCOPE_INDEX_MISMATCH")
        return _PilotProfileBinding(
            project_id=project_id,
            knowledge_base_id=profile.knowledge_base_id,
            profile_revision_id=profile.profile_revision_id,
            index_fingerprint=profile.index_semantic_fingerprint,
            serving_fingerprint=profile.serving_fingerprint,
            binding_identity=self._control.quality.binding_identity(
                profile.profile_revision_id
            ),
            index_revision_id=snapshot.revision.index_revision_id,
            vector_spaces=tuple(
                slot.vector_space_identity for slot in snapshot.topology.slots
            ),
        )

    def _controlled_vector_spaces(
        self, profile: RetrievalProfileRevision
    ) -> tuple[str, ...] | None:
        """每次查询复核临时准入；其它方案保持普通生产门。"""
        scope = self._controlled_scope.get()
        if scope is None:
            return None
        target = next(
            (
                item
                for item in scope.profiles
                if item.profile_revision_id == profile.profile_revision_id
            ),
            None,
        )
        if target is None:
            return None
        if (
            self._control.quality.binding_identity(scope.source_profile_id)
            != scope.source_binding_identity
            or self._pilot_profile_binding(profile, target.project_id) != target
        ):
            raise ValueError("PILOT_SCOPE_CHANGED")
        return target.vector_spaces

    def serving_contract(
        self,
        profile: RetrievalProfileRevision,
    ) -> tuple[RetrievalPolicy, EgressPolicy, str]:
        """供 UI 回读和查询共同使用的实际服务合同。

        Args:
            profile: 已持久化的不可变方案。

        Returns:
            包含当前预算和已接受校准证据的策略、出网约束与指纹。

        """
        policy = RetrievalPolicy.model_validate(dict(profile.retrieval_policy))
        calibrated_spaces = self._control.quality.calibrated_spaces(
            profile.profile_revision_id
        )
        calibrated = bool(calibrated_spaces) and not (
            self._providers.test_only_transport
        )
        controlled = self._controlled_vector_spaces(profile)
        active_profile_spaces = (
            tuple(
                slot.vector_space_identity
                for slot in _product_topology(
                    profile_specs(profile, self._control.get_connection)
                ).slots
            )
            if profile.status == "active"
            and not self._providers.test_only_transport
            else ()
        )
        if controlled is not None:
            spaces = controlled
            readiness = "CONTROLLED_TEST_ONLY"
        elif calibrated:
            spaces = calibrated_spaces
            readiness = "LIVE_CALIBRATED"
        elif active_profile_spaces:
            # 真实验证并成功建索引的 Active Profile 已具备可执行合同；
            # P11 合成质量记录保留为附加证据，不再阻断产品检索。
            spaces = active_profile_spaces
            readiness = "ACTIVE_PROFILE"
        else:
            spaces = ()
            readiness = "UNCALIBRATED"
        policy = policy.model_copy(
            update={
                "dense_semantic_enabled": bool(spaces),
                "dense_semantic_calibration_state": readiness,
                "dense_calibrated_vector_spaces": spaces,
            }
        )
        egress = _product_egress(profile, self._control)
        if self._acceptance_egress_resolver is not None:
            egress = self._acceptance_egress_resolver(profile, egress)
        identity = canonical_sha256(
            {
                "profile_serving": profile.serving_fingerprint,
                "retrieval": policy.model_dump(mode="json"),
                "egress": egress.model_dump(mode="json"),
            }
        )
        return policy, egress, identity

    def _build(
        self,
        profile: RetrievalProfileRevision,
        contract: _ResolvedServiceGenerationContract,
    ) -> _ResolvedProductServices:
        runtime = self._require_runtime()
        persistence = runtime.retrieval_runtime.persistence
        components = persistence.components
        specs = profile_specs(profile, self._control.get_connection)
        topology = _product_topology(specs)
        primary_slot = topology.slot(topology.primary_slot_id)
        primary = self._providers.embedding_adapter(
            profile.primary_connection_id,
            slot_id=primary_slot.slot_id,
            model=primary_slot.model,
            dimension=primary_slot.dimension,
            document_policy_identity=canonical_sha256(
                primary_slot.document_request_policy
            ),
            query_policy_identity=canonical_sha256(
                primary_slot.query_request_policy
            ),
            resolved=specs[0],
        )
        embedding_providers = {primary_slot.slot_id: primary}
        remote_resources: list[object] = [primary]
        standby = None
        if topology.standby_slot_id is not None:
            standby_slot = topology.slot(topology.standby_slot_id)
            if profile.standby_connection_id is None:
                raise ValueError("双槽 Profile 缺少 Standby Connection。")
            standby = self._providers.embedding_adapter(
                profile.standby_connection_id,
                slot_id=standby_slot.slot_id,
                model=standby_slot.model,
                dimension=standby_slot.dimension,
                document_policy_identity=canonical_sha256(
                    standby_slot.document_request_policy
                ),
                query_policy_identity=canonical_sha256(
                    standby_slot.query_request_policy
                ),
                resolved=specs[1],
            )
            embedding_providers[standby_slot.slot_id] = standby
            remote_resources.append(standby)
        reranker = components.reranker
        if profile.reranker_connection_id is not None:
            if profile.reranker_model is None:
                raise ValueError("Reranker Connection 缺少模型。")
            reranker = self._providers.reranker_adapter(
                profile.reranker_connection_id,
                model=profile.reranker_model,
            )
            remote_resources.append(reranker)
        chunk_validator = getattr(
            components.chunker, "validate_persisted", None
        )
        if chunk_validator is None:
            raise TypeError("Product Chunker 必须实现持久化校验端口。")
        validator = RevisionValidator(
            persistence.control,
            components.vector_store,
            cast(ChunkValidationPort, components.chunker),
        )
        embedding = DocumentEmbeddingService(
            persistence.cache,
            persistence.control,
            embedding_providers,
        )
        contracts = resolved_contracts(components)
        contracts["embedding_topology"] = topology.model_dump(mode="json")
        vector_schema = dict(
            cast(dict[str, object], contracts["vector_schema"])
        )
        vector_schema["slots"] = [
            slot.model_dump(mode="json") for slot in topology.slots
        ]
        contracts["vector_schema"] = vector_schema
        builder = RevisionBuilder(
            document_enricher=None
            if self._ocr is None
            else self._ocr.enrich_result,
            trace=components.trace_sink,
            control=persistence.control,
            parser=components.parser,
            parsing_policy=components.parsing_policy,
            chunker=components.chunker,
            chunking_policy=components.chunking_policy,
            artifact_lifecycle=ArtifactLifecycleService(
                components.blob_store,
                persistence.control,
                cast(BlobLocatorPort, components.blob_store),
            ),
            embedding_service=embedding,
            embedding_providers=embedding_providers,
            vector_store=components.vector_store,
            validator=validator,
            slots=topology.slots,
            index_fingerprint=profile.index_semantic_fingerprint,
            resolved_contracts=contracts,
        )
        budgets = _document_budgets(profile, self._control, topology)
        lifecycle = LifecycleService(
            store=runtime.store,
            control=persistence.control,
            builder=builder,
            blob_store=components.blob_store,
            profile_id=profile.profile_revision_id,
            index_fingerprint=profile.index_semantic_fingerprint,
            budgets=budgets,
            egress_allowed_slots=frozenset(embedding_providers),
            retrieval_profile_revision_id=profile.profile_revision_id,
            content_identity=None
            if self._ocr is None
            else self._ocr.content_identity,
        )
        cache = InMemoryRetrievalCache()
        retrieval = RetrievalService(
            source=persistence.control,
            exact_store=cast(ExactStorePort, components.lexical_store),
            lexical_store=components.lexical_store,
            vector_store=components.vector_store,
            query_embedding=QueryEmbeddingRouter(
                primary,
                standby,
                circuit_breaker=(
                    None
                    if self._circuit_factory is None
                    else self._circuit_factory()
                ),
                usage_budget=(
                    None
                    if profile.standby_connection_id is None
                    else _PersistentUsageBudget(
                        self._control,
                        profile.standby_connection_id,
                    )
                ),
            ),
            reranker=reranker,
            generator=components.generator,
            trace=components.trace_sink,
            cache=cache,
            serving_fingerprint=contract.serving_fingerprint,
            egress_policy=contract.egress,
            policy=contract.policy,
            expected_index_fingerprint=profile.index_semantic_fingerprint,
            expected_profile_revision_id=profile.profile_revision_id,
        )
        return _ResolvedProductServices(
            lifecycle=lifecycle,
            retrieval=retrieval,
            cache=cache,
            remote_resources=tuple(remote_resources),
        )

    def _require_runtime(self) -> P09Runtime:
        if self._runtime is None:
            raise RuntimeError(
                "Product Profile Resolver 尚未绑定 P09 Runtime。"
            )
        return self._runtime


def _close_callbacks(callbacks: tuple[Callable[[], None], ...]) -> None:
    """尝试关闭全部资源，并在清理完成后传播首个异常。"""
    first_error: Exception | None = None
    for callback in callbacks:
        try:
            callback()
        except Exception as error:  # 关闭其余资源后仍会传播该错误。
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


@dataclass(slots=True)
class ProductRuntime:
    """拥有 P09 数据面与 P10.5 产品控制面的唯一组合根。"""

    p09: P09Runtime
    connections: SqliteConnectionFactory
    credentials: CredentialStore
    control: ProductControlStore
    auth: AuthStore
    sessions: ConsoleSessionService
    providers: ProviderRuntimeRegistry
    profiles: ProductProfileResolver
    compatibility: CompatibilityManifest
    settings: ProductRuntimeSettings
    history: ProductQueryHistory
    conversations: ProductConversationStore
    feedback: ProductFeedbackStore
    models: ProductModelSettings
    corpus_authorizations: CorpusAuthorizationStore
    retrieval_authorizations: RetrievalAuthorizationStore
    ocr: ProductOcrEnrichment
    relations: ProductDiagramRelations
    traces: ProductTraceCoordinator
    content_identity: Callable[[str], str | None]
    local_ocr_http_client: httpx.Client | None = None
    _closed: bool = False

    @property
    def sdk(self) -> RagSdk:
        """返回 P09 SDK 供稳定 API 复用。

        Args:
            无参数；读取当前 Runtime。

        Returns:
            共享的同步 SDK。

        """
        return self.p09.sdk

    @property
    def jobs(self) -> DurableJobRunner:
        """返回 Durable Job Runner。

        Args:
            无参数；读取当前 Runtime。

        Returns:
            共享的有界作业执行器。

        """
        return self.p09.jobs

    @property
    def data_dir(self) -> Path:
        """返回受控数据根。

        Args:
            无参数；读取当前 Runtime。

        Returns:
            解析后的产品数据目录。

        """
        return self.p09.data_dir

    @property
    def retrieval_runtime(self) -> P07Runtime:
        """返回 P08.5 检索 Runtime 供稳定 Probe 兼容。

        Args:
            无参数；读取当前 Runtime。

        Returns:
            Product Runtime 拥有的检索 Runtime。

        """
        return self.p09.retrieval_runtime

    def close(self) -> None:
        """按 Provider、作业与 Store 所有权顺序关闭资源。

        Args:
            无参数；幂等关闭当前 Runtime。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self.p09.jobs.close()
        self.profiles.close()
        self.providers.close()
        if self.local_ocr_http_client is not None:
            self.local_ocr_http_client.close()
        self.p09.close()
        self.traces.close()

    def __enter__(self) -> ProductRuntime:
        """进入 Product Runtime 资源作用域。"""
        return self

    def __exit__(self, *args: object) -> None:
        """离开作用域并关闭全部资源。"""
        del args
        self.close()


def build_product_runtime(  # noqa: PLR0915
    settings: ProductRuntimeSettings,
    *,
    transport_factory: TransportFactory | None = None,
    circuit_factory: Callable[[], ProviderCircuitBreaker] | None = None,
    acceptance_egress_resolver: Callable[
        [RetrievalProfileRevision, EgressPolicy], EgressPolicy
    ]
    | None = None,
    recover_jobs: bool = True,
) -> ProductRuntime:
    """迁移 SQLite 并构造完整 Product Runtime。

    Args:
        settings: P10.5 最小启动配置。
        transport_factory: 测试用 Provider MockTransport 工厂。
        circuit_factory: 测试用可控时钟 Circuit 工厂。
        acceptance_egress_resolver: 仅受信任验收入口注入的累计授权解析器。
        recover_jobs: 是否恢复已有持久作业；验收入口只运行自己的新作业。

    Returns:
        唯一拥有全部产品资源的 Runtime。

    Raises:
        ValueError: 数据目录、兼容清单或 Secret 文件不安全。

    """
    data_dir = settings.data_dir.resolve()
    if data_dir.is_symlink():
        raise ValueError("RAG_DATA_DIR 禁止 symlink。")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    compatibility = load_manifest(settings.compatibility_manifest)
    bootstrap_token = load_bootstrap_token(settings.bootstrap_token_file)
    master_key = (
        None
        if settings.master_key_file is None
        else load_master_key(settings.master_key_file)
    )
    connections = SqliteConnectionFactory(data_dir / "universal-rag.sqlite3")
    migrations = settings.migrations_dir or _discover_migrations(
        Path(__file__).resolve().parents[3]
    )
    MigrationRunner(connections, migrations).migrate()
    credential_cipher = None if master_key is None else SecretCipher(master_key)
    credentials = CredentialStore(connections, credential_cipher)
    control = ProductControlStore(connections, credentials)
    models = ProductModelSettings(connections, control)
    auth_cipher = SecretCipher(_authentication_key(bootstrap_token))
    auth = AuthStore(connections, auth_cipher)
    sessions = ConsoleSessionService(auth, bootstrap_token)
    conversations = ProductConversationStore(connections, auth_cipher)
    history = ProductQueryHistory(
        connections,
        credential_cipher or auth_cipher,
        save_body=settings.history_save_body,
        retention_days=settings.history_retention_days,
    )
    history.recover()
    product_profile = _product_profile(settings)
    # 在启动非守护 Trace writer 前完成本地 OCR 配置校验，避免失败构造泄漏线程。
    local_ocr_adapter, local_ocr_http_client = _build_local_ocr_adapter(
        settings
    )
    trace_store = TraceStore(data_dir / "product-traces.sqlite3")
    trace_store.initialize()
    trace_recorder = TraceRecorder(
        trace_store,
        audit_failure=lambda trace_id, code: history.record(
            TraceEvent(
                trace_id=trace_id,
                event_name="trace.capture_failed",
                occurred_at=datetime.now(UTC),
                attributes=freeze_json_object({"reason_code": code.value}),
            )
        ),
    )
    traces = ProductTraceCoordinator(
        history,
        trace_recorder,
        trace_store,
        connections,
        pipeline_fingerprint=canonical_sha256(
            {
                "profile": product_profile.model_dump(mode="json"),
                "operational_trace": "product-v2",
            }
        ),
        serving_fingerprint=canonical_sha256(
            product_profile.model_dump(mode="json")
        ),
        release_revision=SOURCE_REVISION,
        profile_id=product_profile.profile_id,
    )
    feedback = ProductFeedbackStore(connections, traces.set_feedback)
    feedback.recover()
    if (
        transport_factory is None
        and os.environ.get("RAG_TEST_NETWORK") == "offline"
    ):
        transport_factory = build_offline_mock_transport
    providers = ProviderRuntimeRegistry(
        credentials,
        control,
        transport_factory=transport_factory,
        budget_ledger_path=data_dir / "provider-budget.sqlite3",
        local_ocr_adapter=local_ocr_adapter,
    )
    corpus_authorizations = CorpusAuthorizationStore(
        connections,
        control,
        models,
        providers,
        data_dir / "provider-budget.sqlite3",
    )
    retrieval_authorizations = RetrievalAuthorizationStore(
        connections,
        control,
        providers,
        data_dir / "provider-budget.sqlite3",
    )
    ocr = ProductOcrEnrichment(
        connections, models, providers, data_dir / "provider-budget.sqlite3"
    )
    relations = ProductDiagramRelations(connections)

    def _content_identity(knowledge_base_id: str) -> str | None:
        identities = {
            "ocr": ocr.content_identity(knowledge_base_id),
            "diagram_relations": relations.content_identity(knowledge_base_id),
        }
        if all(value is None for value in identities.values()):
            return None
        return canonical_sha256(identities)

    def _enrich_media(parsed: ParseResult) -> ParseResult:
        return relations.enrich_result(ocr.enrich_result(parsed))

    profiles = ProductProfileResolver(
        control,
        providers,
        models=models,
        corpus_authorizations=corpus_authorizations,
        retrieval_authorizations=retrieval_authorizations,
        ocr=ocr,
        circuit_factory=circuit_factory,
        acceptance_egress_resolver=acceptance_egress_resolver,
    )

    def _status_overlay(status: SystemStatus) -> SystemStatus:
        return _product_status(
            status,
            control,
            compatibility,
            test_only_transport=providers.test_only_transport,
        )

    try:
        p09 = build_p09_runtime(
            product_profile,
            data_dir=data_dir,
            hooks=P09RuntimeHooks(
                recover_jobs=recover_jobs,
                trace_sink=traces,
                query_history=traces,
                conversation=conversations,
                document_enricher=_enrich_media,
                content_identity=_content_identity,
                retrieval_policy=RetrievalPolicy.model_validate(
                    resolve_retrieval_policy({}, {}),
                ),
                system_status_overlay=_status_overlay,
                retrieval_resolver=profiles.retrieval_service,
                revision_builder_resolver=profiles.revision_lifecycle,
                job_lifecycle_resolver=profiles.job_lifecycle,
                prepare_trace=traces.prepare,
            ),
        )
    except Exception:
        providers.close()
        if local_ocr_http_client is not None:
            local_ocr_http_client.close()
        traces.close()
        raise
    profiles.bind_runtime(p09)
    ocr.bind_blob_store(p09.retrieval_runtime.persistence.components.blob_store)
    return ProductRuntime(
        p09=p09,
        connections=connections,
        credentials=credentials,
        control=control,
        auth=auth,
        sessions=sessions,
        providers=providers,
        profiles=profiles,
        compatibility=compatibility,
        settings=settings,
        history=history,
        conversations=conversations,
        feedback=feedback,
        models=models,
        corpus_authorizations=corpus_authorizations,
        retrieval_authorizations=retrieval_authorizations,
        ocr=ocr,
        relations=relations,
        traces=traces,
        content_identity=_content_identity,
        local_ocr_http_client=local_ocr_http_client,
    )


def _build_local_ocr_adapter(
    settings: ProductRuntimeSettings,
) -> tuple[LocalProductOcrAdapter | None, httpx.Client | None]:
    """仅在端点与 0600 Bearer 文件都存在时构造内部 OCR。"""
    if not settings.local_ocr_endpoints:
        if settings.local_ocr_token_file is not None:
            raise ValueError("配置本地 OCR Token 时必须同时配置端点。")
        return None, None
    if settings.local_ocr_token_file is None:
        raise ValueError("启用本地 OCR 必须配置 RAG_OCR_API_TOKEN_FILE。")
    token = _load_local_ocr_token(settings.local_ocr_token_file)
    if settings.local_ocr_timeout_seconds <= 0:
        raise ValueError("本地 OCR timeout 必须为正数。")
    client = httpx.Client(
        timeout=httpx.Timeout(settings.local_ocr_timeout_seconds)
    )
    pool = ResilientHttpPool(
        settings.local_ocr_endpoints,
        client=client,
        policy=ResiliencePolicy(
            max_attempts=2,
            failure_threshold=2,
            cooldown_seconds=30.0,
            max_concurrency=1,
        ),
    )
    adapter = LocalProductOcrAdapter(
        LocalOcrAdapterConfig(
            revision=settings.local_ocr_revision,
            model=settings.local_ocr_model,
        ),
        OcrClient(
            pool,
            revision=settings.local_ocr_revision,
            api_token=token,
            max_input_bytes=10 * 1024 * 1024,
        ),
    )
    return adapter, client


def _load_local_ocr_token(path: Path) -> str:
    """读取单个 0600 Secret 文件，不允许目录、symlink 或短令牌。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("本地 OCR Token 必须是非 symlink 普通文件。")
    if stat.S_IMODE(path.stat().st_mode) != stat.S_IRUSR | stat.S_IWUSR:
        raise ValueError("本地 OCR Token 文件权限必须严格为 0600。")
    token = path.read_text(encoding="utf-8").strip()
    if (
        not _MIN_LOCAL_OCR_TOKEN_LENGTH
        <= len(token)
        <= _MAX_LOCAL_OCR_TOKEN_LENGTH
    ):
        raise ValueError("本地 OCR Token 长度必须在 32 到 4096。")
    return token


def _parse_local_ocr_endpoints(raw: str | None) -> tuple[str, ...]:
    """解析可选内部端点，并拒绝凭据、路径和公网 IP 字面量。"""
    if raw is None or not raw.strip():
        return ()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(
            "RAG_OCR_ENDPOINTS 必须是 JSON 字符串数组。"
        ) from error
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(item, str) for item in decoded)
    ):
        raise ValueError("RAG_OCR_ENDPOINTS 必须是非空 JSON 字符串数组。")
    endpoints = tuple(item.rstrip("/") for item in decoded)
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("本地 OCR 端点不能重复。")
    for endpoint in endpoints:
        parsed = urlparse(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "本地 OCR 端点必须是无凭据、路径或参数的 HTTP URL。"
            )
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            continue
        if not (
            address.is_loopback or address.is_private or address.is_link_local
        ):
            raise ValueError("本地 OCR 端点禁止使用公网 IP。")
    return endpoints


def _product_topology(
    specs: tuple[ResolvedEmbeddingSpec, ...],
) -> EmbeddingTopology:
    slots = tuple(
        EmbeddingSlotIdentity(
            slot_id=role.value,
            role=role,
            provider_id=spec.provider_id,
            model=spec.model,
            vector_name=f"dense_{role.value}",
            dimension=spec.dimension,
            max_input_tokens=spec.max_input_tokens,
            adapter_revision=spec.adapter_revision,
            document_request_policy=spec.document_policy,
            query_request_policy=spec.query_policy,
            normalization=spec.normalization,
        )
        for spec, role in zip(
            specs,
            (EmbeddingSlotRole.PRIMARY, EmbeddingSlotRole.STANDBY),
            strict=False,
        )
    )
    return EmbeddingTopology(
        mode="single" if len(slots) == 1 else "hot_standby",
        primary_slot_id="primary",
        standby_slot_id=None if len(slots) == 1 else "standby",
        slots=slots,
    )


def _document_budgets(
    profile: RetrievalProfileRevision,
    control: ProductControlStore,
    topology: EmbeddingTopology,
) -> dict[str, DocumentEmbeddingBudget]:
    connection_ids = [profile.primary_connection_id]
    if profile.standby_connection_id is not None:
        connection_ids.append(profile.standby_connection_id)
    return {
        slot.slot_id: DocumentEmbeddingBudget(
            max_requests=connection.request_budget,
            max_tokens=connection.token_budget,
            max_chunks=10000,
        )
        for slot, connection in zip(
            topology.slots,
            (control.get_connection(item) for item in connection_ids),
            strict=True,
        )
    }


def _product_egress(
    profile: RetrievalProfileRevision,
    control: ProductControlStore,
) -> EgressPolicy:
    connections = [control.get_connection(profile.primary_connection_id)]
    if profile.standby_connection_id is not None:
        connections.append(
            control.get_connection(profile.standby_connection_id)
        )
    providers = {item.provider_type for item in connections}
    standby = (
        None
        if profile.standby_connection_id is None
        else control.get_connection(profile.standby_connection_id)
    )
    budget = dict(profile.standby_budget)
    request_budget = _bounded_budget(
        budget.get("requests"),
        fallback=0 if standby is None else standby.request_budget,
    )
    token_budget = _bounded_budget(
        budget.get("tokens"),
        fallback=0 if standby is None else standby.token_budget,
    )
    return EgressPolicy(
        remote_document_embedding=True,
        remote_query_embedding=True,
        remote_reranking=profile.reranker_connection_id is not None,
        remote_document_embedding_jina="jina" in providers,
        remote_query_embedding_jina="jina" in providers,
        remote_reranking_jina=profile.reranker_connection_id is not None,
        remote_document_embedding_aliyun=("aliyun-model-studio" in providers),
        remote_query_embedding_aliyun=(
            "aliyun-model-studio" in providers and profile.failover_enabled
        ),
        allow_aliyun_embedding_failover=profile.failover_enabled,
        aliyun_daily_request_budget=request_budget,
        aliyun_daily_token_budget=token_budget,
    )


def _bounded_budget(value: object, *, fallback: int) -> int:
    if value is None:
        return fallback
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("Standby 预算必须为正整数。")
    return min(value, fallback) if fallback > 0 else value


def _product_profile(settings: ProductRuntimeSettings) -> RagProfile:
    base = default_offline_profile()
    vector_store = (
        "memory-vector" if settings.qdrant_mode == "memory" else "qdrant-local"
    )
    components = ComponentsProfile(
        parser="word-document-v1",
        chunker="docx-structural-v3",
        embedding_topology="deterministic-single",
        embedding_primary="deterministic",
        embedding_router="embedding-router-single",
        reranker="lexical-overlap",
        vector_store=vector_store,
        lexical_store="sqlite-fts5",
        metadata_store="sqlite-control",
        blob_store="filesystem-blob",
        generator="extractive",
        trace_sink="sqlite-product-operational-trace",
    )
    return base.model_copy(
        update={
            "profile_id": "product-runtime",
            "components": components,
            "local_data": LocalDataProfile(
                data_root=str(settings.data_dir),
                qdrant_mode=settings.qdrant_mode,
                qdrant_url=settings.qdrant_url,
                qdrant_api_key_file=(
                    None
                    if settings.qdrant_api_key_file is None
                    else str(settings.qdrant_api_key_file)
                ),
            ),
        }
    )


def _product_status(
    status: SystemStatus,
    control: ProductControlStore,
    compatibility: CompatibilityManifest,
    *,
    test_only_transport: bool = False,
) -> SystemStatus:
    evidence = control.system_evidence()
    profile_ids = evidence["active_profile_ids"]
    if not isinstance(profile_ids, list):
        raise TypeError("Active Profile IDs 必须为列表。")
    records = [
        control.profile_validations(str(profile_id))
        for profile_id in profile_ids
    ]
    states = [
        control.quality.states(str(profile_id)) for profile_id in profile_ids
    ]
    runs = [run for required in records for run in required.values()]
    connectivity = bool(runs) and all(
        run is not None
        and run.status == "succeeded"
        and run.validation_mode == "live"
        for run in runs
    )
    calibrated = bool(states) and all(
        item.get("retrieval_quality_verified") == "live" for item in states
    )
    ready = (
        connectivity
        and calibrated
        and not test_only_transport
        and not status.reindex_required
        and status.integrity_status == "ok"
        and not evidence["reindex_required"]
        and all(
            item.get("local_contract_verified") == "offline"
            and item.get("offline_evaluation_ready") == "offline"
            and all(
                item.get(kind) == "live"
                for kind in (
                    "provider_connectivity_verified",
                    "dual_slot_function_verified",
                    "release_candidate_verified",
                )
            )
            for item in states
        )
    )
    validations = {
        key: {
            "status": "not_verified" if run is None else run.status,
            "validation_mode": "unknown"
            if run is None
            else run.validation_mode,
        }
        for required in records
        for key, run in required.items()
    }
    operation_states = []
    for role in ("primary", "standby", "reranker"):
        selected: list[ProviderValidationRun | None] = []
        for profile_id, required in zip(profile_ids, records, strict=True):
            profile = control.get_profile(str(profile_id))
            connection_id = getattr(profile, f"{role}_connection_id")
            if connection_id is not None:
                selected.extend(
                    run
                    for key, run in required.items()
                    if key.startswith(connection_id + ":")
                )
        if not selected or any(
            run is None or run.status != "succeeded" for run in selected
        ):
            operation_states.append("not_verified")
        else:
            operation_states.append(
                "live_validated"
                if all(
                    run is not None and run.validation_mode == "live"
                    for run in selected
                )
                else "mock_validated"
            )
    return status.model_copy(
        update={
            "active_profile_count": len(profile_ids),
            "active_revision_schema": (
                f"{compatibility.chunk_schema}/{compatibility.fts_schema}"
            ),
            "offline_evaluation_v3_ready": bool(states)
            and all(
                item.get("offline_evaluation_ready") == "offline"
                for item in states
            ),
            "remote_dense_confidence_calibrated": calibrated
            and not test_only_transport,
            "primary_live_evaluation_status": operation_states[0],
            "standby_live_evaluation_status": operation_states[1],
            "reranker_live_evaluation_status": operation_states[2],
            "provider_validation_statuses": freeze_json_object(validations),
            "reindex_required": status.reindex_required
            or bool(evidence["reindex_required"]),
            "remote_production_profile_ready": bool(ready),
            "runtime_identity": "product-runtime-p10.5",
        }
    )


def _authentication_key(bootstrap_token: str) -> MasterKey:
    value = hashlib.sha256(
        b"rag-console-auth-key-v1\x00" + bootstrap_token.encode("utf-8")
    ).digest()
    return MasterKey(
        value=value,
        key_id=f"sha256:{hashlib.sha256(value).hexdigest()}",
    )


def _discover_frontend(repository_root: Path) -> Path:
    image_frontend = Path("/app/frontend")
    if image_frontend.is_dir():
        return image_frontend
    return repository_root / "frontend" / "dist"


def _discover_migrations(repository_root: Path) -> Path:
    image_migrations = Path("/app/migrations/universal_rag")
    if image_migrations.is_dir():
        return image_migrations
    return repository_root / "migrations" / "universal_rag"


def _discover_compatibility_manifest(repository_root: Path) -> Path | None:
    for candidate in (
        Path("/app/compatibility-manifest.json"),
        repository_root / "compatibility-manifest.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def _parse_trusted_origins(value: str) -> tuple[str, ...]:
    origins: list[str] = []
    for item in (part.strip().rstrip("/") for part in value.split(",")):
        if not item:
            continue
        parsed = urlparse(item)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("RAG_TRUSTED_ORIGINS 包含不安全 Origin。")
        origins.append(item)
    if not origins:
        raise ValueError("RAG_TRUSTED_ORIGINS 至少包含一个完整 Origin。")
    return tuple(dict.fromkeys(origins))


def _parse_trusted_proxies(value: str) -> frozenset[str]:
    proxies: set[str] = set()
    for item in (part.strip() for part in value.split(",")):
        if not item:
            continue
        try:
            proxies.add(str(ipaddress.ip_address(item)))
        except ValueError:
            raise ValueError(
                "RAG_TRUSTED_PROXIES 只接受明确的 IP 地址。"
            ) from None
    return frozenset(proxies)


__all__ = [
    "ProductProfileResolver",
    "ProductRuntime",
    "ProductRuntimeSettings",
    "build_product_runtime",
]
