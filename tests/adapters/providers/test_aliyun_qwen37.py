from __future__ import annotations

import json
import math
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError

from rag_app.adapters.providers.aliyun_qwen37 import (
    AliyunQwen37EmbeddingAdapter,
    AliyunQwen37EmbeddingConfig,
)
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.core.errors import (
    ConfigurationError,
    ProviderAuthenticationError,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)
from rag_app.core.models import EmbeddingRequest, EmbeddingRequestRole

_DIMENSION = 1024
_INSTRUCTION = (
    "Given a user query, retrieve the most relevant passages from enterprise "
    "DOCX knowledge bases."
)


def _unit(axis: int = 0) -> list[float]:
    vector = [0.0] * _DIMENSION
    vector[axis] = 4.0
    return vector


def _http(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_attempts: int = 3,
) -> ProviderHttpClient:
    return ProviderHttpClient(
        "https://workspace-1.cn-beijing.maas.aliyuncs.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=max_attempts,
        sleeper=lambda _: None,
        random_value=lambda: 0.0,
    )


def _config(**updates: object) -> AliyunQwen37EmbeddingConfig:
    config = AliyunQwen37EmbeddingConfig(
        slot_id="standby",
        request_policy_identity="qwen-policy-v1",
        document_egress_allowed=True,
        query_egress_allowed=True,
    )
    return config.model_copy(update=updates)


@pytest.mark.parametrize(
    ("role", "text_type", "has_instruct"),
    (
        (EmbeddingRequestRole.DOCUMENT, "document", False),
        (EmbeddingRequestRole.QUERY, "query", True),
    ),
)
def test_native_request_role_endpoint_and_reordering(
    monkeypatch: pytest.MonkeyPatch,
    role: EmbeddingRequestRole,
    text_type: str,
    has_instruct: bool,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    observed: dict[str, object] = {}
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "status_code": 200,
                "code": "",
                "message": "",
                "output": {
                    "embeddings": [
                        {"text_index": 1, "embedding": _unit(1)},
                        {"text_index": 0, "embedding": _unit(0)},
                    ]
                },
                "usage": {"total_tokens": 2},
            },
        )

    adapter = AliyunQwen37EmbeddingAdapter(
        _config(), http_client=_http(handler)
    )
    result = adapter.embed(
        EmbeddingRequest(
            slot_id="standby",
            role=role,
            texts=("first", "second"),
        )
    )
    adapter.close()

    assert urls == [
        "https://workspace-1.cn-beijing.maas.aliyuncs.com/"
        "api/v1/services/embeddings/text-embedding/text-embedding"
    ]
    assert observed["model"] == "qwen3.7-text-embedding"
    parameters = observed["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["text_type"] == text_type
    assert parameters["dimension"] == 1024
    assert parameters["output_type"] == "dense"
    assert ("instruct" in parameters) is has_instruct
    if has_instruct:
        assert parameters["instruct"] == _INSTRUCTION
    assert result.vectors[0][0] == 1.0
    assert result.vectors[1][1] == 1.0
    assert all(
        math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0)
        for vector in result.vectors
    )
    assert "dashscope-native" in adapter.descriptor.version


@pytest.mark.parametrize(
    "payload",
    (
        {"status_code": 500, "code": "InternalError", "output": {}},
        {"status_code": 200, "code": "BadCode", "output": {}},
        {"status_code": 200, "code": "", "output": None},
        {
            "status_code": 200,
            "code": "",
            "output": {
                "embeddings": [
                    {"text_index": 0, "embedding": [0.0] * 1024}
                ]
            },
        },
    ),
)
def test_native_response_rejects_bad_status_code_output_or_vector(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    adapter = AliyunQwen37EmbeddingAdapter(
        _config(),
        http_client=_http(lambda _: httpx.Response(200, json=payload)),
    )
    with pytest.raises(ProviderInvalidResponse):
        adapter.embed(
            EmbeddingRequest(
                slot_id="standby",
                role=EmbeddingRequestRole.QUERY,
                texts=("query",),
            )
        )
    adapter.close()


def test_native_response_restores_text_index_and_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    adapter = AliyunQwen37EmbeddingAdapter(
        _config(),
        http_client=_http(
            lambda _: httpx.Response(
                200,
                json={
                    "status_code": 200,
                    "code": "",
                    "output": {
                        "embeddings": [
                            {"text_index": 0, "embedding": _unit()},
                            {"text_index": 0, "embedding": _unit(1)},
                        ]
                    },
                },
            )
        ),
    )
    with pytest.raises(ProviderInvalidResponse):
        adapter.embed(
            EmbeddingRequest(
                slot_id="standby",
                role=EmbeddingRequestRole.QUERY,
                texts=("first", "second"),
            )
        )
    adapter.close()


@pytest.mark.parametrize("status", (401, 429, 503))
def test_native_http_error_classification_and_retry(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={})

    adapter = AliyunQwen37EmbeddingAdapter(
        _config(), http_client=_http(handler)
    )
    error_type = (
        ProviderAuthenticationError if status == 401 else ProviderRateLimited
    )
    if status == 503:
        error_type = ProviderUnavailable
    with pytest.raises(error_type):
        adapter.embed(
            EmbeddingRequest(
                slot_id="standby",
                role=EmbeddingRequestRole.QUERY,
                texts=("query",),
            )
        )
    adapter.close()
    assert calls == (1 if status == 401 else 3)


def test_workspace_is_required_and_region_is_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.delenv("ALIYUN_MODEL_STUDIO_WORKSPACE_ID", raising=False)
    adapter = AliyunQwen37EmbeddingAdapter(_config())
    with pytest.raises(ConfigurationError):
        adapter.embed(
            EmbeddingRequest(
                slot_id="standby",
                role=EmbeddingRequestRole.QUERY,
                texts=("query",),
            )
        )
    adapter.close()

    monkeypatch.setenv("ALIYUN_MODEL_STUDIO_WORKSPACE_ID", "workspace-1")
    monkeypatch.setenv("ALIYUN_MODEL_STUDIO_REGION", "ap-southeast-1")
    adapter = AliyunQwen37EmbeddingAdapter(_config())
    with pytest.raises(ConfigurationError):
        adapter.embed(
            EmbeddingRequest(
                slot_id="standby",
                role=EmbeddingRequestRole.QUERY,
                texts=("query",),
            )
        )
    adapter.close()


def test_openai_base_url_cannot_masquerade_as_native_config() -> None:
    with pytest.raises(ValidationError):
        AliyunQwen37EmbeddingConfig.model_validate(
            {
                "slot_id": "standby",
                "request_policy_identity": "policy-v1",
                "base_url": "https://example.com/compatible-mode/v1",
            }
        )
