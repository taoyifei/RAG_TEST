"""可信代码显式填充的组件 Registry。"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, ValidationError

from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    ComponentNotRegistered,
    ConfigurationError,
    Conflict,
)
from rag_app.core.models.common import JsonObject, freeze_json_object

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ComponentFactory = Callable[[], object] | Callable[[JsonObject], object]


class EmptyComponentConfig(BaseModel):
    """拒绝未知字段的空组件配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class _Registration:
    descriptor: ComponentDescriptor
    factory: ComponentFactory
    config_model: type[BaseModel]

    def _create(self, config: object) -> object:
        try:
            validated = self.config_model.model_validate(config or {})
        except ValidationError as error:
            paths = tuple(
                ".".join(str(part) for part in item["loc"])
                for item in error.errors(include_url=False)
            )
            raise ConfigurationError(
                "组件配置验证失败。",
                stage="composition.registry",
                details={"paths": list(paths)},
            ) from None
        payload = freeze_json_object(validated.model_dump(mode="json"))
        parameter_count = len(inspect.signature(self.factory).parameters)
        if parameter_count == 0:
            factory_without_config = self.factory
            return factory_without_config()  # type: ignore[call-arg]
        factory_with_config = self.factory
        return factory_with_config(payload)  # type: ignore[call-arg]


class ComponentRegistry:
    """按固定职责保存显式工厂，绝不执行动态 import。"""

    def __init__(self) -> None:
        """创建空 Registry。

        Args:
            无参数；Registry 不自动发现任何组件。

        Returns:
            无返回值。

        """
        self._registrations: dict[tuple[ComponentKind, str], _Registration] = {}

    def register_parser(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Parser factory。

        Args:
            name: 安全小写注册名。
            factory: 无网络副作用的同步工厂。
            descriptor: 可选完整描述符。
            config_model: factory 运行前使用的严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.PARSER, name, factory, descriptor, config_model
        )

    def register_chunker(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Chunker factory。

        Args:
            name: 安全小写注册名。
            factory: 无网络副作用的同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.CHUNKER, name, factory, descriptor, config_model
        )

    def register_embedding(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Embedding factory。

        Args:
            name: 安全小写注册名。
            factory: 无隐式网络的同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.EMBEDDING, name, factory, descriptor, config_model
        )

    def register_embedding_router(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Embedding Router factory。

        Args:
            name: 安全小写注册名。
            factory: 无网络副作用的同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.EMBEDDING_ROUTER,
            name,
            factory,
            descriptor,
            config_model,
        )

    def register_reranker(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Reranker factory。

        Args:
            name: 安全小写注册名。
            factory: 无隐式网络的同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.RERANKER, name, factory, descriptor, config_model
        )

    def register_vector_store(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Vector Store factory。

        Args:
            name: 安全小写注册名。
            factory: 同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.VECTOR_STORE, name, factory, descriptor, config_model
        )

    def register_lexical_store(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Lexical Store factory。

        Args:
            name: 安全小写注册名。
            factory: 同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.LEXICAL_STORE, name, factory, descriptor, config_model
        )

    def register_metadata_store(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Metadata Store factory。

        Args:
            name: 安全小写注册名。
            factory: 同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.METADATA_STORE,
            name,
            factory,
            descriptor,
            config_model,
        )

    def register_blob_store(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Blob Store factory。

        Args:
            name: 安全小写注册名。
            factory: 同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.BLOB_STORE, name, factory, descriptor, config_model
        )

    def register_generator(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Generator factory。

        Args:
            name: 安全小写注册名。
            factory: 无隐式网络的同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.GENERATOR, name, factory, descriptor, config_model
        )

    def register_trace_sink(
        self,
        name: str,
        factory: ComponentFactory,
        *,
        descriptor: ComponentDescriptor | None = None,
        config_model: type[BaseModel] = EmptyComponentConfig,
    ) -> None:
        """显式注册 Trace Sink factory。

        Args:
            name: 安全小写注册名。
            factory: 同步工厂。
            descriptor: 可选完整描述符。
            config_model: 严格配置 schema。

        Returns:
            无返回值。

        """
        self._register(
            ComponentKind.TRACE_SINK, name, factory, descriptor, config_model
        )

    def get_parser(self, name: str, config: object = None) -> object:
        """创建已注册 Parser。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Parser 实例。

        """
        return self._create(ComponentKind.PARSER, name, config)

    def get_chunker(self, name: str, config: object = None) -> object:
        """创建已注册 Chunker。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Chunker 实例。

        """
        return self._create(ComponentKind.CHUNKER, name, config)

    def get_embedding(self, name: str, config: object = None) -> object:
        """创建已注册 Embedding Provider。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Provider 实例。

        """
        return self._create(ComponentKind.EMBEDDING, name, config)

    def get_embedding_router(self, name: str, config: object = None) -> object:
        """创建已注册 Embedding Router。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Router 实例。

        """
        return self._create(ComponentKind.EMBEDDING_ROUTER, name, config)

    def get_reranker(self, name: str, config: object = None) -> object:
        """创建已注册 Reranker。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Reranker 实例。

        """
        return self._create(ComponentKind.RERANKER, name, config)

    def get_vector_store(self, name: str, config: object = None) -> object:
        """创建已注册 Vector Store。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Store 实例。

        """
        return self._create(ComponentKind.VECTOR_STORE, name, config)

    def get_lexical_store(self, name: str, config: object = None) -> object:
        """创建已注册 Lexical Store。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Store 实例。

        """
        return self._create(ComponentKind.LEXICAL_STORE, name, config)

    def get_metadata_store(self, name: str, config: object = None) -> object:
        """创建已注册 Metadata Store。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Store 实例。

        """
        return self._create(ComponentKind.METADATA_STORE, name, config)

    def get_blob_store(self, name: str, config: object = None) -> object:
        """创建已注册 Blob Store。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Store 实例。

        """
        return self._create(ComponentKind.BLOB_STORE, name, config)

    def get_generator(self, name: str, config: object = None) -> object:
        """创建已注册 Generator。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Generator 实例。

        """
        return self._create(ComponentKind.GENERATOR, name, config)

    def get_trace_sink(self, name: str, config: object = None) -> object:
        """创建已注册 Trace Sink。

        Args:
            name: 注册名。
            config: 组件配置。

        Returns:
            新 Sink 实例。

        """
        return self._create(ComponentKind.TRACE_SINK, name, config)

    def descriptor(self, kind: ComponentKind, name: str) -> ComponentDescriptor:
        """读取注册描述符但不运行 factory。

        Args:
            kind: 组件职责。
            name: 注册名。

        Returns:
            不含 secret 的描述符。

        """
        return self._registration(kind, name).descriptor

    def list_components(self) -> tuple[ComponentDescriptor, ...]:
        """列出来源、版本、mode 和 capability。

        Args:
            无参数；读取当前 Registry。

        Returns:
            按 kind/name 排序的安全描述符。

        """
        return tuple(
            registration.descriptor
            for _, registration in sorted(
                self._registrations.items(),
                key=lambda item: (item[0][0].value, item[0][1]),
            )
        )

    def _register(
        self,
        kind: ComponentKind,
        name: str,
        factory: ComponentFactory,
        descriptor: ComponentDescriptor | None,
        config_model: type[BaseModel],
    ) -> None:
        _validate_name(name)
        parameter_count = len(inspect.signature(factory).parameters)
        if parameter_count > 1:
            raise ConfigurationError(
                "组件 factory 只能接受零个参数或一个已验证配置。",
                stage="composition.registry",
            )
        key = (kind, name)
        if key in self._registrations:
            raise Conflict(
                "组件注册名已经存在。",
                stage="composition.registry",
                details={"kind": kind.value, "name": name},
            )
        resolved = descriptor or ComponentDescriptor(
            kind=kind,
            name=name,
            version="1",
            mode=ProviderMode.LOCAL,
            capabilities=ComponentCapabilities(),
        )
        if resolved.kind is not kind or resolved.name != name:
            raise ConfigurationError(
                "组件描述符与注册位置不匹配。",
                stage="composition.registry",
            )
        self._registrations[key] = _Registration(
            descriptor=resolved,
            factory=factory,
            config_model=config_model,
        )

    def _registration(self, kind: ComponentKind, name: str) -> _Registration:
        _validate_name(name)
        registration = self._registrations.get((kind, name))
        if registration is None:
            raise ComponentNotRegistered(
                "组件未显式注册。",
                stage="composition.registry",
                details={"kind": kind.value, "name": name},
            )
        return registration

    def _create(self, kind: ComponentKind, name: str, config: object) -> object:
        return self._registration(kind, name)._create(config)


def _validate_name(name: str) -> None:
    if _SAFE_NAME.fullmatch(name) is None:
        raise ConfigurationError(
            "组件名必须是安全小写短名。",
            stage="composition.registry",
            details={"name": name},
        )


def register_builtin_components(registry: ComponentRegistry) -> None:
    """显式注册 P02 内置离线组件和真实远程 adapters。

    Args:
        registry: 必须由调用方创建的空或未冲突 Registry。

    Returns:
        无返回值；所有 factory 仅构造本地对象，不执行网络。

    """
    from rag_app.adapters.chunkers import (  # noqa: PLC0415
        DocxStructuralChunker,
    )
    from rag_app.adapters.legacy.contracts import (  # noqa: PLC0415
        LegacyDocxParserAdapter,
        LegacySectionChunkerAdapter,
    )
    from rag_app.adapters.legacy.providers import (  # noqa: PLC0415
        DeterministicEmbeddingProvider,
        EmbeddingAdapterConfig,
        ExtractiveGenerator,
        HotStandbyRouter,
        LexicalOverlapReranker,
        SingleSlotRouter,
    )
    from rag_app.adapters.legacy.stores import (  # noqa: PLC0415
        InMemoryBlobStore,
        InMemoryLexicalStore,
        InMemoryVectorStore,
        SqliteMetadataStore,
        SqliteTraceSink,
    )
    from rag_app.adapters.parsers import (  # noqa: PLC0415
        DocxOoxmlV4Parser,
        LegacyDocxIrParser,
        WordDocumentV1Parser,
    )
    from rag_app.adapters.stores import (  # noqa: PLC0415
        FilesystemBlobStore,
        MemoryRevisionVectorStore,
        QdrantRevisionVectorStore,
        SqliteControlStore,
        SqliteFtsStore,
    )
    from rag_app.composition.builtin_providers import (  # noqa: PLC0415
        register_builtin_provider_components,
    )
    from rag_app.composition.persistent import (  # noqa: PLC0415
        LocalPersistenceConfig,
        filesystem_blob_factory,
        memory_revision_vector_factory,
        qdrant_local_factory,
        sqlite_control_factory,
        sqlite_fts_factory,
    )
    from rag_app.core.models import ChunkingPolicy  # noqa: PLC0415

    registry.register_parser(
        "docx-ooxml-v4",
        lambda: DocxOoxmlV4Parser(),  # noqa: PLW0108
        descriptor=DocxOoxmlV4Parser.descriptor,
    )
    registry.register_parser(
        "word-document-v1",
        lambda: WordDocumentV1Parser(),  # noqa: PLW0108
        descriptor=WordDocumentV1Parser.descriptor,
    )
    registry.register_parser(
        "legacy-docx-ir",
        lambda: LegacyDocxIrParser(),  # noqa: PLW0108
        descriptor=LegacyDocxIrParser.descriptor,
    )
    registry.register_parser(
        "legacy-docx",
        lambda: LegacyDocxParserAdapter(),  # noqa: PLW0108
        descriptor=LegacyDocxParserAdapter.descriptor,
    )
    registry.register_chunker(
        "docx-structural-v3",
        lambda config: DocxStructuralChunker(
            policy=ChunkingPolicy.model_validate(dict(config))
        ),
        descriptor=DocxStructuralChunker.descriptor,
        config_model=ChunkingPolicy,
    )
    registry.register_chunker(
        "legacy-section-pack",
        LegacySectionChunkerAdapter,
        descriptor=LegacySectionChunkerAdapter.descriptor,
    )
    deterministic_descriptor = ComponentDescriptor(
        kind=ComponentKind.EMBEDDING,
        name="deterministic",
        version="deterministic-sha256-v1",
        mode=ProviderMode.DETERMINISTIC,
        capabilities=ComponentCapabilities(
            supports_batch=True,
            dimensions=(8,),
            roles=("document", "query"),
        ),
    )
    registry.register_embedding(
        "deterministic",
        lambda config: DeterministicEmbeddingProvider(
            slot_id=str(dict(config)["slot_id"]),
            dimension=int(dict(config)["dimension"]),
            model=str(dict(config)["model"]),
            request_policy_identity=str(
                dict(config)["request_policy_identity"]
            ),
            document_request_policy_identity=str(
                dict(config)["document_request_policy_identity"]
            ),
            query_request_policy_identity=str(
                dict(config)["query_request_policy_identity"]
            ),
        ),
        descriptor=deterministic_descriptor,
        config_model=EmbeddingAdapterConfig,
    )
    register_builtin_provider_components(registry)
    registry.register_embedding_router(
        "embedding-router-single",
        SingleSlotRouter,
        descriptor=SingleSlotRouter.descriptor,
    )
    registry.register_embedding_router(
        "embedding-router-hot-standby",
        HotStandbyRouter,
        descriptor=HotStandbyRouter.descriptor,
    )
    registry.register_reranker(
        "lexical-overlap",
        LexicalOverlapReranker,
        descriptor=LexicalOverlapReranker.descriptor,
    )
    registry.register_vector_store(
        "memory",
        InMemoryVectorStore,
        descriptor=InMemoryVectorStore.descriptor,
    )
    registry.register_vector_store(
        "memory-vector",
        memory_revision_vector_factory,
        descriptor=MemoryRevisionVectorStore.descriptor,
    )
    registry.register_vector_store(
        "qdrant-local",
        qdrant_local_factory,
        descriptor=QdrantRevisionVectorStore.descriptor,
        config_model=LocalPersistenceConfig,
    )
    registry.register_lexical_store(
        "memory",
        InMemoryLexicalStore,
        descriptor=InMemoryLexicalStore.descriptor,
    )
    registry.register_lexical_store(
        "sqlite-fts5",
        sqlite_fts_factory,
        descriptor=SqliteFtsStore.descriptor,
        config_model=LocalPersistenceConfig,
    )
    registry.register_metadata_store(
        "sqlite",
        SqliteMetadataStore,
        descriptor=SqliteMetadataStore.descriptor,
    )
    registry.register_metadata_store(
        "sqlite-control",
        sqlite_control_factory,
        descriptor=SqliteControlStore.descriptor,
        config_model=LocalPersistenceConfig,
    )
    registry.register_blob_store(
        "local",
        InMemoryBlobStore,
        descriptor=InMemoryBlobStore.descriptor,
    )
    registry.register_blob_store(
        "filesystem-blob",
        filesystem_blob_factory,
        descriptor=FilesystemBlobStore.descriptor,
        config_model=LocalPersistenceConfig,
    )
    registry.register_generator(
        "extractive",
        ExtractiveGenerator,
        descriptor=ExtractiveGenerator.descriptor,
    )
    registry.register_trace_sink(
        "sqlite",
        SqliteTraceSink,
        descriptor=SqliteTraceSink.descriptor,
    )
