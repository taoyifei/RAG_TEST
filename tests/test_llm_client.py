import uuid

import httpx
import pytest

from rag_app.clients.llm import BufferedLlmClient, ChatMessage
from rag_app.clients.resilience import (
    ExternalServiceUnavailableError,
    ResiliencePolicy,
    ResilientHttpPool,
)


def _policy() -> ResiliencePolicy:
    return ResiliencePolicy(
        max_attempts=2,
        failure_threshold=1,
        cooldown_seconds=30,
        max_concurrency=2,
    )


def test_buffered_llm_fails_over_before_publishing_content() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bad":
            return httpx.Response(503)
        payload = httpx.Response(200, content=request.read()).json()
        payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "完整答案",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )

    pool = ResilientHttpPool(
        ("http://bad", "http://good"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        policy=_policy(),
    )
    client = BufferedLlmClient(
        pool,
        model="Qwen/Qwen3-8B-AWQ",
        max_context_tokens=8192,
        api_token=uuid.uuid4().hex,
    )

    result = client.generate(
        (
            ChatMessage(role="system", content="只按证据回答。"),
            ChatMessage(role="user", content="问题"),
        ),
        max_output_tokens=128,
    )

    assert result.content == "完整答案"
    assert result.call.endpoint == "http://good"
    assert result.call.retry_count == 1
    assert payloads == [
        {
            "model": "Qwen/Qwen3-8B-AWQ",
            "messages": [
                {"role": "system", "content": "只按证据回答。"},
                {"role": "user", "content": "问题"},
            ],
            "temperature": 0,
            "max_tokens": 128,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ]


def test_buffered_llm_rejects_truncated_generation() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "未完成"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    client = BufferedLlmClient(
        ResilientHttpPool(
            ("http://only",),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            policy=_policy(),
        ),
        model="Qwen/Qwen3-8B-AWQ",
        max_context_tokens=8192,
        api_token=None,
    )
    with pytest.raises(
        ExternalServiceUnavailableError,
        match="INVALID_RESPONSE_SCHEMA",
    ):
        client.generate(
            (ChatMessage(role="user", content="问题"),),
            max_output_tokens=128,
        )


def test_llm_schema_error_fails_over_before_completion() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        finish_reason = "length" if host == "bad" else "stop"
        return httpx.Response(
            200,
            json={
                "model": "Qwen/Qwen3-8B-AWQ",
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": "完整答案"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )

    client = BufferedLlmClient(
        ResilientHttpPool(
            ("http://bad", "http://good"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            policy=_policy(),
        ),
        model="Qwen/Qwen3-8B-AWQ",
        max_context_tokens=8192,
        api_token=None,
    )

    result = client.generate(
        (ChatMessage(role="user", content="问题"),),
        max_output_tokens=128,
    )

    assert result.content == "完整答案"
    assert result.call.endpoint == "http://good"
    assert result.call.retry_count == 1
    assert calls == ["bad", "good"]
