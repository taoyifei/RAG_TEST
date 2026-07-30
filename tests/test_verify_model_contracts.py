import json
import math

import httpx
import pytest

from scripts.verify_model_contracts import (
    ContractError,
    ModelContractOptions,
    verify_model_contract,
)

_TOKEN = "DUMMY_TEST_TOKEN_REPLACE_ME"  # noqa: S105
_REVISION = "model-revision-test"
_MODELS = {
    "embedding": "Qwen3-Embedding-0.6B",
    "reranker": "Qwen3-Reranker-0.6B",
    "llm": "Qwen/Qwen3-8B-AWQ",
}


def _options(service: str) -> ModelContractOptions:
    return ModelContractOptions(
        service=service,
        endpoint="http://model.internal:8000",
        model=_MODELS[service],
        token=_TOKEN,
        dimension=3 if service == "embedding" else None,
        timeout_seconds=5.0,
    )


def _common_response(
    request: httpx.Request,
    *,
    model: str,
) -> httpx.Response | None:
    assert request.headers["authorization"] == f"Bearer {_TOKEN}"
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/v1/models":
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": model, "revision": _REVISION}],
            },
        )
    return None


def _llm_response(
    request: httpx.Request,
    *,
    content: object | None = None,
    finish_reason: str = "stop",
) -> httpx.Response:
    payload = json.loads(request.content)
    schema_name = payload["response_format"]["json_schema"]["name"]
    assert payload["temperature"] == 0
    assert payload["stream"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["response_format"]["json_schema"]["strict"] is True
    if content is None:
        content = (
            {"standalone_query": "standalone synthetic query"}
            if schema_name == "query_rewrite"
            else {
                "status": "refused",
                "claims": [],
                "refusal_reason": "synthetic evidence is insufficient",
            }
        )
    return httpx.Response(
        200,
        json={
            "model": _MODELS["llm"],
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"content": json.dumps(content)},
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        },
    )


@pytest.mark.parametrize("service", ["embedding", "reranker", "llm"])
def test_model_contract_success_is_sanitized(service: str) -> None:
    request_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model=_MODELS[service])
        if common is not None:
            return common
        request_bodies.append(request.content.decode())
        if request.url.path == "/v1/embeddings":
            payload = json.loads(request.content)
            assert payload["model"] == _MODELS["embedding"]
            assert payload["truncate"] is False
            return httpx.Response(
                200,
                json={
                    "model": _MODELS["embedding"],
                    "data": [
                        {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                        {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                    ],
                },
            )
        if request.url.path == "/rerank":
            payload = json.loads(request.content)
            assert payload["truncate"] is False
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 1, "score": 0.2},
                        {"index": 0, "score": 0.8},
                    ]
                },
            )
        return _llm_response(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_model_contract(_options(service), client=client)

    assert report["status"] == "passed"
    assert report["service"] == service
    assert report["model"] == _MODELS[service]
    assert report["endpoint_revision"] == _REVISION
    assert report["health"] == "passed"
    assert report["model_id"] == "passed"
    probe = report["probe"]
    assert isinstance(probe, dict)
    if service == "embedding":
        assert probe["count"] == 2
        assert probe["dimension"] == 3
        assert probe["indexes"] == [0, 1]
        assert probe["finite"] is True
    elif service == "reranker":
        assert probe["count"] == 2
        assert probe["indexes"] == [0, 1]
        assert probe["score_range"] == [0.0, 1.0]
    else:
        for contract in ("rewrite", "answer"):
            assert probe[contract]["finish_reason"] == "stop"
            assert probe[contract]["usage"] == {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            }
        assert probe["temperature"] == 0
        assert probe["thinking_enabled"] is False
    serialized = json.dumps(report, ensure_ascii=False)
    assert _TOKEN not in serialized
    assert all(body not in serialized for body in request_bodies)
    assert "standalone synthetic query" not in serialized
    assert "synthetic evidence is insufficient" not in serialized


def test_model_contract_rejects_wrong_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model="wrong-model")
        assert common is not None
        return common

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match="MODEL_MISMATCH") as raised,
    ):
        verify_model_contract(_options("embedding"), client=client)

    assert raised.value.code == "MODEL_MISMATCH"


def test_model_contract_rejects_wrong_llm_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model=_MODELS["llm"])
        if common is not None:
            return common
        return _llm_response(request, content={"unexpected": "value"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="RESPONSE_SCHEMA_INVALID",
        ) as raised,
    ):
        verify_model_contract(_options("llm"), client=client)

    assert raised.value.code == "RESPONSE_SCHEMA_INVALID"


@pytest.mark.parametrize(
    ("vector", "expected_code"),
    [
        ([0.1, 0.2], "EMBEDDING_DIMENSION_MISMATCH"),
        ([0.1, math.inf, 0.3], "EMBEDDING_NONFINITE"),
    ],
)
def test_model_contract_rejects_bad_embedding(
    vector: list[float],
    expected_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model=_MODELS["embedding"])
        if common is not None:
            return common
        response_payload = {
            "model": _MODELS["embedding"],
            "data": [
                {"index": 0, "embedding": vector},
                {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            ],
        }
        return httpx.Response(
            200,
            content=json.dumps(response_payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match=expected_code) as raised,
    ):
        verify_model_contract(_options("embedding"), client=client)

    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "score": 0.8}],
        [
            {"index": 0, "score": 1.1},
            {"index": 1, "score": 0.2},
        ],
    ],
)
def test_model_contract_rejects_bad_reranker_results(
    results: list[dict[str, object]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model=_MODELS["reranker"])
        if common is not None:
            return common
        return httpx.Response(200, json={"results": results})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError) as raised,
    ):
        verify_model_contract(_options("reranker"), client=client)

    assert raised.value.code in {
        "RERANK_INDEX_MISMATCH",
        "RERANK_SCORE_INVALID",
    }


def test_model_contract_rejects_truncated_llm() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model=_MODELS["llm"])
        if common is not None:
            return common
        return _llm_response(request, finish_reason="length")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match="LLM_TRUNCATED") as raised,
    ):
        verify_model_contract(_options("llm"), client=client)

    assert raised.value.code == "LLM_TRUNCATED"


def test_model_contract_reports_endpoint_failure_without_response() -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(503))
        ) as client,
        pytest.raises(
            ContractError,
            match="ENDPOINT_FAILURE",
        ) as raised,
    ):
        verify_model_contract(_options("reranker"), client=client)

    assert raised.value.code == "ENDPOINT_FAILURE"
    assert _TOKEN not in str(raised.value)
