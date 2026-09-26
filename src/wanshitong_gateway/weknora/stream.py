"""把原生 WeKnora SSE 转为湾事通可展示的传输事件。"""

from __future__ import annotations

import codecs
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from wanshitong_gateway.weknora.client import classify_native_error

PROTOCOL = "wanshitong-weknora-sse-v1"
_FRAME_END = re.compile(r"\r\n\r\n|\n\n|\r\r")


@dataclass(frozen=True, slots=True)
class NativeEvent:
    """原生 SSE 事件及 JSON 正文。"""

    event: str
    payload: Mapping[str, Any]


class NativeSseDecoder:
    """在任意 UTF-8 和 SSE 字节边界上还原原生事件。"""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[NativeEvent]:
        """接收一段字节，返回其中已完整结束的事件。"""
        self._buffer += self._decoder.decode(chunk)
        events: list[NativeEvent] = []
        while match := _FRAME_END.search(self._buffer):
            frame = self._buffer[: match.start()]
            self._buffer = self._buffer[match.end() :]
            event = _parse_frame(frame)
            if event is not None:
                events.append(event)
        return events

    def finish(self) -> list[NativeEvent]:
        """EOF 时只接受已用空行完整结束的事件。"""
        self._buffer += self._decoder.decode(b"", final=True)
        events = self.feed(b"")
        if self._buffer.strip():
            raise ValueError("原生 SSE 在事件中途结束。")
        return events


def _parse_frame(frame: str) -> NativeEvent | None:
    event_type = "message"
    data_lines: list[str] = []
    for line in frame.splitlines():
        if not line or line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
    if not data_lines:
        return None
    payload = json.loads("\n".join(data_lines))
    if not isinstance(payload, dict):
        raise ValueError("原生 SSE 事件正文必须为 JSON 对象。")
    return NativeEvent(event=event_type, payload=payload)


def encode_event(event_type: str, payload: Mapping[str, Any]) -> bytes:
    """生成一帧不缓冲的湾事通 SSE。"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {body}\n\n".encode()


class BridgeStream:
    """保留原生回答增量、引用顺序和明确终态的单轮状态。"""

    def __init__(
        self,
        *,
        trace_id: str,
        session_id: str,
        reference_mapper: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        self.trace_id = trace_id
        self.session_id = session_id
        self._reference_mapper = reference_mapper
        self._sequence = 0
        self._answer: list[str] = []
        self._references: list[dict[str, Any]] = []
        self._native_request_id: str | None = None
        self._message_id: str | None = None
        self._usage: Mapping[str, Any] | None = None
        self._finish_reason: str | None = None
        self._truncated = False
        self._stopped = False
        self.terminal = False
        self.terminal_type: str | None = None
        self.error_category: str | None = None

    @property
    def answer(self) -> str:
        """当前收到的原生正文，保持字面内容。"""
        return "".join(self._answer)

    @property
    def references(self) -> tuple[Mapping[str, Any], ...]:
        """当前收到的引用，保持原生顺序。"""
        return tuple(self._references)

    @property
    def message_id(self) -> str | None:
        """原生助手消息 ID，可用于停止生成。"""
        return self._message_id

    @property
    def request_id(self) -> str | None:
        """原生请求 ID。"""
        return self._native_request_id

    @property
    def truncated(self) -> bool:
        """原生回答是否报告截断。"""
        return self._truncated

    @property
    def finish_reason(self) -> str | None:
        """原生结束原因，未返回时保持空值。"""
        return self._finish_reason

    def meta(self) -> bytes:
        """在原生请求前发送本轮映射信息。"""
        return self._emit(
            "meta",
            {"native_session_id": self.session_id, "engine": "weknora"},
        )

    def accept(self, native: NativeEvent) -> list[bytes]:  # noqa: PLR0911, PLR0912 - 原生事件逐类保留独立终态。
        """处理一帧原生事件，不把局部 done 误判为整轮结束。"""
        if self.terminal:
            return []
        payload = native.payload
        response_type = payload.get("response_type")
        if native.event != "message" or not isinstance(response_type, str):
            raise ValueError("原生 SSE 事件类型无效。")
        self._capture_metadata(payload)
        if response_type == "answer":
            content = payload.get("content", "")
            if not isinstance(content, str):
                raise ValueError("原生 answer.content 类型无效。")
            data = payload.get("data")
            if isinstance(data, dict) and data.get("truncated") is True:
                self._truncated = True
            if not content:
                return [
                    self._emit(
                        "native_event",
                        {"response_type": response_type, "native": payload},
                    )
                ]
            self._answer.append(content)
            return [
                self._emit(
                    "answer_delta",
                    {
                        "text": content,
                        "native_request_id": self._native_request_id,
                        "native_message_id": self._message_id,
                        "truncated": self._truncated,
                        "native": payload,
                    },
                )
            ]
        if response_type == "references":
            raw_references = payload.get("knowledge_references")
            if not isinstance(raw_references, list):
                raise ValueError("原生 references 缺少引用数组。")
            mapped: list[dict[str, Any]] = []
            for raw in raw_references:
                if not isinstance(raw, dict):
                    raise ValueError("原生引用必须为对象。")
                mapped.append(dict(self._reference_mapper(raw)))
            self._references = mapped
            return [
                self._emit("references", {"items": mapped, "native": payload})
            ]
        if response_type == "complete":
            self.terminal = True
            self.terminal_type = "stopped" if self._stopped else "complete"
            if self._stopped:
                return [
                    self._emit(
                        "cancelled",
                        {
                            "native_message_id": self._message_id,
                            "native_request_id": self._native_request_id,
                            "finish_reason": self._finish_reason,
                        },
                    )
                ]
            return [
                self._emit(
                    "final",
                    {
                        "status": "completed",
                        "answer": self.answer,
                        "citations": self._references,
                        "native_session_id": self.session_id,
                        "native_message_id": self._message_id,
                        "native_request_id": self._native_request_id,
                        "usage": self._usage,
                        "finish_reason": self._finish_reason,
                        "truncated": self._truncated,
                        "native": payload,
                    },
                )
            ]
        if response_type == "error":
            self.terminal = True
            self.terminal_type = "error"
            self.error_category = classify_native_error(payload.get("content"))
            return [
                self._emit(
                    "error",
                    {
                        "code": self.error_category,
                        "message": "原生回答失败，请联系管理员查看诊断。",
                        "partial": bool(self._answer),
                        "native_request_id": self._native_request_id,
                        "native_message_id": self._message_id,
                    },
                )
            ]
        if response_type == "stop":
            self._stopped = True
            self._finish_reason = "user_requested"
            return [
                self._emit(
                    "native_event",
                    {
                        "response_type": response_type,
                        "native_request_id": self._native_request_id,
                        "native": payload,
                    },
                )
            ]
        return [
            self._emit(
                "native_event",
                {
                    "response_type": response_type,
                    "done": payload.get("done"),
                    "native_request_id": self._native_request_id,
                    "native": payload,
                },
            )
        ]

    def disconnected(self, *, code: str = "UPSTREAM_EOF") -> bytes | None:
        """无明确 complete/error 的断流一律作为失败。"""
        if self.terminal:
            return None
        self.terminal = True
        self.terminal_type = "disconnected"
        self.error_category = code
        return self._emit(
            "error",
            {
                "code": code,
                "message": "原生回答流未正常结束。",
                "partial": bool(self._answer),
                "native_request_id": self._native_request_id,
            },
        )

    def _capture_metadata(self, payload: Mapping[str, Any]) -> None:
        request_id = payload.get("id")
        if isinstance(request_id, str) and request_id:
            self._native_request_id = request_id
        message_id = payload.get("assistant_message_id")
        if isinstance(message_id, str) and message_id:
            self._message_id = message_id
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self._usage = usage
        finish_reason = payload.get("finish_reason")
        if isinstance(finish_reason, str):
            self._finish_reason = finish_reason

    def _emit(self, event_type: str, data: Mapping[str, Any]) -> bytes:
        frame = encode_event(
            event_type,
            {
                "protocol": PROTOCOL,
                "type": event_type,
                "trace_id": self.trace_id,
                "sequence": self._sequence,
                **data,
            },
        )
        self._sequence += 1
        return frame
