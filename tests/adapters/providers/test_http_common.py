from __future__ import annotations

import socket
import ssl
from typing import NoReturn

import httpx
import pytest

from rag_app.adapters.providers.http_common import (
    ProviderHttpClient,
    ProviderHttpError,
)
from rag_app.adapters.providers.transport_diagnostics import (
    transport_diagnostics,
)
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import ProviderInvalidResponse
from rag_app.core.models import ProviderCall, ProviderFailureCategory


@pytest.mark.parametrize(
    "error_class",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
def test_transport_diagnostics_are_safe_and_attempt_local(
    error_class: type[httpx.TransportError],
) -> None:
    """合成异常只验证诊断，不作为历史真实故障证据。"""
    attempts = []

    def handler(request: httpx.Request) -> NoReturn:
        attempts.append(request)
        raise error_class("private query secret-value", request=request)

    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        sleeper=lambda _: None,
    )
    with pytest.raises(ProviderHttpError) as captured:
        _request(client)
    client.close()
    diagnostics = dict(captured.value.call.transport_diagnostics)
    assert diagnostics["transport_error_type"] == error_class.__name__
    assert diagnostics["cause_type"] == "UNKNOWN"
    assert diagnostics["attempt_elapsed_ms"] >= 0
    assert diagnostics["retry_index"] == 1
    assert diagnostics["max_attempts"] == 2
    assert len(attempts) == 2
    assert "secret-value" not in captured.value.call.model_dump_json()


@pytest.mark.parametrize(
    "error_class", [httpx.ProxyError, httpx.LocalProtocolError]
)
def test_configuration_transport_errors_are_not_retried(
    error_class: type[httpx.TransportError],
) -> None:
    calls = []

    def handler(request: httpx.Request) -> NoReturn:
        calls.append(request)
        raise error_class("secret-value", request=request)

    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
    )
    with pytest.raises(ProviderHttpError):
        _request(client)
    client.close()
    assert len(calls) == 1


def test_certificate_failure_is_sanitized_and_not_retried() -> None:
    calls: list[httpx.Request] = []
    certificate = ssl.SSLCertVerificationError(1, "secret certificate details")
    certificate.verify_code = 62

    def handler(request: httpx.Request) -> NoReturn:
        calls.append(request)
        raise httpx.ConnectError(
            "secret-value", request=request
        ) from certificate

    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
    )
    with pytest.raises(ProviderHttpError) as captured:
        _request(client)
    client.close()
    diagnostics = dict(captured.value.call.transport_diagnostics)
    assert len(calls) == 1
    assert diagnostics["cause_type"] == "SSLCertVerificationError"
    assert diagnostics["certificate_reason"] == "HOSTNAME_MISMATCH"
    assert "secret" not in captured.value.call.model_dump_json()


def test_safe_dns_cause_and_bounded_cycle_without_message_copy() -> None:
    cause = socket.gaierror(socket.EAI_AGAIN, "secret proxy query")
    error = httpx.ConnectError("secret-value")
    error.__cause__ = cause
    cause.__cause__ = error
    details = transport_diagnostics(error, elapsed_ms=4, extensions={})
    assert details["cause_type"] == "gaierror"
    assert details["errno"] == socket.EAI_AGAIN
    assert "secret" not in str(details)


def test_single_attempt_elapsed_excludes_prior_attempt_and_backoff() -> None:
    ticks = iter((0.0, 0.0, 0.2, 0.2, 10.2, 10.3, 10.3))

    def handler(request: httpx.Request) -> NoReturn:
        raise httpx.ReadTimeout("unit_synthetic", request=request)

    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        monotonic=lambda: next(ticks),
        sleeper=lambda _: None,
    )
    with pytest.raises(ProviderHttpError) as captured:
        _request(client)
    client.close()
    assert captured.value.call.elapsed_ms == 10300
    assert (
        dict(captured.value.call.transport_diagnostics)["attempt_elapsed_ms"]
        == 100
    )


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


def _stream_request(client: ProviderHttpClient) -> object:
    return client.request_stream(
        "POST",
        "/chat/completions",
        payload={"private": "text"},
        headers={"Authorization": "Bearer secret-value"},
        provider_id="test-provider",
        operation="generation",
        model="test-model",
        input_count=1,
        estimated_tokens=4,
        consumer=b"".join,
        cancellation=StreamCancellation(),
    )


def test_stream_success_is_observed_without_deferred_completion() -> None:
    events: list[ProviderCall] = []
    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=b"data: [DONE]\\n\\n",
                    headers={"Content-Type": "text/event-stream"},
                )
            )
        ),
        observer=events.append,
    )

    result = _stream_request(client)
    client.close()

    assert events == [result.call]


def test_stream_total_byte_limit_cannot_be_bypassed_by_small_events() -> None:
    events: list[ProviderCall] = []
    client = ProviderHttpClient(
        "https://provider.example/v1",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=b"data: {}\\n\\ndata: {}\\n\\n",
                    headers={"Content-Type": "text/event-stream"},
                )
            )
        ),
        max_response_bytes=8,
        observer=events.append,
    )

    with pytest.raises(ProviderInvalidResponse) as captured:
        _stream_request(client)
    client.close()

    assert captured.value.code == "RESPONSE_TOO_LARGE"
    assert events[0].reason_code == "RESPONSE_TOO_LARGE"


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
