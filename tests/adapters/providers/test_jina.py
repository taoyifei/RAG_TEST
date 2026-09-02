from __future__ import annotations

import json
import math
from collections.abc import Callable

import httpx
import pytest

from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.jina import (
    JinaEmbeddingConfig,
    JinaRerankerConfig,
    JinaRerankerV35Adapter,
    JinaV5TextEmbeddingAdapter,
)
from rag_app.core.errors import (
    PolicyDenied,
    ProviderAuthenticationError,
    ProviderInputTooLarge,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)
from rag_app.core.models import (
    EmbeddingRequest,
    EmbeddingRequestRole,
    RerankRequest,
)

_DIMENSION = 1024


def _unit(axis: int = 0) -> list[float]:
    vector = [0.0] * _DIMENSION
    vector[axis] = 2.0
    return vector


def _http(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_attempts: int = 3,
    sleeps: list[float] | None = None,
) -> ProviderHttpClient:
    observed_sleeps = sleeps if sleeps is not None else []
    return ProviderHttpClient(
        "https://api.jina.ai/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=max_attempts,
        sleeper=observed_sleeps.append,
        random_value=lambda: 0.0,
    )


def _json_response(payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(payload, allow_nan=True).encode(),
        headers={"Content-Type": "application/json"},
    )


def _embedding_config(**updates: object) -> JinaEmbeddingConfig:
    config = JinaEmbeddingConfig(
        slot_id="primary",
        request_policy_identity="policy-v1",
        document_egress_allowed=True,
        query_egress_allowed=True,
    )
    return config.model_copy(update=updates)


@pytest.mark.parametrize(
    ("role", "expected_task"),
    (
        (EmbeddingRequestRole.DOCUMENT, "retrieval.passage"),
        (EmbeddingRequestRole.QUERY, "retrieval.query"),
    ),
)
def test_embedding_maps_role_and_restores_shuffled_indices(
    monkeypatch: pytest.MonkeyPatch,
    role: EmbeddingRequestRole,
    expected_task: str,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "jina-embeddings-v5-text-small",
                "data": [
                    {"index": 1, "embedding": _unit(1)},
                    {"index": 0, "embedding": _unit(0)},
                ],
            },
        )

    adapter = JinaV5TextEmbeddingAdapter(
        _embedding_config(), http_client=_http(handler)
    )
    result = adapter.embed(
        EmbeddingRequest(
            slot_id="primary",
            role=role,
            texts=("first", "second"),
        )
    )
    adapter.close()

    assert observed == {
        "model": "jina-embeddings-v5-text-small",
        "task": expected_task,
        "dimensions": 1024,
        "normalized": True,
        "embedding_type": "float",
        "truncate": False,
        "input": ["first", "second"],
    }
    assert result.vectors[0][0] == 1.0
    assert result.vectors[1][1] == 1.0
    assert all(
        math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0)
        for vector in result.vectors
    )
    assert result.calls[0].endpoint == "api.jina.ai/v1/embeddings"


@pytest.mark.parametrize(
    "data",
    (
        [{"index": 0, "embedding": _unit()}],
        [
            {"index": 0, "embedding": _unit()},
            {"index": 0, "embedding": _unit(1)},
        ],
        [
            {"index": 0, "embedding": [float("nan")] + [0.0] * 1023},
            {"index": 1, "embedding": _unit(1)},
        ],
        [
            {"index": 0, "embedding": [float("inf")] + [0.0] * 1023},
            {"index": 1, "embedding": _unit(1)},
        ],
        [
            {"index": 0, "embedding": [0.0] * 1024},
            {"index": 1, "embedding": _unit(1)},
        ],
    ),
)
def test_embedding_rejects_incomplete_duplicate_or_bad_vectors(
    monkeypatch: pytest.MonkeyPatch,
    data: list[dict[str, object]],
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    adapter = JinaV5TextEmbeddingAdapter(
        _embedding_config(),
        http_client=_http(
            lambda _: _json_response(
                {
                    "model": "jina-embeddings-v5-text-small",
                    "data": data,
                }
            )
        ),
    )
    with pytest.raises(ProviderInvalidResponse):
        adapter.embed(
            EmbeddingRequest(
                slot_id="primary",
                role=EmbeddingRequestRole.QUERY,
                texts=("first", "second"),
            )
        )
    adapter.close()


def test_embedding_rejects_wrong_response_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    adapter = JinaV5TextEmbeddingAdapter(
        _embedding_config(),
        http_client=_http(
            lambda _: httpx.Response(
                200,
                json={"model": "wrong", "data": []},
            )
        ),
    )
    with pytest.raises(ProviderInvalidResponse):
        adapter.embed(
            EmbeddingRequest(
                slot_id="primary",
                role=EmbeddingRequestRole.QUERY,
                texts=("query",),
            )
        )
    adapter.close()


def test_embedding_rejects_oversize_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    adapter = JinaV5TextEmbeddingAdapter(
        _embedding_config(max_input_tokens=4),
        http_client=_http(handler),
    )
    with pytest.raises(ProviderInputTooLarge):
        adapter.embed(
            EmbeddingRequest(
                slot_id="primary",
                role=EmbeddingRequestRole.QUERY,
                texts=("too long",),
            )
        )
    adapter.close()
    assert calls == 0


def test_embedding_checks_egress_before_transport_or_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    adapter = JinaV5TextEmbeddingAdapter(
        _embedding_config(query_egress_allowed=False),
        http_client=_http(handler),
    )
    with pytest.raises(PolicyDenied):
        adapter.embed(
            EmbeddingRequest(
                slot_id="primary",
                role=EmbeddingRequestRole.QUERY,
                texts=("private text",),
            )
        )
    adapter.close()
    assert calls == 0


@pytest.mark.parametrize("status", (401, 403, 404))
def test_embedding_auth_or_model_status_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"detail": "hidden"})

    adapter = JinaV5TextEmbeddingAdapter(
        _embedding_config(), http_client=_http(handler)
    )
    with pytest.raises(ProviderAuthenticationError):
        adapter.embed(
            EmbeddingRequest(
                slot_id="primary",
                role=EmbeddingRequestRole.QUERY,
                texts=("private text",),
            )
        )
    adapter.close()
    assert calls == 1


def test_embedding_429_honors_retry_after_and_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={"private": "must not escape"},
        )

    adapter = JinaV5TextEmbeddingAdapter(
        _embedding_config(),
        http_client=_http(handler, sleeps=sleeps),
    )
    with pytest.raises(ProviderRateLimited) as captured:
        adapter.embed(
            EmbeddingRequest(
                slot_id="primary",
                role=EmbeddingRequestRole.QUERY,
                texts=("private text",),
            )
        )
    adapter.close()
    assert calls == 3
    assert sleeps == [2.0, 2.0]
    assert captured.value.provider_call is not None
    assert captured.value.provider_call.retry_after_ms == 2000
    assert "private text" not in str(captured.value)
    assert "test-jina-key" not in str(captured.value)


@pytest.mark.parametrize("failure", ("timeout", "503"))
def test_embedding_retries_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("injected", request=request)
            return httpx.Response(503, json={})
        return httpx.Response(
            200,
            json={
                "model": "jina-embeddings-v5-text-small",
                "data": [{"index": 0, "embedding": _unit()}],
            },
        )

    adapter = JinaV5TextEmbeddingAdapter(
        _embedding_config(), http_client=_http(handler)
    )
    result = adapter.embed(
        EmbeddingRequest(
            slot_id="primary",
            role=EmbeddingRequestRole.QUERY,
            texts=("query",),
        )
    )
    adapter.close()
    assert calls == 2
    assert result.calls[0].attempt_count == 2


def test_reranker_requests_complete_top_n_and_restores_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "score": 0.9, "document": "second"},
                    {
                        "index": 0,
                        "relevance_score": 0.2,
                        "document": "first",
                    },
                ]
            },
        )

    adapter = JinaRerankerV35Adapter(
        JinaRerankerConfig(egress_allowed=True),
        http_client=_http(handler),
    )
    result = adapter.rerank(
        RerankRequest(
            query="query",
            candidates=(("a", "first"), ("b", "second")),
            limit=1,
        )
    )
    adapter.close()
    assert observed["model"] == "jina-reranker-v3.5"
    assert observed["top_n"] == 2
    assert tuple(item.candidate_id for item in result.items) == ("b", "a")


@pytest.mark.parametrize(
    "results",
    (
        [{"index": 0, "relevance_score": 0.5}],
        [
            {"index": 0, "relevance_score": float("nan")},
            {"index": 1, "relevance_score": 0.2},
        ],
    ),
)
def test_reranker_rejects_missing_candidate_or_bad_score(
    monkeypatch: pytest.MonkeyPatch,
    results: list[dict[str, object]],
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    adapter = JinaRerankerV35Adapter(
        JinaRerankerConfig(egress_allowed=True),
        http_client=_http(
            lambda _: _json_response({"results": results})
        ),
    )
    with pytest.raises(ProviderInvalidResponse):
        adapter.rerank(
            RerankRequest(
                query="query",
                candidates=(("a", "first"), ("b", "second")),
                limit=2,
            )
        )
    adapter.close()


def test_reranker_rejects_local_total_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    adapter = JinaRerankerV35Adapter(
        JinaRerankerConfig(egress_allowed=True, max_total_tokens=4),
        http_client=_http(lambda _: httpx.Response(200, json={})),
    )
    with pytest.raises(ProviderInputTooLarge):
        adapter.rerank(
            RerankRequest(
                query="query",
                candidates=(("a", "document"),),
                limit=1,
            )
        )
    adapter.close()


def test_reranker_maps_unavailable_without_leaking_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "test-jina-key")
    adapter = JinaRerankerV35Adapter(
        JinaRerankerConfig(egress_allowed=True),
        http_client=_http(lambda _: httpx.Response(503, json={})),
    )
    with pytest.raises(ProviderUnavailable) as captured:
        adapter.rerank(
            RerankRequest(
                query="private query",
                candidates=(("a", "private candidate"),),
                limit=1,
            )
        )
    adapter.close()
    assert "private query" not in str(captured.value)
    assert "private candidate" not in str(captured.value)
