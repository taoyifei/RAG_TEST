import json

import pytest

from rag_app.composition.profiles import (
    default_hot_standby_profile,
    default_offline_profile,
    profile_from_mapping,
)
from rag_app.core.errors import ConfigurationError


def test_offline_profile_matches_required_development_stack() -> None:
    profile = default_offline_profile()
    components = profile.components
    assert components.parser == "docx-ooxml-v4"
    assert components.chunker == "docx-structural-v3"
    assert components.embedding_topology == "deterministic-single"
    assert components.embedding_primary == "deterministic"
    assert components.reranker == "lexical-overlap"
    assert components.vector_store == "memory"
    assert components.metadata_store == "sqlite"
    assert profile.security.remote_document_embedding is False


def test_hot_standby_profile_uses_fixed_provider_decision() -> None:
    profile = default_hot_standby_profile()
    topology = profile.components.embedding_topology
    assert not isinstance(topology, str)
    assert topology.primary.model == "jina-embeddings-v5-text-small"
    assert topology.primary.dimension == 1024
    assert topology.primary.vector_name == "dense_primary"
    assert topology.standby is not None
    assert topology.standby.model == "qwen3.7-text-embedding"
    assert topology.standby.vector_name == "dense_standby"
    reranker = profile.components.reranker
    assert not isinstance(reranker, str)
    assert reranker.model == "jina-reranker-v3.5"


def test_profile_rejects_unknown_fields_and_reports_paths() -> None:
    payload = default_offline_profile().model_dump(mode="json")
    payload["components"]["unknown_component"] = "unsafe"
    payload["security"]["unknown_permission"] = True
    with pytest.raises(ConfigurationError) as captured:
        profile_from_mapping(payload)
    paths = dict(captured.value.details)["paths"]
    assert "components.unknown_component" in paths
    assert "security.unknown_permission" in paths


def test_redacted_profile_contains_only_secret_environment_names() -> None:
    profile = default_hot_standby_profile()
    rendered = json.dumps(profile.redacted_dict(), ensure_ascii=False)
    assert "JINA_API_KEY" in rendered
    assert "DASHSCOPE_API_KEY" in rendered
    assert "sk-" not in rendered
    assert "Bearer " not in rendered


def test_required_hot_standby_json_fragment_passes_schema() -> None:
    profile = profile_from_mapping(
        {
            "schema_version": "1",
            "profile_id": "production-candidate",
            "components": {
                "embedding_topology": {
                    "mode": "hot_standby",
                    "activation_policy": "all_required_slots_complete",
                    "primary": {
                        "slot_id": "primary",
                        "provider": "jina-embedding",
                        "model": "jina-embeddings-v5-text-small",
                        "dimension": 1024,
                        "vector_name": "dense_primary",
                    },
                    "standby": {
                        "slot_id": "standby",
                        "provider": "aliyun-qwen37-embedding",
                        "model": "qwen3.7-text-embedding",
                        "dimension": 1024,
                        "vector_name": "dense_standby",
                    },
                },
                "reranker": {
                    "provider": "jina-reranker",
                    "model": "jina-reranker-v3.5",
                    "on_unavailable": "bypass_keep_rrf",
                },
            },
        }
    )
    assert profile.components.embedding_router == "embedding-router-hot-standby"
