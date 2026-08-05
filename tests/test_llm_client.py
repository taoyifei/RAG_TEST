import json
import threading
import uuid
from collections.abc import Iterator

import httpx
import pytest

from rag_app.clients.llm import BufferedLlmClient, ChatMessage
from rag_app.clients.resilience import (
    ExternalRequestRejectedError,
    ExternalStreamInterruptedError,
    ResiliencePolicy,
    ResilientHttpPool,
    StreamCancellation,
)

_MODEL = "Qwen/Qwen3-8B-AWQ"


class _ChunkedStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _InterruptedStream(httpx.SyncByteStream):
    def __init__(self, first_chunk: bytes) -> None:
        self._first_chunk = first_chunk
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield self._first_chunk
        raise httpx.ReadError("synthetic stream interruption")

    def close(self) -> None:
        self.closed = True


class _BlockingStream(httpx.SyncByteStream):
    def __init__(self, first_chunk: bytes) -> None:
        self._first_chunk = first_chunk
        self.closed = threading.Event()

    def __iter__(self) -> Iterator[bytes]:
        yield self._first_chunk
        self.closed.wait(timeout=2)
        raise httpx.ReadError("stream closed")

    def close(self) -> None:
        self.closed.set()


def _sse(payload: object) -> bytes:
    return (
        "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode()


def _delta(content: str, *, finish_reason: str | None = None) -> object:
    return {
        "model": _MODEL,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": None,
    }


def _usage() -> object:
    return {
        "model": _MODEL,
        "choices": [],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
        },
    }


def _streaming_client(handler: object) -> BufferedLlmClient:
    return BufferedLlmClient(
        ResilientHttpPool(
            ("http://first", "http://second"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            policy=_policy(),
        ),
        model=_MODEL,
        max_context_tokens=8192,
        api_token=None,
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
        ExternalRequestRejectedError,
        match="INVALID_RESPONSE_SCHEMA",
    ):
        client.generate(
            (ChatMessage(role="user", content="问题"),),
            max_output_tokens=128,
        )


def test_llm_schema_error_does_not_generate_on_another_replica() -> None:
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

    with pytest.raises(
        ExternalRequestRejectedError,
        match="INVALID_RESPONSE_SCHEMA",
    ):
        client.generate(
            (ChatMessage(role="user", content="问题"),),
            max_output_tokens=128,
        )

    assert calls == ["bad"]


def test_streaming_llm_parses_sse_and_utf8_at_every_byte_boundary() -> None:
    content = (
        '{"claims":[{"text":"需求变更应书面确认。",'
        '"support_ids":["E1:S1"]}]}'
    )
    body = b"".join(
        (
            _sse(_delta(content[:18])),
            _sse(_delta(content[18:])),
            _sse(_delta("", finish_reason="stop")),
            _sse(_usage()),
            b"data: [DONE]\n\n",
        )
    )
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            stream=_ChunkedStream(
                [body[index : index + 1] for index in range(len(body))]
            ),
        )

    deltas: list[str] = []
    result = _streaming_client(handler).generate_stream(
        (ChatMessage(role="user", content="问题"),),
        max_output_tokens=128,
        response_format={"type": "json_schema"},
        on_delta=deltas.append,
        cancellation=StreamCancellation(),
    )

    assert "".join(deltas) == content
    assert result.content == content
    assert result.usage.total_tokens == 14
    assert result.stream is not None
    assert result.stream.delta_count == 2
    assert result.stream.finish_reason == "stop"
    assert result.stream.first_delta_seconds is not None
    assert payloads[0]["stream"] is True
    assert payloads[0]["stream_options"] == {"include_usage": True}
    assert payloads[0]["chat_template_kwargs"] == {
        "enable_thinking": False
    }


@pytest.mark.parametrize("transient_status", [503, 599])
def test_streaming_llm_fails_over_once_before_first_content_delta(
    transient_status: int,
) -> None:
    calls: list[str] = []
    content = '{"claims":[]}'
    successful = b"".join(
        (
            _sse(_delta(content)),
            _sse(_delta("", finish_reason="stop")),
            _sse(_usage()),
            b"data: [DONE]\n\n",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        if host == "first":
            return httpx.Response(transient_status)
        return httpx.Response(200, stream=_ChunkedStream([successful]))

    result = _streaming_client(handler).generate_stream(
        (ChatMessage(role="user", content="问题"),),
        max_output_tokens=128,
        on_delta=lambda _: None,
        cancellation=StreamCancellation(),
    )

    assert result.content == content
    assert result.call.endpoint == "http://second"
    assert result.call.retry_count == 1
    assert calls == ["first", "second"]


def test_streaming_llm_does_not_replay_after_first_content_delta() -> None:
    calls: list[str] = []
    first_delta = _sse(_delta('{"claims":['))

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        calls.append(host)
        if host == "first":
            return httpx.Response(
                200,
                stream=_InterruptedStream(first_delta),
            )
        raise AssertionError("首 delta 后不得切换副本。")

    observed: list[str] = []
    with pytest.raises(
        ExternalStreamInterruptedError,
        match="LLM_STREAM_INTERRUPTED",
    ):
        _streaming_client(handler).generate_stream(
            (ChatMessage(role="user", content="问题"),),
            max_output_tokens=128,
            on_delta=observed.append,
            cancellation=StreamCancellation(),
        )

    assert observed == ['{"claims":[']
    assert calls == ["first"]


def test_streaming_llm_cancellation_closes_upstream_without_failover() -> None:
    calls: list[str] = []
    stream = _BlockingStream(_sse(_delta('{"claims":[')))
    cancellation = StreamCancellation()
    errors: list[BaseException] = []
    first_delta = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host or "")
        return httpx.Response(200, stream=stream)

    def run() -> None:
        try:
            _streaming_client(handler).generate_stream(
                (ChatMessage(role="user", content="问题"),),
                max_output_tokens=128,
                on_delta=lambda _: first_delta.set(),
                cancellation=cancellation,
            )
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert first_delta.wait(timeout=1)

    cancellation.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert stream.closed.is_set()
    assert len(errors) == 1
    assert calls == ["first"]
