"""用无私有正文的流片段重放 Qwen 兼容接口的终态边界。"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from rag_app.adapters.providers.aliyun_chat import (
    ChatResponseError,
    _ChatStreamAccumulator,
    _sse_data_events,
)

_MODEL = "Qwen/Qwen3-8B-AWQ"


def _frame(
    *,
    delta: dict[str, object] | None = None,
    finish: str | None = None,
    usage: dict[str, int] | None = None,
    model: str = _MODEL,
) -> bytes:
    payload: dict[str, object] = {
        "model": model,
        "choices": [
            {"index": 0, "delta": delta or {}, "finish_reason": finish}
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\r\n\r\n".encode()


def _replay(chunks: Iterator[bytes]) -> tuple[str, list[str]]:
    deltas: list[str] = []
    accumulator = _ChatStreamAccumulator(_MODEL, deltas.append)
    for event in _sse_data_events(chunks):
        accumulator.consume(event)
    return accumulator.complete().content, deltas


def test_fragmented_crlf_utf8_and_multiline_data_complete_once() -> None:
    payload = json.dumps(
        {
            "model": _MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "研发项目 [S1]"},
                    "finish_reason": None,
                }
            ],
        },
        ensure_ascii=False,
    )
    first, second = payload.split(', "choices"', 1)
    body = (
        f'data: {first},\r\ndata: "choices"{second}\r\n\r\n'.encode()
        + _frame(finish="stop")
        + b"data: [DONE]\r\n\r\n"
    )
    assert _replay(iter(bytes((item,)) for item in body)) == (
        "研发项目 [S1]",
        ["研发项目 [S1]"],
    )
    assert _replay(iter((body[:7], body[7:37], body[37:]))) == (
        "研发项目 [S1]",
        ["研发项目 [S1]"],
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            _frame(delta={"content": "草稿"}, finish="length")
            + b"data: [DONE]\n\n",
            "CHAT_OUTPUT_TRUNCATED",
        ),
        (
            _frame(delta={"content": "正文"}, finish="stop"),
            "CHAT_STREAM_MISSING_DONE",
        ),
        (
            _frame(delta={"content": "正文"}) + b"data: [DONE]\n\n",
            "CHAT_FINISH_INVALID",
        ),
        (
            _frame(delta={"reasoning_content": "思考"})
            + _frame(finish="stop")
            + b"data: [DONE]\n\n",
            "CHAT_STREAM_PRIVATE_OUTPUT_FORBIDDEN",
        ),
        (
            _frame(delta={"tool_calls": [{"id": "tool"}]})
            + _frame(finish="stop")
            + b"data: [DONE]\n\n",
            "CHAT_STREAM_PRIVATE_OUTPUT_FORBIDDEN",
        ),
        (
            _frame(delta={"content": "正文"}, usage={"total_tokens": 4})
            + _frame(finish="stop", usage={"total_tokens": 5})
            + b"data: [DONE]\n\n",
            "CHAT_STREAM_USAGE_DUPLICATE",
        ),
        (
            _frame(delta={"content": "正文"}, finish="stop")
            + _frame(finish="stop")
            + b"data: [DONE]\n\n",
            "CHAT_FINISH_INVALID",
        ),
        (
            _frame(delta={"content": "正文"}, finish="stop")
            + _frame(delta={"content": "越界"})
            + b"data: [DONE]\n\n",
            "CHAT_CONTENT_AFTER_FINISH",
        ),
        (
            _frame(delta={"content": "正文"}, model="wrong-model"),
            "CHAT_MODEL_MISMATCH",
        ),
        (b"data: {not-json}\n\n", "CHAT_JSON_INVALID"),
        (b"data: \xff\n\n", "CHAT_SSE_UTF8_INVALID"),
        (
            _frame(delta={"content": "正文"}, finish="stop")
            + b"data: [DONE]\n\ndata: [DONE]\n\n",
            "CHAT_DATA_AFTER_DONE",
        ),
    ],
)
def test_replay_preserves_specific_contract_failure(
    body: bytes, reason: str
) -> None:
    with pytest.raises(ChatResponseError) as raised:
        _replay(iter((body,)))
    assert raised.value.reason_code == reason
