"""湾事通复用 Universal Provider 控制面的定向测试。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rag_app.product.models import ProviderConnection
from rag_app.wanshitong.errors import InternalModelConfigurationError
from rag_app.wanshitong.internal_model_settings import InternalModelSettings
from rag_app.wanshitong.internal_models import InternalModelConfigurator
from tests.product_support import build_product_harness


def _settings() -> InternalModelSettings:
    return InternalModelSettings(
        embedding_base_url="https://embedding.internal.example/v1",
        reranker_base_url="https://reranker.internal.example",
        llm_base_url="https://llm.internal.example/v1",
    )


def _openai_mock_transport(
    _connection: ProviderConnection,
) -> httpx.MockTransport:
    def _response(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        body = json.loads(payload)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "data": [{"index": 0, "embedding": [0.125] * 1024}],
                    "usage": {"total_tokens": 8},
                },
            )
        if request.url.path == "/rerank":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": index, "score": 1.0 - index / 10}
                        for index, _ in enumerate(body["texts"])
                    ]
                },
            )
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "连接正常"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(_response)


def test_internal_model_configurator_rejects_mock_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    harness = build_product_harness(
        tmp_path, transport_factory=_openai_mock_transport
    )
    try:
        with pytest.raises(InternalModelConfigurationError, match="测试专用"):
            InternalModelConfigurator(harness.runtime).configure(_settings())

        assert harness.runtime.control.list_connections() == ()
    finally:
        harness.close()


def test_internal_model_configurator_is_idempotent_and_sets_universal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    harness = build_product_harness(
        tmp_path, transport_factory=_openai_mock_transport
    )
    configurator = InternalModelConfigurator(
        harness.runtime, allow_mock_validation=True
    )
    try:
        first = configurator.configure(_settings())
        connections_before = harness.runtime.control.list_connections()
        validations_before = sum(
            len(harness.runtime.control.list_validations(item.connection_id))
            for item in connections_before
        )
        second = configurator.configure(_settings())
        connections_after = harness.runtime.control.list_connections()
        validations_after = sum(
            len(harness.runtime.control.list_validations(item.connection_id))
            for item in connections_after
        )

        assert first == second
        assert len(connections_after) == 3
        assert connections_after == connections_before
        assert validations_after == validations_before == 4
        assert first.profile_status == "active"
        assert {item.operation for item in first.validations} == {
            "embedding.document",
            "embedding.query",
            "reranking",
            "generation",
        }
        assert {item.validation_mode for item in first.validations} == {"mock"}
        profile = harness.runtime.control.active_profile(
            first.knowledge_base_id
        )
        assert profile is not None
        assert profile.profile_revision_id == (
            first.retrieval_profile_revision_id
        )
        model_settings = harness.runtime.models.get(first.knowledge_base_id)
        assert model_settings.generation_connection_id == (
            first.llm_connection_id
        )
        assert model_settings.generation_model == "Qwen/Qwen3-8B-AWQ"
        assert model_settings.rewrite_enabled is False
    finally:
        harness.close()


def test_internal_model_configurator_rejects_configuration_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    harness = build_product_harness(
        tmp_path, transport_factory=_openai_mock_transport
    )
    configurator = InternalModelConfigurator(
        harness.runtime, allow_mock_validation=True
    )
    try:
        configurator.configure(_settings())
        drifted = InternalModelSettings(
            embedding_base_url="https://other-embedding.internal.example/v1",
            reranker_base_url="https://reranker.internal.example",
            llm_base_url="https://llm.internal.example/v1",
        )

        with pytest.raises(
            InternalModelConfigurationError, match="配置已变化"
        ):
            configurator.configure(drifted)
    finally:
        harness.close()


def test_internal_model_configurator_fails_closed_on_corrupt_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    harness = build_product_harness(
        tmp_path, transport_factory=_openai_mock_transport
    )
    try:
        with harness.runtime.connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO metadata(namespace, key, value) VALUES (?, ?, ?)",
                ("wanshitong.internal-models", "embedding", "not-json"),
            )

        with pytest.raises(InternalModelConfigurationError, match="已损坏"):
            InternalModelConfigurator(
                harness.runtime, allow_mock_validation=True
            ).configure(_settings())
    finally:
        harness.close()
