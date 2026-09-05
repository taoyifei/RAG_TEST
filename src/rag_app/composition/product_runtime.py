"""P10.5 唯一 Product Runtime 组合根。"""

from __future__ import annotations

import hashlib
import ipaddress
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import cast
from urllib.parse import urlparse

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
from rag_app.application.retrieval import RetrievalService
from rag_app.application.revision_builder import RevisionBuilder
from rag_app.application.revision_validator import RevisionValidator
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
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    DocumentEmbeddingBudget,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    EmbeddingTopology,
    RetrievalPolicy,
    SystemStatus,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.policies import EgressPolicy
from rag_app.core.ports import ChunkValidationPort, ExactStorePort
from rag_app.product.auth import (
    AuthStore,
    ConsoleSessionService,
    load_bootstrap_token,
)
from rag_app.product.compatibility import CompatibilityManifest, load_manifest
from rag_app.product.control_store import ProductControlStore
from rag_app.product.credential_store import CredentialStore
from rag_app.product.crypto import MasterKey, SecretCipher, load_master_key
from rag_app.product.models import (
    ProviderValidationRun,
    RetrievalProfileRevision,
)
from rag_app.product.provider_runtime import (
    ProviderRuntimeRegistry,
    TransportFactory,
    build_offline_mock_transport,
)
from rag_app.product.resolved_profile import ResolvedEmbeddingSpec
from rag_app.product.verification import profile_specs
from rag_app.sdk import RagSdk


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
        self.cache.close()
        for resource in reversed(self.remote_resources):
            closer = getattr(resource, "close", None)
            if callable(closer):
                closer()


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

    def __init__(
        self,
        control: ProductControlStore,
        providers: ProviderRuntimeRegistry,
        *,
        circuit_factory: Callable[[], ProviderCircuitBreaker] | None = None,
    ) -> None:
        """保存产品控制面。

        Args:
            control: Retrieval Profile Store。
            providers: 页面托管 Credential 的 Provider 工厂。
            circuit_factory: 仅测试可注入的 Circuit 工厂。

        Returns:
            无返回值。

        """
        self._control = control
        self._providers = providers
        self._circuit_factory = circuit_factory
        self._last_profile: dict[str, str | None] = {}
        self._runtime: P09Runtime | None = None
        self._services: dict[str, _ResolvedProductServices] = {}
        self._retired_services: list[_ResolvedProductServices] = []
        self._lock = RLock()

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
        self._resolve(profile).lifecycle.queue_profile_rebuild(
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
        profile = self.active_profile(knowledge_base_id)
        if profile is None:
            return fallback
        return self._resolve(profile).retrieval

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
        profile = self.active_profile(knowledge_base_id)
        if profile is None:
            return fallback
        return self._resolve(profile).lifecycle

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
        runtime = self._require_runtime()
        profile_id = runtime.store.ingestion_profile_revision_id(job_id)
        if profile_id is None:
            return fallback
        return self._resolve(self._control.get_profile(profile_id)).lifecycle

    def invalidate(self) -> None:
        """退役全部 Profile 缓存，供 Credential 轮换后重建。

        Args:
            无参数；清理 Resolver 自有资源。

        Returns:
            无返回值。

        """
        with self._lock:
            # 已分发查询和作业仍持有旧服务；在 Runtime 停止后统一关闭。
            self._retired_services.extend(self._services.values())
            self._services.clear()

    def close(self) -> None:
        """在 Runtime 请求生命周期结束后关闭当前和退役服务。

        Args:
            无参数；由 Runtime shutdown 调用。

        Returns:
            无返回值。

        """
        self.invalidate()
        with self._lock:
            for item in self._retired_services:
                item.close()
            self._retired_services.clear()

    def _resolve(
        self, profile: RetrievalProfileRevision
    ) -> _ResolvedProductServices:
        with self._lock:
            cache_key = self._control.quality.binding_identity(
                profile.profile_revision_id
            )
            cache_key = profile.profile_revision_id + cache_key
            cache_key += canonical_sha256(
                self._control.quality.states(profile.profile_revision_id)
            )
            existing = self._services.get(cache_key)
            if existing is not None:
                return existing
            resolved = self._build(profile)
            self._services[cache_key] = resolved
            return resolved

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
        spaces = self._control.quality.calibrated_spaces(
            profile.profile_revision_id
        )
        calibrated = bool(spaces) and not self._providers.test_only_transport
        policy = policy.model_copy(
            update={
                "dense_semantic_enabled": calibrated,
                "dense_semantic_calibration_state": "LIVE_CALIBRATED"
                if calibrated
                else "UNCALIBRATED",
                "dense_calibrated_vector_spaces": spaces if calibrated else (),
            }
        )
        egress = _product_egress(profile, self._control)
        identity = canonical_sha256(
            {
                "profile_serving": profile.serving_fingerprint,
                "retrieval": policy.model_dump(mode="json"),
                "egress": egress.model_dump(mode="json"),
            }
        )
        return policy, egress, identity

    def _build(
        self, profile: RetrievalProfileRevision
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
        )
        cache = InMemoryRetrievalCache()
        policy, egress, serving_fingerprint = self.serving_contract(profile)
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
            serving_fingerprint=serving_fingerprint,
            egress_policy=egress,
            policy=policy,
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
        self.p09.close()

    def __enter__(self) -> ProductRuntime:
        """进入 Product Runtime 资源作用域。"""
        return self

    def __exit__(self, *args: object) -> None:
        """离开作用域并关闭全部资源。"""
        del args
        self.close()


def build_product_runtime(
    settings: ProductRuntimeSettings,
    *,
    transport_factory: TransportFactory | None = None,
    circuit_factory: Callable[[], ProviderCircuitBreaker] | None = None,
    recover_jobs: bool = True,
) -> ProductRuntime:
    """迁移 SQLite 并构造完整 Product Runtime。

    Args:
        settings: P10.5 最小启动配置。
        transport_factory: 测试用 Provider MockTransport 工厂。
        circuit_factory: 测试用可控时钟 Circuit 工厂。
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
    auth_cipher = SecretCipher(_authentication_key(bootstrap_token))
    auth = AuthStore(connections, auth_cipher)
    sessions = ConsoleSessionService(auth, bootstrap_token)
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
    )
    profiles = ProductProfileResolver(
        control,
        providers,
        circuit_factory=circuit_factory,
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
            _product_profile(settings),
            data_dir=data_dir,
            hooks=P09RuntimeHooks(
                recover_jobs=recover_jobs,
                system_status_overlay=_status_overlay,
                retrieval_resolver=profiles.retrieval_service,
                revision_builder_resolver=profiles.revision_lifecycle,
                job_lifecycle_resolver=profiles.job_lifecycle,
            ),
        )
    except Exception:
        providers.close()
        raise
    profiles.bind_runtime(p09)
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
    )


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
        trace_sink="sqlite",
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
