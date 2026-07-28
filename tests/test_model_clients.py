import uuid

import httpx
import pytest

from rag_app.clients.model_services import (
    EmbeddingClientConfig,
    RerankerClient,
    TeiEmbeddingClient,
)
from rag_app.clients.resilience import (
    ExternalServiceUnavailableError,
    ResiliencePolicy,
    ResilientHttpPool,
)


def _pool(
    handler: object,
    endpoints: tuple[str, ...] = ("http://model",),
) -> ResilientHttpPool:
    return ResilientHttpPool(
        endpoints,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(
            max_attempts=max(2, len(endpoints)),
            failure_threshold=1,
            cooldown_seconds=30,
            max_concurrency=2,
        ),
    )


def test_embedding_client_batches_instructions_and_validates_dimension(
) -> None:
    requests: list[dict[str, object]] = []
    auth_value = uuid.uuid4().hex

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        decoded = httpx.Response(200, content=payload).json()
        requests.append(decoded)
        data = [
            {"index": index, "embedding": [float(index), 1.0, 2.0]}
            for index, _ in enumerate(decoded["input"])
        ]
        return httpx.Response(
            200,
            json={
                "model": "Qwen3-Embedding-0.6B",
                "data": list(reversed(data)),
            },
        )

    client = TeiEmbeddingClient(
        _pool(handler),
        config=EmbeddingClientConfig(
            model="Qwen3-Embedding-0.6B",
            dimension=3,
            max_batch_size=2,
            max_batch_chars=200,
        ),
        api_token=auth_value,
    )
    result = client.embed(
        ("甲", "乙", "丙"),
        instruction="检索文档",
    )

    assert result.vectors == (
        (0.0, 1.0, 2.0),
        (1.0, 1.0, 2.0),
        (0.0, 1.0, 2.0),
    )
    assert len(requests) == 2
    assert requests[0] == {
        "model": "Qwen3-Embedding-0.6B",
        "input": [
            "Instruct: 检索文档\nText: 甲",
            "Instruct: 检索文档\nText: 乙",
        ],
        "truncate": False,
        "encoding_format": "float",
    }


def test_embedding_client_rejects_wrong_dimension() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "Qwen3-Embedding-0.6B",
                "data": [{"index": 0, "embedding": [1.0]}],
            },
        )

    client = TeiEmbeddingClient(
        _pool(handler),
        config=EmbeddingClientConfig(
            model="Qwen3-Embedding-0.6B",
            dimension=3,
            max_batch_size=2,
            max_batch_chars=200,
        ),
        api_token=None,
    )
    with pytest.raises(
        ExternalServiceUnavailableError,
        match="INVALID_RESPONSE_SCHEMA",
    ):
        client.embed(("甲",), instruction="")


def test_embedding_schema_errors_fail_over_by_endpoint() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        if host == "wrong-model":
            return httpx.Response(
                200,
                json={
                    "model": "other",
                    "data": [{"index": 0, "embedding": [1.0, 2.0]}],
                },
            )
        if host == "wrong-dimension":
            return httpx.Response(
                200,
                json={
                    "model": "Qwen3-Embedding-0.6B",
                    "data": [{"index": 0, "embedding": [1.0]}],
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "Qwen3-Embedding-0.6B",
                "data": [{"index": 0, "embedding": [1.0, 2.0]}],
            },
        )

    client = TeiEmbeddingClient(
        _pool(
            handler,
            (
                "http://wrong-model",
                "http://wrong-dimension",
                "http://good",
            ),
        ),
        config=EmbeddingClientConfig(
            model="Qwen3-Embedding-0.6B",
            dimension=2,
            max_batch_size=2,
            max_batch_chars=200,
        ),
        api_token=None,
    )

    result = client.embed(("甲",), instruction="")

    assert result.vectors == ((1.0, 2.0),)
    assert result.calls[0].endpoint == "http://good"
    assert result.calls[0].retry_count == 2
    assert calls == ["wrong-model", "wrong-dimension", "good"]


def test_embedding_rejects_convertible_non_numeric_values() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "Qwen3-Embedding-0.6B",
                "data": [{"index": 0, "embedding": ["1.0", 2.0]}],
            },
        )

    client = TeiEmbeddingClient(
        _pool(handler),
        config=EmbeddingClientConfig(
            model="Qwen3-Embedding-0.6B",
            dimension=2,
            max_batch_size=2,
            max_batch_chars=200,
        ),
        api_token=None,
    )

    with pytest.raises(
        ExternalServiceUnavailableError,
        match="INVALID_RESPONSE_SCHEMA",
    ):
        client.embed(("甲",), instruction="")


def test_reranker_requires_complete_unique_indexed_scores() -> None:
    auth_value = uuid.uuid4().hex

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {auth_value}"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "score": 0.2},
                    {"index": 0, "score": 0.9},
                ]
            },
        )

    client = RerankerClient(_pool(handler), api_token=auth_value)
    result = client.rerank("问题", ("证据一", "证据二"))

    assert [(item.index, item.score) for item in result.items] == [
        (0, 0.9),
        (1, 0.2),
    ]


def test_reranker_schema_error_fails_over_by_endpoint() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        if host == "bad":
            return httpx.Response(
                200,
                json={"results": [{"index": 0, "score": 2.0}]},
            )
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "score": 0.8}]},
        )

    client = RerankerClient(
        _pool(handler, ("http://bad", "http://good")),
        api_token=None,
    )

    result = client.rerank("问题", ("文档",))

    assert result.call.endpoint == "http://good"
    assert result.call.retry_count == 1
    assert calls == ["bad", "good"]
