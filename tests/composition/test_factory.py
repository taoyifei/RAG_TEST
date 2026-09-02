from dataclasses import dataclass

import pytest

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
