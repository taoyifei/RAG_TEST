from dataclasses import dataclass

import pytest

from rag_app.adapters.providers import (
    AliyunQwen37EmbeddingAdapter,
    JinaRerankerV35Adapter,
    JinaV5TextEmbeddingAdapter,
)
from rag_app.application.engine import RagEngine
from rag_app.composition import (
    ComponentRegistry,
    build_components,
    default_hot_standby_profile,
    default_offline_profile,
    register_builtin_components,
)
from rag_app.composition.profiles import profile_from_mapping
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    CapabilityMismatch,
    CapabilityUnavailable,
    ComponentNotRegistered,
    ConfigurationError,
)
from rag_app.core.models import ChunkingPolicy
from rag_app.core.policies import ParsingPolicy


@dataclass
class _CloseProbe:
    descriptor: ComponentDescriptor
    close_count: int = 0

    def close(self) -> None:
        self.close_count += 1


class _EmbeddingProbe(_CloseProbe):
    @property
    def capabilities(self) -> ComponentCapabilities:
        return self.descriptor.capabilities


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    register_builtin_components(registry)
    return registry


def test_offline_factory_builds_fingerprints_and_closes_once() -> None:
    components = build_components(default_offline_profile(), _registry())
    assert components.index_fingerprint.startswith("sha256:")
    assert components.serving_fingerprint.startswith("sha256:")
    vector_store = components.vector_store
    components.close()
    components.close()
    assert vector_store.close_count == 1


def test_hot_standby_builds_two_declared_providers_without_network_calls() -> (
    None
):
    with build_components(
        default_hot_standby_profile(),
        _registry(),
    ) as components:
        topology = components.embedding_topology
        assert tuple(slot.slot_id for slot in topology.slots) == (
            "primary",
            "standby",
        )
        assert components.embedding_primary.health().checked_network is False
        assert components.embedding_standby is not None
        assert components.embedding_standby.health().checked_network is False


def test_explicit_python_override_wins_and_is_auditable() -> None:
    probe = _EmbeddingProbe(
        descriptor=ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name="host-embedding",
            version="1",
            mode=ProviderMode.LOCAL,
            capabilities=ComponentCapabilities(dimensions=(8,)),
        )
    )
    components = build_components(
        default_offline_profile(),
        _registry(),
        overrides={"embedding_primary": probe},
    )
    assert components.embedding_primary is probe
    assert "host-embedding" in {
        item.name for item in components.component_info()
    }
    components.close()
    assert probe.close_count == 1


def test_capability_mismatch_fails_and_closes_override_once() -> None:
    probe = _EmbeddingProbe(
        descriptor=ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name="wrong-dimension",
            version="1",
            mode=ProviderMode.LOCAL,
            capabilities=ComponentCapabilities(dimensions=(7,)),
        )
    )
    with pytest.raises(CapabilityMismatch):
        build_components(
            default_offline_profile(),
            _registry(),
            overrides={"embedding_primary": probe},
        )
    assert probe.close_count == 1


def test_partial_factory_failure_closes_already_created_resource_once() -> None:
    probe = _CloseProbe(
        descriptor=ComponentDescriptor(
            kind=ComponentKind.VECTOR_STORE,
            name="host-vector",
            version="1",
            mode=ProviderMode.LOCAL,
        )
    )
    profile = default_offline_profile()
    components = profile.components.model_copy(
        update={"lexical_store": "missing"}
    )
    invalid_profile = profile.model_copy(update={"components": components})
    with pytest.raises(ComponentNotRegistered):
        build_components(
            invalid_profile,
            _registry(),
            overrides={"vector_store": probe},
        )
    assert probe.close_count == 1


def test_failover_flag_requires_permissions_and_positive_budgets() -> None:
    profile = default_offline_profile()
    security = profile.security.model_copy(
        update={"allow_aliyun_embedding_failover": True}
    )
    invalid_profile = profile.model_copy(update={"security": security})
    with pytest.raises(ConfigurationError):
        build_components(invalid_profile, _registry())


def test_engine_reports_components_and_unmigrated_capabilities() -> None:
    engine = RagEngine.from_components(
        build_components(default_offline_profile(), _registry())
    )
    assert engine.component_info()
    assert engine.health()
    with pytest.raises(CapabilityUnavailable):
        engine.search(object())
    engine.close()


def test_unknown_override_field_is_rejected_before_construction() -> None:
    with pytest.raises(ConfigurationError):
        build_components(
            default_offline_profile(),
            _registry(),
            overrides={"service_locator": object()},
        )


def test_required_hot_standby_fragment_passes_composition_validation() -> None:
    profile = profile_from_mapping(
        {
            "schema_version": "1",
            "profile_id": "candidate",
            "components": default_hot_standby_profile().components.model_dump(
                mode="json"
            ),
        }
    )
    with build_components(profile, _registry()) as components:
        assert components.embedding_topology.mode == "hot_standby"


def test_resolved_policies_drive_fingerprint_and_topology_limits() -> None:
    offline = default_offline_profile().model_copy(
        update={
            "parsing": ParsingPolicy(hidden_text="include"),
            "chunking": ChunkingPolicy(target_tokens=256),
        }
    )
    with build_components(offline, _registry()) as components:
        assert components.parsing_policy.hidden_text == "include"
        assert components.chunking_policy.target_tokens == 256
        assert components.chunking_policy.required_embedding_slots == (
            "primary",
        )
        assert dict(
            components.chunking_policy.max_embedding_tokens_by_slot
        ) == {"primary": 32768}
        changed_fingerprint = components.index_fingerprint

    with build_components(default_offline_profile(), _registry()) as baseline:
        assert baseline.index_fingerprint != changed_fingerprint

    with build_components(
        default_hot_standby_profile(), _registry()
    ) as hot_standby:
        assert hot_standby.chunking_policy.required_embedding_slots == (
            "primary",
            "standby",
        )
        assert dict(
            hot_standby.chunking_policy.max_embedding_tokens_by_slot
        ) == {"primary": 32768, "standby": 128000}


def test_profile_fields_reach_provider_configs_without_network() -> None:
    with build_components(
        default_hot_standby_profile(), _registry()
    ) as components:
        primary = components.embedding_primary
        standby = components.embedding_standby
        reranker = components.reranker
        assert isinstance(primary, JinaV5TextEmbeddingAdapter)
        assert isinstance(standby, AliyunQwen37EmbeddingAdapter)
        assert isinstance(reranker, JinaRerankerV35Adapter)
        assert primary.config.model == "jina-embeddings-v5-text-small"
        assert primary.config.document_task == "retrieval.passage"
        assert primary.config.query_task == "retrieval.query"
        assert primary.config.embedding_type == "float"
        assert primary.config.api_key_env == "JINA_API_KEY"
        assert standby.config.transport == "dashscope-native"
        assert standby.config.document_text_type == "document"
        assert standby.config.query_text_type == "query"
        assert standby.config.output_type == "dense"
        assert standby.config.workspace_id_env == (
            "ALIYUN_MODEL_STUDIO_WORKSPACE_ID"
        )
        assert reranker.config.api_key_env == "JINA_API_KEY"
        assert reranker.config.max_candidates == 100
        assert components.query_embedding_router is not None
        assert components.query_embedding_router.descriptor.name == (
            "query-embedding-router-hot-standby"
        )
