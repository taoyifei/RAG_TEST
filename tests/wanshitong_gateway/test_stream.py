"""固定版原生 SSE 的分片、正文和终态回放。"""

from __future__ import annotations

import json

import pytest

from wanshitong_gateway.weknora.stream import (
    BridgeStream,
    NativeEvent,
    NativeSseDecoder,
)


def _native(response_type: str, **fields: object) -> NativeEvent:
    return NativeEvent(
        event="message",
        payload={"id": "request-1", "response_type": response_type, **fields},
    )


def _decode_bridge(frame: bytes) -> tuple[str, dict[str, object]]:
    lines = frame.decode("utf-8").splitlines()
    return lines[0].removeprefix("event: "), json.loads(
        lines[1].removeprefix("data: ")
    )


@pytest.mark.parametrize("fragment_size", [1, 2, 7, 1024])
def test_native_sse_handles_arbitrary_utf8_fragments(
    fragment_size: int,
) -> None:
    payload = json.dumps(
        {"response_type": "answer", "content": "湾事通📝"},
        ensure_ascii=False,
    )
    wire = f": heartbeat\r\nevent: message\r\ndata: {payload}\r\n\r\n".encode()
    parser = NativeSseDecoder()
    events = []
    for index in range(0, len(wire), fragment_size):
        events.extend(parser.feed(wire[index : index + fragment_size]))
    events.extend(parser.finish())
    assert len(events) == 1
    assert events[0].payload["content"] == "湾事通📝"


def test_answer_done_does_not_complete_round() -> None:
    bridge = BridgeStream(
        trace_id="trace_11111111111111111111111111111111",
        session_id="session-a",
        reference_mapper=lambda item: item,
    )
    first = bridge.accept(_native("answer", content="第一段", done=True))
    assert _decode_bridge(first[0])[1]["text"] == "第一段"
    assert bridge.terminal is False
    second = bridge.accept(_native("answer", content="第二段", done=False))
    assert _decode_bridge(second[0])[1]["text"] == "第二段"
    completed = bridge.accept(
        _native("complete", done=True, finish_reason="stop")
    )
    event_type, final = _decode_bridge(completed[0])
    assert event_type == "final"
    assert final["answer"] == "第一段第二段"
    assert final["finish_reason"] == "stop"
    assert bridge.terminal is True


def test_references_before_answer_keep_native_order_and_truncation() -> None:
    bridge = BridgeStream(
        trace_id="trace_22222222222222222222222222222222",
        session_id="session-b",
        reference_mapper=lambda item: {
            "document_name": item["knowledge_title"],
            "native_id": item["id"],
        },
    )
    references = [
        {"id": "chunk-2", "knowledge_title": "第二份"},
        {"id": "chunk-1", "knowledge_title": "第一份"},
    ]
    bridge.accept(
        _native("references", knowledge_references=references, done=True)
    )
    bridge.accept(
        _native(
            "answer",
            content="原文 *Markdown*",
            done=True,
            data={"truncated": True},
        )
    )
    completed = bridge.accept(_native("complete", done=True))
    final = _decode_bridge(completed[0])[1]
    assert final["answer"] == "原文 *Markdown*"
    assert [item["native_id"] for item in final["citations"]] == [
        "chunk-2",
        "chunk-1",
    ]
    assert final["truncated"] is True


def test_eof_is_error_after_partial_answer() -> None:
    bridge = BridgeStream(
        trace_id="trace_33333333333333333333333333333333",
        session_id="session-c",
        reference_mapper=lambda item: item,
    )
    bridge.accept(_native("answer", content="未完成"))
    frame = bridge.disconnected()
    assert frame is not None
    event_type, error = _decode_bridge(frame)
    assert event_type == "error"
    assert error["partial"] is True
    assert error["code"] == "UPSTREAM_EOF"
    assert bridge.terminal is True


def test_decoder_rejects_incomplete_event() -> None:
    parser = NativeSseDecoder()
    assert parser.feed(b"event: message\ndata: {}") == []
    with pytest.raises(ValueError, match="中途结束"):
        parser.finish()


def test_unmapped_native_event_keeps_complete_payload() -> None:
    bridge = BridgeStream(
        trace_id="trace_44444444444444444444444444444444",
        session_id="session-d",
        reference_mapper=lambda item: item,
    )
    payload = {
        "id": "request-1",
        "response_type": "thinking",
        "content": "推理原文  ",
        "data": {"step": 2, "details": ["一", "二"]},
        "done": False,
    }
    frame = bridge.accept(NativeEvent(event="message", payload=payload))[0]
    event_type, data = _decode_bridge(frame)
    assert event_type == "native_event"
    assert data["native"] == payload
