from __future__ import annotations

import httpx
import pytest

from rag_app.adapters.providers.http_common import (
    ProviderHttpClient,
    ProviderHttpError,
)
from rag_app.core.models import ProviderCall, ProviderFailureCategory


def _request(client: ProviderHttpClient) -> object:
    return client.request_json(
        "POST",
        "/embeddings",
        payload={"private": "text"},
        headers={"Authorization": "Bearer secret-value"},
        provider_id="test-provider",
        operation="embedding",
        model="test-model",
        input_count=1,
        estimated_tokens=4,
    )


def test_invalid_json_and_content_type_are_contract_failures() -> None:
    responses = iter(
        (
            httpx.Response(
                200,
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            ),
            httpx.Response(
                200,
                content=b"{}",
                headers={"Content-Type": "text/plain"},
            ),
        )
    )
    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: next(responses))
        ),
    )
    with pytest.raises(ProviderHttpError) as invalid_json:
        _request(client)
    with pytest.raises(ProviderHttpError) as invalid_content_type:
        _request(client)
    client.close()

    assert (
        invalid_json.value.category is ProviderFailureCategory.RESPONSE_CONTRACT
    )
    assert invalid_json.value.reason_code == "INVALID_JSON"
    assert invalid_content_type.value.reason_code == "INVALID_CONTENT_TYPE"
    assert "secret-value" not in str(invalid_json.value)
    assert "private" not in str(invalid_json.value)


def test_response_byte_limit_fails_closed() -> None:
    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"value": "long"})
            )
        ),
        max_response_bytes=4,
    )
    with pytest.raises(ProviderHttpError) as captured:
        _request(client)
    client.close()
    assert captured.value.reason_code == "RESPONSE_TOO_LARGE"


def test_deferred_success_is_observed_only_after_completion() -> None:
    events: list[ProviderCall] = []
    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={})
            )
        ),
        observer=events.append,
        defer_success_observation=True,
    )

    result = _request(client)
    assert events == []
    completed_call = client.complete_call(
        result.call,
        observed_tokens=7,
    )
    client.close()

    assert completed_call.status_category == "SUCCESS"
    assert completed_call.observed_tokens == 7
    assert events == [completed_call]


def test_deferred_semantic_failure_is_observed_without_prior_success() -> None:
    events: list[ProviderCall] = []
    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={})
            )
        ),
        observer=events.append,
        defer_success_observation=True,
    )

    result = _request(client)
    completed_call = client.complete_call(
        result.call,
        observed_tokens=5,
        failure_reason_code="INVALID_RESPONSE_CONTRACT",
    )
    client.close()

    assert completed_call.status_category == "RESPONSE_CONTRACT"
    assert completed_call.reason_code == "INVALID_RESPONSE_CONTRACT"
    assert completed_call.observed_tokens == 5
    assert events == [completed_call]


def test_retry_after_http_date_is_respected() -> None:
    sleeps: list[float] = []
    events: list[ProviderCall] = []
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "Thu, 01 Jan 1970 00:00:02 GMT"},
                json={},
            )
        return httpx.Response(200, json={})

    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        sleeper=sleeps.append,
        wall_clock=lambda: 0.0,
        random_value=lambda: 0.0,
        observer=events.append,
    )
    result = _request(client)
    client.close()
    assert calls == 2
    assert sleeps == [2.0]
    assert result.call.retry_after_ms == 2000
    assert result.call.rate_limited is True
    assert events == [result.call]


@pytest.mark.parametrize("status", (400, 422))
def test_400_and_422_are_not_retried(status: int) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={})

    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderHttpError) as captured:
        _request(client)
    client.close()
    assert captured.value.category is ProviderFailureCategory.INPUT_INVALID
    assert calls == 1


def test_close_is_idempotent_and_rejects_future_calls() -> None:
    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={})
            )
        ),
    )
    client.close()
    client.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        _request(client)


@pytest.mark.parametrize(
    "base_url",
    (
        "http://provider.example",
        "https://user:secret@provider.example",
        "https://provider.example?token=secret",
    ),
)
def test_base_url_rejects_insecure_or_secret_bearing_values(
    base_url: str,
) -> None:
    with pytest.raises(ValueError):
        ProviderHttpClient(base_url)
