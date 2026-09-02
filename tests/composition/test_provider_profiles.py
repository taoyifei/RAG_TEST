from __future__ import annotations

import json

import pytest

from rag_app.adapters.providers import (
    AliyunQwen37EmbeddingAdapter,
    JinaRerankerV35Adapter,
    JinaV5TextEmbeddingAdapter,
)
from rag_app.composition import (
    ComponentRegistry,
    build_components,
    load_profile,
    register_builtin_components,
)
from rag_app.composition.provider_profiles import (
    PROFILE_DIRECTORY,
    load_provider_catalog,
)


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    register_builtin_components(registry)
    return registry


def test_catalog_contains_only_verified_fixed_defaults() -> None:
    catalog = load_provider_catalog()
    assert catalog["verified_at"] == "2026-09-01"
    rendered = json.dumps(catalog)
    assert "jina-embeddings-v5-text-small" in rendered
    assert "jina-reranker-v3.5" in rendered
    assert "qwen3.7-text-embedding" in rendered
    assert "price" not in rendered.casefold()
    assert "free" not in rendered.casefold()
    assert "api_key" not in rendered.casefold()


def test_offline_profile_builds_without_remote_provider() -> None:
    profile = load_profile(PROFILE_DIRECTORY / "dev-offline.json")
    with build_components(profile, _registry()) as components:
        assert components.embedding_primary.descriptor.name == "deterministic"
        assert components.embedding_standby is None


def test_hot_standby_profile_builds_real_adapters_without_network() -> None:
    profile = load_profile(
        PROFILE_DIRECTORY / "dev-jina-qwen37-hot-standby.json"
    )
    with build_components(profile, _registry()) as components:
        assert isinstance(
            components.embedding_primary, JinaV5TextEmbeddingAdapter
        )
        assert isinstance(
            components.embedding_standby, AliyunQwen37EmbeddingAdapter
        )
        assert isinstance(components.reranker, JinaRerankerV35Adapter)
        assert components.embedding_topology.slots[0].normalization == "l2-v1"
        assert components.embedding_topology.slots[1].normalization == "l2-v1"
        assert components.embedding_primary.health().checked_network is False


def test_jina_only_profile_has_no_standby() -> None:
    profile = load_profile(PROFILE_DIRECTORY / "dev-jina-only.json")
    with build_components(profile, _registry()) as components:
        assert isinstance(
            components.embedding_primary, JinaV5TextEmbeddingAdapter
        )
        assert components.embedding_standby is None
        assert components.embedding_topology.mode == "single"


def test_profile_export_contains_env_names_but_not_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "must-not-appear")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "also-must-not-appear")
    profile = load_profile(
        PROFILE_DIRECTORY / "dev-jina-qwen37-hot-standby.json"
    )
    rendered = json.dumps(profile.redacted_dict())
    assert "JINA_API_KEY" in rendered
    assert "DASHSCOPE_API_KEY" in rendered
    assert "must-not-appear" not in rendered
    assert "also-must-not-appear" not in rendered
