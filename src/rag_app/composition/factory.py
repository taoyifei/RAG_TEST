"""Profile、Registry 和显式 overrides 的唯一 Composition Root。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Protocol, cast, runtime_checkable

from rag_app.application.embedding_router import QueryEmbeddingRouter
from rag_app.composition.profiles import (
    EmbeddingSlotProfile,
    EmbeddingTopologyProfile,
    RagProfile,
    RerankerProfile,
    load_profile,
)
from rag_app.composition.registry import ComponentRegistry
from rag_app.core.capabilities import ComponentDescriptor, ComponentKind
from rag_app.core.errors import CapabilityMismatch, ConfigurationError
from rag_app.core.fingerprints import (
    IndexFingerprintInput,
    ServingFingerprintInput,
    compute_index_fingerprint,
    compute_serving_fingerprint,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ChunkingPolicy,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    EmbeddingTopology,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.policies import CircuitBreakerPolicy, ParsingPolicy
from rag_app.core.ports import (
    BlobStorePort,
    ChunkerPort,
    EmbeddingPort,
    GeneratorPort,
    LexicalStorePort,
    MetadataStorePort,
    ParserPort,
    RerankerPort,
    SlotEligibilityPort,
    TracePort,
    VectorStorePort,
)

_COMPONENT_FIELDS = frozenset(
    {
        "parser",
        "chunker",
        "embedding_primary",
        "embedding_standby",
        "embedding_router",
        "reranker",
        "vector_store",
        "lexical_store",
        "metadata_store",
        "blob_store",
        "generator",
        "trace_sink",
    }
)
_HOT_STANDBY_SLOT_COUNT = 2


@runtime_checkable
class _Closeable(Protocol):
    def close(self) -> None:
        """释放资源。

        Args:
            无参数；关闭当前资源。

        Returns:
            无返回值。

        """
        ...


@dataclass(slots=True)
class RagComponents:
    """宿主显式持有且可整体关闭的组件集合。"""

    profile: RagProfile
    parser: ParserPort
    chunker: ChunkerPort
    embedding_primary: EmbeddingPort
    embedding_standby: EmbeddingPort | None
    slot_eligibility: SlotEligibilityPort
    query_embedding_router: QueryEmbeddingRouter | None
    reranker: RerankerPort
    vector_store: VectorStorePort
    lexical_store: LexicalStorePort
    metadata_store: MetadataStorePort
    blob_store: BlobStorePort
    generator: GeneratorPort
    trace_sink: TracePort
    embedding_topology: EmbeddingTopology
    parsing_policy: ParsingPolicy
    chunking_policy: ChunkingPolicy
    index_fingerprint: str
    serving_fingerprint: str
    descriptors: tuple[ComponentDescriptor, ...]
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def embedding_router(self) -> SlotEligibilityPort:
        """返回 P01-P05 兼容的静态 slot eligibility 对象。

        Args:
            无参数；读取当前装配结果。

        Returns:
            不执行 Provider 调用的静态 eligibility 端口。

        """
        return self.slot_eligibility

    def component_info(self) -> tuple[ComponentDescriptor, ...]:
        """返回不含 secret 的组件清单。

        Args:
            无参数；读取当前装配结果。

        Returns:
            按职责顺序保存的描述符。

        """
        return self.descriptors

    def close(self) -> None:
        """按依赖逆序且每个实例最多一次地关闭资源。

        Args:
            无参数；关闭当前组件集合。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        _close_resources(
            (
                self.trace_sink,
                self.generator,
                self.reranker,
                self.embedding_standby,
                self.embedding_primary,
                self.chunker,
                self.parser,
                self.blob_store,
                self.metadata_store,
                self.lexical_store,
                self.vector_store,
            )
        )

    def __enter__(self) -> RagComponents:
        """进入宿主管理的资源作用域。

        Args:
            无参数；返回当前实例。

        Returns:
            当前组件集合。

        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """离开作用域并关闭全部资源。"""
        del exc_type, exc_value, traceback
        self.close()


def build_components(
    profile: RagProfile | str | Path,
    registry: ComponentRegistry,
    overrides: Mapping[str, object] | None = None,
) -> RagComponents:
    """验证并按固定顺序装配全部 P01 组件。

    Args:
        profile: 已验证 Profile 或严格 JSON 文件路径。
        registry: 由可信代码显式注册的 Registry。
        overrides: 宿主按字段名直接注入的显式实例。

    Returns:
        带两类指纹和幂等生命周期的组件集合。

    Raises:
        ConfigurationError: override、Profile 或安全策略无效。
        CapabilityMismatch: slot 与 Provider 能力不匹配。

    """
    resolved_profile = (
        profile if isinstance(profile, RagProfile) else load_profile(profile)
    )
    resolved_overrides = dict(overrides or {})
    unknown_overrides = sorted(set(resolved_overrides) - _COMPONENT_FIELDS)
    if unknown_overrides:
        raise ConfigurationError(
            "overrides 包含未知组件字段。",
            stage="composition.factory",
            details={"fields": unknown_overrides},
        )
    _validate_egress_policy(resolved_profile)
    topology = _resolve_topology(resolved_profile)
    parsing_policy = resolved_profile.parsing
    chunking_policy = _resolve_chunking_policy(
        resolved_profile.chunking,
        topology,
    )
    created: list[object] = []
    try:
        vector_store = cast(
            VectorStorePort,
            _create(
                "vector_store",
                resolved_profile.components.vector_store,
                registry.get_vector_store,
                resolved_overrides,
                created,
                config=_persistent_config(
                    resolved_profile,
                    resolved_profile.components.vector_store,
                ),
            ),
        )
        lexical_store = cast(
            LexicalStorePort,
            _create(
                "lexical_store",
                resolved_profile.components.lexical_store,
                registry.get_lexical_store,
                resolved_overrides,
                created,
                config=_persistent_config(
                    resolved_profile,
                    resolved_profile.components.lexical_store,
                ),
            ),
        )
        metadata_store = cast(
            MetadataStorePort,
            _create(
                "metadata_store",
                resolved_profile.components.metadata_store,
                registry.get_metadata_store,
                resolved_overrides,
                created,
                config=_persistent_config(
                    resolved_profile,
                    resolved_profile.components.metadata_store,
                ),
            ),
        )
        blob_store = cast(
            BlobStorePort,
            _create(
                "blob_store",
                resolved_profile.components.blob_store,
                registry.get_blob_store,
                resolved_overrides,
                created,
                config=_persistent_config(
                    resolved_profile,
                    resolved_profile.components.blob_store,
                ),
            ),
        )
        parser = cast(
            ParserPort,
            _create(
                "parser",
                resolved_profile.components.parser,
                registry.get_parser,
                resolved_overrides,
                created,
            ),
        )
        chunker = cast(
            ChunkerPort,
            _create(
                "chunker",
                resolved_profile.components.chunker,
                registry.get_chunker,
                resolved_overrides,
                created,
                config=(
                    chunking_policy
                    if resolved_profile.components.chunker
                    == "docx-structural-v3"
                    else None
                ),
            ),
        )
        embedding_primary = cast(
            EmbeddingPort,
            _embedding_component(
                "embedding_primary",
                topology.slots[0],
                resolved_profile,
                registry,
                resolved_overrides,
                created,
            ),
        )
        embedding_standby: EmbeddingPort | None = None
        if len(topology.slots) == _HOT_STANDBY_SLOT_COUNT:
            embedding_standby = cast(
                EmbeddingPort,
                _embedding_component(
                    "embedding_standby",
                    topology.slots[1],
                    resolved_profile,
                    registry,
                    resolved_overrides,
                    created,
                ),
            )
        slot_eligibility = cast(
            SlotEligibilityPort,
            _create(
                "embedding_router",
                resolved_profile.components.embedding_router,
                registry.get_embedding_router,
                resolved_overrides,
                created,
            ),
        )
        query_embedding_router = (
            QueryEmbeddingRouter(embedding_primary, embedding_standby)
            if embedding_standby is not None
            else None
        )
        reranker_name, reranker_config, reranker_model = _reranker_config(
            resolved_profile
        )
        reranker = cast(
            RerankerPort,
            _create(
                "reranker",
                reranker_name,
                registry.get_reranker,
                resolved_overrides,
                created,
                config=reranker_config,
            ),
        )
        generator = cast(
            GeneratorPort,
            _create(
                "generator",
                resolved_profile.components.generator,
                registry.get_generator,
                resolved_overrides,
                created,
            ),
        )
        trace_sink = cast(
            TracePort,
            _create(
                "trace_sink",
                resolved_profile.components.trace_sink,
                registry.get_trace_sink,
                resolved_overrides,
                created,
            ),
        )
        _validate_capabilities(topology, embedding_primary, embedding_standby)
        descriptors = tuple(
            _descriptor(component)
            for component in (
                parser,
                chunker,
                embedding_primary,
                embedding_standby,
                slot_eligibility,
                query_embedding_router,
                reranker,
                vector_store,
                lexical_store,
                metadata_store,
                blob_store,
                generator,
                trace_sink,
            )
            if component is not None
        )
        index_input = _index_fingerprint_input(
            topology,
            parsing_policy=parsing_policy,
            chunking_policy=chunking_policy,
            parser=_descriptor(parser),
            chunker=chunker,
            vector_store=_descriptor(vector_store),
            lexical_store=_descriptor(lexical_store),
        )
        serving_input = _serving_fingerprint_input(
            topology,
            router=(
                _descriptor(query_embedding_router)
                if query_embedding_router is not None
                else _descriptor(slot_eligibility)
            ),
            reranker=_descriptor(reranker),
            reranker_model=reranker_model,
            reranker_policy=reranker_config or {},
            generator=_descriptor(generator),
        )
        return RagComponents(
            profile=resolved_profile,
            parser=parser,
            chunker=chunker,
            embedding_primary=embedding_primary,
            embedding_standby=embedding_standby,
            slot_eligibility=slot_eligibility,
            query_embedding_router=query_embedding_router,
            reranker=reranker,
            vector_store=vector_store,
            lexical_store=lexical_store,
            metadata_store=metadata_store,
            blob_store=blob_store,
            generator=generator,
            trace_sink=trace_sink,
            embedding_topology=topology,
            parsing_policy=parsing_policy,
            chunking_policy=chunking_policy,
            index_fingerprint=compute_index_fingerprint(index_input),
            serving_fingerprint=compute_serving_fingerprint(serving_input),
            descriptors=descriptors,
        )
    except Exception:
        _close_resources(tuple(reversed(created)))
        raise


def _create(  # noqa: PLR0913
    field_name: str,
    registry_name: str,
    getter: object,
    overrides: Mapping[str, object],
    created: list[object],
    *,
    config: object = None,
) -> object:
    override = overrides.get(field_name)
    if override is not None:
        created.append(override)
        return override
    if not callable(getter):
        raise TypeError("registry getter 必须可调用。")
    component = getter(registry_name, config)
    created.append(component)
    return component


def _persistent_config(profile: RagProfile, component_name: str) -> object:
    if component_name not in {
        "qdrant-local",
        "sqlite-fts5",
        "sqlite-control",
        "filesystem-blob",
    }:
        return None
    return profile.local_data.model_dump(mode="json")


def _embedding_component(  # noqa: PLR0913, PLR0917
    field_name: str,
    slot: EmbeddingSlotIdentity,
    profile: RagProfile,
    registry: ComponentRegistry,
    overrides: Mapping[str, object],
    created: list[object],
) -> object:
    if field_name not in overrides:
        registered = registry.descriptor(
            ComponentKind.EMBEDDING,
            slot.provider_id,
        )
        dimensions = registered.capabilities.dimensions
        if dimensions and slot.dimension not in dimensions:
            raise CapabilityMismatch(
                "Embedding slot 维度不在注册能力中。",
                stage="composition.capability",
                details={"slot_id": slot.slot_id},
            )
    config = {
        "slot_id": slot.slot_id,
        "provider_id": slot.provider_id,
        "model": slot.model,
        "dimension": slot.dimension,
        "request_policy_identity": canonical_sha256(
            {
                "document": slot.document_request_policy,
                "query": slot.query_request_policy,
            }
        ),
        "document_request_policy_identity": canonical_sha256(
            slot.document_request_policy
        ),
        "query_request_policy_identity": canonical_sha256(
            slot.query_request_policy
        ),
        "document_egress_allowed": _embedding_egress_allowed(
            profile, slot, document=True
        ),
        "query_egress_allowed": _embedding_egress_allowed(
            profile, slot, document=False
        ),
    }
    slot_profile = _embedding_slot_profile(profile, slot.slot_id)
    if slot_profile is not None and slot.provider_id != "deterministic":
        config.update(
            {
                "adapter_revision": slot.adapter_revision,
                "normalization": slot.normalization,
                "max_input_tokens": slot.max_input_tokens,
            }
        )
        if slot_profile.api_key_env is not None:
            config["api_key_env"] = slot_profile.api_key_env
        optional_fields = (
            "document_task",
            "query_task",
            "embedding_type",
            "transport",
            "document_text_type",
            "query_text_type",
            "query_instruct",
            "output_type",
            "workspace_id_env",
            "region",
            "region_env",
        )
        for name in optional_fields:
            value = getattr(slot_profile, name)
            if value is not None:
                config[name] = value
    return _create(
        field_name,
        slot.provider_id,
        registry.get_embedding,
        overrides,
        created,
        config=config,
    )


def _resolve_topology(profile: RagProfile) -> EmbeddingTopology:
    configured = profile.components.embedding_topology
    if isinstance(configured, EmbeddingTopologyProfile):
        return configured.to_core()
    if configured != "deterministic-single":
        raise ConfigurationError(
            "未知的内置 embedding topology。",
            stage="composition.factory",
        )
    provider = profile.components.embedding_primary or "deterministic"
    return EmbeddingTopology(
        mode="single",
        primary_slot_id="primary",
        slots=(
            EmbeddingSlotIdentity(
                slot_id="primary",
                role=EmbeddingSlotRole.PRIMARY,
                provider_id=provider,
                model="deterministic-sha256-v1",
                vector_name="dense_primary",
                dimension=8,
                max_input_tokens=32768,
                document_request_policy=freeze_json_object(
                    {"role": "document"}
                ),
                query_request_policy=freeze_json_object({"role": "query"}),
                normalization="l2",
                adapter_revision="deterministic-v1",
            ),
        ),
    )


def _reranker_config(
    profile: RagProfile,
) -> tuple[str, object, str]:
    configured = profile.components.reranker
    if isinstance(configured, RerankerProfile):
        return (
            configured.provider,
            {
                "model": configured.model,
                "api_key_env": configured.api_key_env,
                "max_total_tokens": configured.max_total_tokens,
                "max_candidates": configured.max_candidates,
                "request_policy_revision": configured.request_policy_revision,
                "egress_allowed": (
                    profile.security.remote_reranking
                    and profile.security.remote_reranking_jina
                ),
            },
            configured.model,
        )
    return configured, None, configured


def _embedding_egress_allowed(
    profile: RagProfile,
    slot: EmbeddingSlotIdentity,
    *,
    document: bool,
) -> bool:
    security = profile.security
    if document:
        general = security.remote_document_embedding
        specific = (
            security.remote_document_embedding_jina
            if slot.provider_id == "jina-embedding"
            else security.remote_document_embedding_aliyun
        )
    else:
        general = security.remote_query_embedding
        specific = (
            security.remote_query_embedding_jina
            if slot.provider_id == "jina-embedding"
            else security.remote_query_embedding_aliyun
        )
    return general and specific


def _validate_egress_policy(profile: RagProfile) -> None:
    security = profile.security
    if security.allow_aliyun_embedding_failover and not (
        security.remote_query_embedding
        and security.remote_query_embedding_aliyun
        and security.aliyun_daily_request_budget > 0
        and security.aliyun_daily_token_budget > 0
    ):
        raise ConfigurationError(
            "启用阿里自动备用必须同时授权 query 出网并设置正预算。",
            stage="composition.security",
        )


def _validate_capabilities(
    topology: EmbeddingTopology,
    primary: EmbeddingPort,
    standby: EmbeddingPort | None,
) -> None:
    providers = (primary,) if standby is None else (primary, standby)
    if len(providers) != len(topology.slots):
        raise CapabilityMismatch(
            "Embedding Provider 数量与 topology 不匹配。",
            stage="composition.capability",
        )
    for slot, provider in zip(topology.slots, providers, strict=True):
        dimensions = provider.capabilities.dimensions
        if dimensions and slot.dimension not in dimensions:
            raise CapabilityMismatch(
                "Embedding slot 维度不在 Provider 能力中。",
                stage="composition.capability",
                details={"slot_id": slot.slot_id},
            )


def _descriptor(component: object) -> ComponentDescriptor:
    descriptor = getattr(component, "descriptor", None)
    if not isinstance(descriptor, ComponentDescriptor):
        raise CapabilityMismatch(
            "组件实例缺少有效 descriptor。",
            stage="composition.capability",
        )
    return descriptor


def _index_fingerprint_input(  # noqa: PLR0913
    topology: EmbeddingTopology,
    *,
    parsing_policy: ParsingPolicy,
    chunking_policy: ChunkingPolicy,
    parser: ComponentDescriptor,
    chunker: object,
    vector_store: ComponentDescriptor,
    lexical_store: ComponentDescriptor,
) -> IndexFingerprintInput:
    chunker_descriptor = _descriptor(chunker)
    counter = getattr(chunker, "token_counter", None)
    counter_probe = counter.count("") if counter is not None else None
    return IndexFingerprintInput(
        parser=parser,
        parsing_policy=freeze_json_object(
            parsing_policy.model_dump(mode="json", exclude_none=False)
        ),
        ir_schema_version="1",
        chunker=chunker_descriptor,
        chunker_parameters=freeze_json_object(
            chunking_policy.model_dump(mode="json", exclude_none=False)
        ),
        token_counter_identity=(
            counter_probe.tokenizer_id
            if counter_probe is not None
            else "legacy-tokenizer-v1"
        ),
        token_count_exact=(
            counter_probe.exact if counter_probe is not None else False
        ),
        embedding_slots=topology.slots,
        lexical_schema=freeze_json_object(
            {"store": lexical_store.name, "rank_semantics": "rank"}
        ),
        vector_schema=freeze_json_object(
            {
                "store": vector_store.name,
                "named_vectors": [
                    {
                        "slot_id": slot.slot_id,
                        "vector_name": slot.vector_name,
                        "dimension": slot.dimension,
                    }
                    for slot in topology.slots
                ],
                "distance": "cosine",
            }
        ),
        chunk_payload_schema=freeze_json_object(
            {
                "schema_version": (
                    "3"
                    if chunker_descriptor.name == "docx-structural-v3"
                    else "1"
                )
            }
        ),
    )


def _embedding_slot_profile(
    profile: RagProfile,
    slot_id: str,
) -> EmbeddingSlotProfile | None:
    configured = profile.components.embedding_topology
    if not isinstance(configured, EmbeddingTopologyProfile):
        return None
    candidates = (configured.primary, configured.standby)
    return next(
        (
            candidate
            for candidate in candidates
            if candidate is not None and candidate.slot_id == slot_id
        ),
        None,
    )


def _resolve_chunking_policy(
    configured: ChunkingPolicy,
    topology: EmbeddingTopology,
) -> ChunkingPolicy:
    required_slots = tuple(slot.slot_id for slot in topology.slots)
    limits = tuple(
        (slot.slot_id, slot.max_input_tokens) for slot in topology.slots
    )
    return configured.model_copy(
        update={
            "required_embedding_slots": required_slots,
            "max_embedding_tokens_by_slot": limits,
            "profile_hard_cap": min(
                configured.profile_hard_cap,
                *(limit for _, limit in limits),
            ),
        }
    )


def _serving_fingerprint_input(  # noqa: PLR0913
    topology: EmbeddingTopology,
    *,
    router: ComponentDescriptor,
    reranker: ComponentDescriptor,
    reranker_model: str,
    reranker_policy: object,
    generator: ComponentDescriptor,
) -> ServingFingerprintInput:
    return ServingFingerprintInput(
        query_analyzer=freeze_json_object({"id": "legacy-query-analyzer"}),
        query_planner=freeze_json_object({"id": "legacy-query-planner"}),
        query_expansion_policy=freeze_json_object({"enabled": False}),
        embedding_query_policies=freeze_json_object(
            {
                slot.slot_id: dict(slot.query_request_policy)
                for slot in topology.slots
            }
        ),
        embedding_router=router,
        circuit_breaker=CircuitBreakerPolicy(),
        retrieval_channels=freeze_json_object(
            {"exact": True, "fts5": True, "dense": True}
        ),
        fusion=freeze_json_object({"method": "rrf"}),
        reranker=reranker,
        reranker_model=reranker_model,
        reranker_policy=freeze_json_object(reranker_policy),
        rerank_mode="provider_or_explicit_bypass",
        neighbor_parent_expansion=freeze_json_object({"enabled": True}),
        evidence_policy=freeze_json_object({"id": "legacy-evidence"}),
        confidence_policy=freeze_json_object({"id": "legacy-confidence"}),
        generator=generator,
        generator_policy=freeze_json_object({"id": "profile"}),
        citation_protocol=freeze_json_object({"schema_version": "1"}),
    )


def _close_resources(resources: tuple[object | None, ...]) -> None:
    closed_ids: set[int] = set()
    for resource in resources:
        if resource is None or id(resource) in closed_ids:
            continue
        closed_ids.add(id(resource))
        if isinstance(resource, _Closeable):
            resource.close()
