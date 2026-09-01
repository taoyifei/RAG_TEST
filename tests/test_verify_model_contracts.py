import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from rag_app.chunking import Utf8TokenCounter
from scripts.verify_model_contracts import (
    ContractError,
    LlmBudgetOptions,
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
    llm_budget = (
        LlmBudgetOptions(
            context_limit=8192,
            max_question_tokens=32,
            max_evidence_tokens=128,
            rewrite_output_tokens=128,
            answer_output_tokens=1024,
            repair_output_tokens=1024,
            token_counter=Utf8TokenCounter(),
        )
        if service == "llm"
        else None
    )
    return ModelContractOptions(
        service=service,
        endpoint="http://model.internal:8000",
        model=_MODELS[service],
        expected_revision=_REVISION,
        token=_TOKEN,
        dimension=3 if service == "embedding" else None,
        timeout_seconds=5.0,
        deployment_manifest=None,
        llm_budget=llm_budget,
    )


def _common_response(
    request: httpx.Request,
    *,
    model: str,
) -> httpx.Response | None:
    assert request.headers["authorization"] == f"Bearer {_TOKEN}"
    if request.url.path == "/health":
        if model == _MODELS["reranker"]:
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "model_path": f"/models/{model}",
                    "device": "cuda:0",
                },
            )
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/info":
        return httpx.Response(
            200,
            json=_tei_info_payload(
                model_id=f"/models/{model}",
                served_model_name=model,
            ),
        )
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
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
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
            else {"claims": []}
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
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


@pytest.mark.parametrize("service", ["embedding", "reranker", "llm"])
def test_model_contract_success_is_sanitized(
    tmp_path: Path,
    service: str,
) -> None:
    request_bodies: list[str] = []
    if service == "embedding":
        options = _embedding_options_with_v2_manifest(tmp_path)
    elif service == "reranker":
        options = _reranker_options_with_v2_manifest(tmp_path)
    else:
        options = _options(service)

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
        report = verify_model_contract(options, client=client)

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
        for contract in (
            "rewrite",
            "answer_initial_max",
            "answer_repair_max",
        ):
            assert probe[contract]["finish_reason"] == "stop"
            assert probe[contract]["usage"] == {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            }
            assert probe[contract]["budget"]["within_context"] is True
        assert probe["temperature"] == 0
        assert probe["thinking_enabled"] is False
    serialized = json.dumps(report, ensure_ascii=False)
    assert _TOKEN not in serialized
    assert all(body not in serialized for body in request_bodies)
    assert "standalone synthetic query" not in serialized


def test_model_contract_rejects_wrong_model(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model="wrong-model")
        assert common is not None
        return common

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match="MODEL_MISMATCH") as raised,
    ):
        verify_model_contract(
            _embedding_options_with_v2_manifest(tmp_path),
            client=client,
        )

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
    tmp_path: Path,
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
        verify_model_contract(
            _embedding_options_with_v2_manifest(tmp_path),
            client=client,
        )

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
    tmp_path: Path,
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
        verify_model_contract(
            _reranker_options_with_v2_manifest(tmp_path),
            client=client,
        )

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


def test_model_contract_rejects_non_origin_endpoint_before_request() -> None:
    with pytest.raises(ValueError, match="origin 根 URL"):
        replace(
            _options("embedding"),
            endpoint="http://model.internal:8000/v1",
        )


def test_model_contract_allows_endpoint_without_token(
    tmp_path: Path,
) -> None:
    options = replace(
        _embedding_options_with_v2_manifest(tmp_path),
        token=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/info":
            return httpx.Response(200, json=_tei_info_payload())
        return httpx.Response(
            200,
            json={
                "model": _MODELS["embedding"],
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_model_contract(options, client=client)

    assert report["status"] == "passed"


def test_model_contract_rejects_endpoint_revision_drift() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": _MODELS["llm"],
                            "revision": "unexpected-revision",
                        }
                    ]
                },
            )
        return _llm_response(request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match="REVISION_MISMATCH"),
    ):
        verify_model_contract(_options("llm"), client=client)


def test_llm_contract_uses_maximum_initial_and_repair_requests() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model=_MODELS["llm"])
        if common is not None:
            return common
        requests.append(json.loads(request.content))
        return _llm_response(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        verify_model_contract(_options("llm"), client=client)

    answer_requests = [
        request
        for request in requests
        if request["response_format"]["json_schema"]["name"]
        == "strict_evidence_answer"
    ]
    assert len(requests) == 3
    assert [request["max_tokens"] for request in answer_requests] == [
        1024,
        1024,
    ]
    initial_payload = json.loads(answer_requests[0]["messages"][1]["content"])
    repair_payload = json.loads(answer_requests[1]["messages"][1]["content"])
    expected_profile = {
        "primary_operation": "LIST",
        "secondary_operations": [],
        "requested_slots": [],
    }
    assert initial_payload["question_profile"] == expected_profile
    assert repair_payload["original_request"]["question_profile"] == (
        expected_profile
    )


def _write_manifest(
    path: Path,
    *,
    service: str,
    digest_matches: bool = True,
) -> None:
    payload: dict[str, object] = {
        "schema_version": "1",
        "service": service,
        "endpoint": "http://model.internal:8000",
        "model": _MODELS[service],
        "model_revision": _REVISION,
        "tokenizer_revision": "sha256:" + "1" * 64,
        "code_revision": "2" * 40,
        "vllm_version": "0.10.2",
        "quantization": "awq",
        "max_context_tokens": 8192,
        "chat_template_sha256": "sha256:" + "3" * 64,
    }
    _write_sealed_manifest(
        path,
        payload,
        digest_matches=digest_matches,
    )


def _write_sealed_manifest(
    path: Path,
    payload: dict[str, object],
    *,
    digest_matches: bool = True,
) -> None:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    payload["manifest_sha256"] = (
        f"sha256:{digest}" if digest_matches else "sha256:" + "0" * 64
    )
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    path.chmod(0o444)


def _manifest_v2_payload(
    service: str,
    *,
    runtime_revision: str = "4" * 40,
) -> dict[str, object]:
    service_contracts: dict[str, dict[str, object]] = {
        "embedding": {"dimension": 3},
        "reranker": {"score_max": 1.0, "score_min": 0.0},
        "llm": {
            "chat_template_sha256": "sha256:" + "3" * 64,
            "max_context_tokens": 8192,
            "quantization": "awq",
        },
    }
    runtime_names = {
        "embedding": "text-embeddings-inference",
        "reranker": "covlink-rerank-api",
        "llm": "vllm",
    }
    return {
        "schema_version": "2",
        "service": service,
        "endpoint": "http://model.internal:8000",
        "model": _MODELS[service],
        "model_revision": _REVISION,
        "tokenizer_revision": "sha256:" + "1" * 64,
        "runtime": {
            "name": runtime_names[service],
            "revision": runtime_revision,
            "version": "1.9.1",
        },
        "service_contract": service_contracts[service],
    }


def _manifest_endpoint_response(
    request: httpx.Request,
    *,
    service: str,
) -> httpx.Response:
    assert request.headers["authorization"] == f"Bearer {_TOKEN}"
    if request.url.path in {"/health", "/info"}:
        if request.url.path == "/info":
            payload: object = _tei_info_payload()
        elif service == "reranker":
            payload = {
                "status": "ok",
                "model_path": f"/models/{_MODELS[service]}",
                "device": "cuda:0",
            }
        else:
            payload = {"status": "ok"}
        return httpx.Response(200, json=payload)
    if request.url.path == "/v1/models":
        return httpx.Response(
            200,
            json={"data": [{"id": _MODELS[service]}]},
        )
    if request.url.path == "/v1/embeddings":
        return httpx.Response(
            200,
            json={
                "model": _MODELS["embedding"],
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                ],
            },
        )
    if request.url.path == "/rerank":
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "score": 0.8},
                    {"index": 1, "score": 0.2},
                ]
            },
        )
    return _llm_response(request)


@pytest.mark.parametrize("service", ["embedding", "reranker", "llm"])
def test_service_specific_v2_manifest_supplies_missing_revision(
    tmp_path: Path,
    service: str,
) -> None:
    manifest_path = tmp_path / f"{service}-deployment-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload(service),
    )
    options = replace(
        _options(service),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _manifest_endpoint_response(request, service=service)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_model_contract(options, client=client)

    assert report["status"] == "passed"
    assert report["revision_source"] == "deployment_manifest"
    assert report["deployment_manifest_sha256"].startswith("sha256:")


@pytest.mark.parametrize(
    "runtime_revision",
    ["5" * 40, "sha256:" + "6" * 64, "1.9.1"],
)
def test_v2_manifest_accepts_supported_runtime_revisions(
    tmp_path: Path,
    runtime_revision: str,
) -> None:
    manifest_path = tmp_path / "embedding-deployment-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload(
            "embedding",
            runtime_revision=runtime_revision,
        ),
    )
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _manifest_endpoint_response(request, service="embedding")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_model_contract(options, client=client)

    assert report["status"] == "passed"


@pytest.mark.parametrize("service", ["embedding", "reranker", "llm"])
@pytest.mark.parametrize("location", ["top", "runtime", "contract"])
def test_v2_manifest_rejects_extra_fields(
    tmp_path: Path,
    service: str,
    location: str,
) -> None:
    payload = _manifest_v2_payload(service)
    if location == "top":
        payload["unexpected"] = "forbidden"
    else:
        field = "runtime" if location == "runtime" else "service_contract"
        nested = payload[field]
        assert isinstance(nested, dict)
        nested["unexpected"] = "forbidden"
    manifest_path = tmp_path / f"{service}-deployment-manifest.json"
    _write_sealed_manifest(manifest_path, payload)
    options = replace(
        _options(service),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _manifest_endpoint_response(request, service=service)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ),
    ):
        verify_model_contract(options, client=client)


def test_v2_manifest_rejects_bad_canonical_digest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "embedding-deployment-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload("embedding"),
        digest_matches=False,
    )
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _manifest_endpoint_response(request, service="embedding")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ),
    ):
        verify_model_contract(options, client=client)


def test_v2_manifest_rejects_symlink(tmp_path: Path) -> None:
    real_manifest = tmp_path / "real-manifest.json"
    _write_sealed_manifest(
        real_manifest,
        _manifest_v2_payload("embedding"),
    )
    manifest_path = tmp_path / "embedding-deployment-manifest.json"
    manifest_path.symlink_to(real_manifest)
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _manifest_endpoint_response(request, service="embedding")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ),
    ):
        verify_model_contract(options, client=client)


@pytest.mark.parametrize("runtime_revision", ["main", "latest", "branch"])
def test_v2_manifest_rejects_unpinned_runtime_revision(
    tmp_path: Path,
    runtime_revision: str,
) -> None:
    manifest_path = tmp_path / "embedding-deployment-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload(
            "embedding",
            runtime_revision=runtime_revision,
        ),
    )
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _manifest_endpoint_response(request, service="embedding")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ),
    ):
        verify_model_contract(options, client=client)


@pytest.mark.parametrize(
    "revision_field",
    ["model_revision", "tokenizer_revision"],
)
def test_v2_manifest_rejects_unpinned_common_revision(
    tmp_path: Path,
    revision_field: str,
) -> None:
    payload = _manifest_v2_payload("embedding")
    payload[revision_field] = "main"
    manifest_path = tmp_path / "embedding-deployment-manifest.json"
    _write_sealed_manifest(manifest_path, payload)
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _manifest_endpoint_response(request, service="embedding")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ),
    ):
        verify_model_contract(options, client=client)


def test_model_contract_uses_verified_manifest_when_revision_missing(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "deployment-model-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload("embedding"),
    )
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/info":
            return httpx.Response(200, json=_tei_info_payload())
        return httpx.Response(
            200,
            json={
                "model": _MODELS["embedding"],
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_model_contract(options, client=client)

    assert report["endpoint_revision"] == _REVISION
    assert report["revision_source"] == "deployment_manifest"
    assert report["deployment_manifest_sha256"].startswith("sha256:")


def test_model_contract_rejects_bad_manifest_digest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "deployment-model-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload("embedding"),
        digest_matches=False,
    )
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, json=_tei_info_payload())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ),
    ):
        verify_model_contract(options, client=client)


def test_model_contract_rejects_writable_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "deployment-model-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload("embedding"),
    )
    manifest_path.chmod(0o644)
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, json=_tei_info_payload())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ),
    ):
        verify_model_contract(options, client=client)


def test_model_contract_rejects_conflicting_endpoint_revisions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ok"},
                headers={"x-model-revision": "other-revision"},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": _MODELS["llm"],
                        "revision": _REVISION,
                    }
                ]
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match="REVISION_MISMATCH"),
    ):
        verify_model_contract(_options("llm"), client=client)


def test_model_contract_rejects_llm_context_overflow() -> None:
    options = _options("llm")
    assert options.llm_budget is not None

    def handler(request: httpx.Request) -> httpx.Response:
        common = _common_response(request, model=_MODELS["llm"])
        if common is not None:
            return common
        request_payload = json.loads(request.content)
        max_tokens = request_payload["max_tokens"]
        prompt_tokens = (
            options.llm_budget.context_limit - max_tokens + 1
            if max_tokens == options.llm_budget.answer_output_tokens
            else 11
        )
        return _llm_response(
            request,
            prompt_tokens=prompt_tokens,
            completion_tokens=1,
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="LLM_CONTEXT_BUDGET_EXCEEDED",
        ),
    ):
        verify_model_contract(options, client=client)


@pytest.mark.parametrize("revision", ["unknown", "main", "latest"])
def test_model_contract_rejects_unpinned_expected_revision(
    revision: str,
) -> None:
    with pytest.raises(ValueError, match="expected_revision"):
        replace(_options("embedding"), expected_revision=revision)


def _reranker_options_with_v2_manifest(
    tmp_path: Path,
) -> ModelContractOptions:
    manifest_path = tmp_path / "reranker-deployment-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload("reranker"),
    )
    return replace(
        _options("reranker"),
        deployment_manifest=manifest_path,
    )


def _reranker_health_payload(
    *,
    model_path: str = "/models/Qwen3-Reranker-0.6B",
    device: str = "cuda:0",
    status: str = "ok",
) -> dict[str, str]:
    return {
        "status": status,
        "model_path": model_path,
        "device": device,
    }


def test_reranker_contract_skips_v1_models_and_uses_v2_manifest(
    tmp_path: Path,
) -> None:
    options = _reranker_options_with_v2_manifest(tmp_path)
    request_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json=_reranker_health_payload())
        if request.url.path == "/rerank":
            assert json.loads(request.content) == {
                "query": "contract probe",
                "texts": ["candidate alpha", "candidate beta"],
                "truncate": False,
            }
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 0, "score": 0.8},
                        {"index": 1, "score": 0.2},
                    ]
                },
            )
        pytest.fail(f"reranker 不应请求 {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_model_contract(options, client=client)

    assert request_paths == ["/health", "/rerank"]
    assert report["revision_source"] == "deployment_manifest"
    assert report["deployment_manifest_sha256"].startswith("sha256:")
    serialized = json.dumps(report, ensure_ascii=False)
    assert "/models/Qwen3-Reranker-0.6B" not in serialized
    assert "cuda:0" not in serialized


@pytest.mark.parametrize(
    ("health_payload", "expected_code"),
    [
        (
            _reranker_health_payload(
                model_path="/models/wrong-reranker",
            ),
            "MODEL_MISMATCH",
        ),
        (
            _reranker_health_payload(device="cpu"),
            "RERANK_DEVICE_INVALID",
        ),
        (
            _reranker_health_payload(status="unavailable"),
            "HEALTH_INVALID",
        ),
    ],
)
def test_reranker_contract_rejects_invalid_health_identity(
    tmp_path: Path,
    health_payload: dict[str, str],
    expected_code: str,
) -> None:
    options = _reranker_options_with_v2_manifest(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json=health_payload)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match=expected_code) as raised,
    ):
        verify_model_contract(options, client=client)

    assert raised.value.code == expected_code
    assert health_payload["model_path"] not in str(raised.value)
    assert health_payload["device"] not in str(raised.value)


def test_reranker_contract_rejects_missing_deployment_manifest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json=_reranker_health_payload())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match="REVISION_MISSING") as raised,
    ):
        verify_model_contract(_options("reranker"), client=client)

    assert raised.value.code == "REVISION_MISSING"


def test_reranker_contract_rejects_schema_v1_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "reranker-deployment-manifest.json"
    _write_manifest(manifest_path, service="reranker")
    options = replace(
        _options("reranker"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json=_reranker_health_payload())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ) as raised,
    ):
        verify_model_contract(options, client=client)

    assert raised.value.code == "DEPLOYMENT_MANIFEST_INVALID"


def _embedding_options_with_v2_manifest(
    tmp_path: Path,
) -> ModelContractOptions:
    manifest_path = tmp_path / "embedding-deployment-manifest.json"
    _write_sealed_manifest(
        manifest_path,
        _manifest_v2_payload("embedding"),
    )
    return replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )


def _tei_info_payload(
    *,
    model_id: str = "/models/Qwen3-Embedding-0.6B",
    served_model_name: str = "Qwen3-Embedding-0.6B",
) -> dict[str, object]:
    return {
        "model_id": model_id,
        "model_sha": None,
        "model_dtype": "float16",
        "served_model_name": served_model_name,
        "model_type": {"embedding": {"pooling": "lasttoken"}},
        "max_concurrent_requests": 512,
        "max_input_length": 32768,
        "max_batch_tokens": 16384,
        "max_batch_requests": None,
        "max_client_batch_size": 32,
        "auto_truncate": False,
        "tokenization_workers": 8,
        "version": "1.9.3",
        "sha": "06670157fb6c1523482219bdb2d1660277d38088",
        "docker_label": None,
    }


def test_embedding_contract_skips_v1_models_and_uses_tei_info(
    tmp_path: Path,
) -> None:
    options = _embedding_options_with_v2_manifest(tmp_path)
    request_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/info":
            return httpx.Response(200, json=_tei_info_payload())
        if request.url.path == "/v1/embeddings":
            return httpx.Response(
                200,
                json={
                    "model": _MODELS["embedding"],
                    "data": [
                        {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                        {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                    ],
                },
            )
        pytest.fail(f"embedding 不应请求 {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify_model_contract(options, client=client)

    assert request_paths == ["/health", "/info", "/v1/embeddings"]
    assert report["revision_source"] == "deployment_manifest"
    assert report["deployment_manifest_sha256"].startswith("sha256:")
    serialized = json.dumps(report, ensure_ascii=False)
    assert "/models/Qwen3-Embedding-0.6B" not in serialized
    assert "06670157fb6c1523482219bdb2d1660277d38088" not in serialized


@pytest.mark.parametrize(
    "info_payload",
    [
        _tei_info_payload(served_model_name="wrong-embedding"),
        _tei_info_payload(model_id="/models/wrong-embedding"),
    ],
)
def test_embedding_contract_rejects_wrong_tei_model_identity(
    tmp_path: Path,
    info_payload: dict[str, object],
) -> None:
    options = _embedding_options_with_v2_manifest(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        assert request.url.path == "/info"
        return httpx.Response(200, json=info_payload)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match="MODEL_MISMATCH") as raised,
    ):
        verify_model_contract(options, client=client)

    assert raised.value.code == "MODEL_MISMATCH"


def test_embedding_contract_rejects_missing_deployment_manifest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        assert request.url.path == "/info"
        return httpx.Response(200, json=_tei_info_payload())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ContractError, match="REVISION_MISSING") as raised,
    ):
        verify_model_contract(_options("embedding"), client=client)

    assert raised.value.code == "REVISION_MISSING"


def test_embedding_contract_rejects_schema_v1_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "embedding-deployment-manifest.json"
    _write_manifest(manifest_path, service="embedding")
    options = replace(
        _options("embedding"),
        deployment_manifest=manifest_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        assert request.url.path == "/info"
        return httpx.Response(200, json=_tei_info_payload())

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(
            ContractError,
            match="DEPLOYMENT_MANIFEST_INVALID",
        ) as raised,
    ):
        verify_model_contract(options, client=client)

    assert raised.value.code == "DEPLOYMENT_MANIFEST_INVALID"
