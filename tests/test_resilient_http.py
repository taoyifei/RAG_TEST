import threading

import httpx
import pytest

from rag_app.clients.resilience import (
    ExternalRequestRejectedError,
    ExternalServiceUnavailableError,
    ResiliencePolicy,
    ResilientHttpPool,
)


def test_pool_rejects_base_url_with_api_path() -> None:
    with pytest.raises(ValueError, match="无路径"):
        ResilientHttpPool(
            ("http://llm:8000/v1",),
            client=httpx.Client(),
            policy=ResiliencePolicy(
                max_attempts=1,
                failure_threshold=1,
                cooldown_seconds=30,
                max_concurrency=1,
            ),
        )


def test_pool_fails_over_and_opens_circuit_without_logging_payload() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        if request.url.host == "bad":
            return httpx.Response(503, json={"detail": "temporary"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pool = ResilientHttpPool(
        ("http://bad", "http://good"),
        client=client,
        policy=ResiliencePolicy(
            max_attempts=2,
            failure_threshold=1,
            cooldown_seconds=30,
            max_concurrency=2,
        ),
    )

    first = pool.request_json("POST", "/work", payload={"secret": "value"})
    second = pool.request_json("POST", "/work", payload={"secret": "value"})

    assert first.payload == {"ok": True}
    assert first.retry_count == 1
    assert second.payload == {"ok": True}
    assert calls == ["bad", "good", "good"]
    assert "secret" not in first.endpoint


def test_pool_distinguishes_terminal_4xx_from_unavailable() -> None:
    rejected_calls: list[str] = []

    def rejected(_: httpx.Request) -> httpx.Response:
        rejected_calls.append("only")
        return httpx.Response(400, json={"detail": "invalid"})

    rejected_pool = ResilientHttpPool(
        ("http://only", "http://must-not-run"),
        client=httpx.Client(transport=httpx.MockTransport(rejected)),
        policy=ResiliencePolicy(
            max_attempts=2,
            failure_threshold=1,
            cooldown_seconds=30,
            max_concurrency=1,
        ),
    )
    with pytest.raises(ExternalRequestRejectedError):
        rejected_pool.request_json("POST", "/work", payload={})
    assert rejected_calls == ["only"]

    def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    unavailable_pool = ResilientHttpPool(
        ("http://only",),
        client=httpx.Client(transport=httpx.MockTransport(unavailable)),
        policy=ResiliencePolicy(
            max_attempts=2,
            failure_threshold=1,
            cooldown_seconds=30,
            max_concurrency=1,
        ),
    )
    with pytest.raises(ExternalServiceUnavailableError):
        unavailable_pool.request_json("POST", "/work", payload={})


def test_schema_validator_error_does_not_switch_endpoint() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        return httpx.Response(200, json={"host": host, "secret": "hidden"})

    def validate(payload: object) -> object:
        if not isinstance(payload, dict) or payload.get("host") != "good":
            raise ValueError("secret response body must not escape")
        return payload

    pool = ResilientHttpPool(
        ("http://bad", "http://good"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(
            max_attempts=2,
            failure_threshold=1,
            cooldown_seconds=30,
            max_concurrency=1,
        ),
    )

    with pytest.raises(ExternalRequestRejectedError) as captured:
        pool.request_json(
            "POST",
            "/work",
            payload={"question": "private"},
            validator=validate,
        )
    assert "secret" not in str(captured.value)
    assert "private" not in str(captured.value)
    assert calls == ["bad"]


def test_four_concurrent_requests_use_multiple_healthy_endpoints() -> None:
    barrier = threading.Barrier(4)
    calls: list[str] = []
    calls_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        with calls_lock:
            calls.append(host)
        barrier.wait(timeout=2)
        return httpx.Response(200, json={"host": host})

    pool = ResilientHttpPool(
        tuple(f"http://llm-{index}" for index in range(4)),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=ResiliencePolicy(
            max_attempts=1,
            failure_threshold=1,
            cooldown_seconds=30,
            max_concurrency=4,
        ),
    )
    errors: list[Exception] = []

    def request() -> None:
        try:
            pool.request_json("POST", "/work", payload={})
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert set(calls) == {"llm-0", "llm-1", "llm-2", "llm-3"}
