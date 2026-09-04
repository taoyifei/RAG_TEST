"""P10.5 唯一 Product Runtime 组合根。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from rag_app.adapters.stores import MigrationRunner, SqliteConnectionFactory
from rag_app.application.durable_jobs import DurableJobRunner
from rag_app.application.lifecycle import LifecycleService
from rag_app.application.retrieval import RetrievalService
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
from rag_app.core.models import SystemStatus
from rag_app.core.models.common import freeze_json_object
from rag_app.product.auth import (
    AuthStore,
    ConsoleSessionService,
    load_bootstrap_token,
)
from rag_app.product.compatibility import CompatibilityManifest, load_manifest
from rag_app.product.control_store import ProductControlStore
from rag_app.product.credential_store import CredentialStore
from rag_app.product.crypto import MasterKey, SecretCipher, load_master_key
from rag_app.product.models import RetrievalProfileRevision
from rag_app.product.provider_runtime import (
    ProviderRuntimeRegistry,
    TransportFactory,
    build_offline_mock_transport,
)
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
        )


class ProductProfileResolver:
    """每次请求从 SQLite 解析知识库 Active Profile。"""

    def __init__(self, control: ProductControlStore) -> None:
        """保存产品控制面。

        Args:
            control: Retrieval Profile Store。

        Returns:
            无返回值。

        """
        self._control = control
        self._last_profile: dict[str, str | None] = {}

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
        """为单次 Query 解析服务，并保留 FTS 基础模式。

        P10.5 的远程 Profile 只完成配置、验证和版本绑定。真实 Provider 未经
        P11 授权时，查询继续使用已激活 Revision 的本地 FTS/Exact 安全通道。

        Args:
            knowledge_base_id: 当前 Query 的知识库。
            fallback: P08.5 已验证的本地检索服务。

        Returns:
            当前请求可安全使用的检索服务。

        """
        self.active_profile(knowledge_base_id)
        return fallback

    def revision_lifecycle(
        self,
        knowledge_base_id: str,
        fallback: LifecycleService,
    ) -> LifecycleService:
        """为单次构建作业解析 Active Profile 与安全基础构建器。

        Args:
            knowledge_base_id: 新 Revision 所属知识库。
            fallback: P09 已验证的本地 Revision 构建服务。

        Returns:
            P10.5 不联网边界内可安全使用的构建服务。

        """
        self.active_profile(knowledge_base_id)
        return fallback


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
) -> ProductRuntime:
    """迁移 SQLite 并构造完整 Product Runtime。

    Args:
        settings: P10.5 最小启动配置。
        transport_factory: 测试用 Provider MockTransport 工厂。

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
    )
    profiles = ProductProfileResolver(control)

    def _status_overlay(status: SystemStatus) -> SystemStatus:
        return _product_status(status, control, compatibility)

    try:
        p09 = build_p09_runtime(
            _product_profile(settings),
            data_dir=data_dir,
            hooks=P09RuntimeHooks(
                system_status_overlay=_status_overlay,
                retrieval_resolver=profiles.retrieval_service,
                revision_builder_resolver=profiles.revision_lifecycle,
            ),
        )
    except Exception:
        providers.close()
        raise
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


def _product_profile(settings: ProductRuntimeSettings) -> RagProfile:
    base = default_offline_profile()
    vector_store = (
        "memory-vector" if settings.qdrant_mode == "memory" else "qdrant-local"
    )
    components = ComponentsProfile(
        parser="docx-ooxml-v4",
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
) -> SystemStatus:
    evidence = control.system_evidence()
    validations = evidence["provider_validation_statuses"]
    if not isinstance(validations, dict):
        raise TypeError("Provider validation evidence 必须为 object。")
    primary = _validation_state(validations, "jina:embedding.query")
    standby = _validation_state(
        validations, "aliyun-model-studio:embedding.query"
    )
    reranker = _validation_state(validations, "jina:reranking")
    live_ready = (
        all(item == "live_validated" for item in (primary, standby, reranker))
        and status.remote_dense_confidence_calibrated
    )
    return status.model_copy(
        update={
            "active_profile_count": evidence["active_profile_count"],
            "active_revision_schema": (
                f"{compatibility.chunk_schema}/{compatibility.fts_schema}"
            ),
            "offline_evaluation_v3_ready": compatibility.fts_schema == "fts-v2",
            "primary_live_evaluation_status": primary,
            "provider_validation_statuses": freeze_json_object(validations),
            "reindex_required": (
                status.reindex_required or bool(evidence["reindex_required"])
            ),
            "remote_production_profile_ready": live_ready,
            "reranker_live_evaluation_status": reranker,
            "runtime_identity": "product-runtime-p10.5",
            "standby_live_evaluation_status": standby,
        }
    )


def _validation_state(
    validations: dict[str, object],
    key: str,
) -> str:
    value = validations.get(key)
    if not isinstance(value, dict) or value.get("status") != "succeeded":
        return "not_verified"
    category = str(value.get("http_category", ""))
    return (
        "live_validated" if category.startswith("live_") else "mock_validated"
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


__all__ = [
    "ProductProfileResolver",
    "ProductRuntime",
    "ProductRuntimeSettings",
    "build_product_runtime",
]
