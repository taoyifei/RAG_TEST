"""把 Universal P09 回答流裁剪为湾事通公共字段。"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import cast

from rag_app.core.models import EvidenceItem, SearchAnswerResult

PUBLIC_STREAM_PROTOCOL = "wanshitong-public-sse-v1"
_SSE_FRAME_LINE_COUNT = 3
_PUBLIC_EVENT_FIELDS = {
    "meta": ("protocol", "type", "trace_id", "sequence", "delivery"),
    "stage": ("protocol", "type", "trace_id", "sequence", "stage"),
    "error": (
        "protocol",
        "type",
        "trace_id",
        "sequence",
        "code",
        "message",
        "stage",
        "retryable",
        "partial",
    ),
    "cancelled": (
        "protocol",
        "type",
        "trace_id",
        "sequence",
        "cancel_requested",
        "upstream_close_attempted",
        "upstream_stopped",
    ),
    "final": (
        "protocol",
        "type",
        "trace_id",
        "sequence",
        "status",
        "reason_code",
        "answer",
        "citations",
    ),
}


def render_public_final(result: SearchAnswerResult) -> dict[str, object]:
    """从权威 Final 复制回答语义，并把 Evidence 裁剪为公开引用。

    Args:
        result: 已完成 Grounded Answer 与 Claim Validation 的结果。

    Returns:
        不含 Scope、Revision、分数、向量或 Provider 的公开 Final。

    """
    return {
        "trace_id": result.trace_id,
        "status": result.status.value,
        "reason_code": result.reason_code,
        "answer": result.answer,
        "citations": [_public_citation(item) for item in result.evidence],
    }


def project_public_stream(frames: Iterator[bytes]) -> Iterator[bytes]:
    """对白名单事件逐帧做字段投影，心跳保持原样。

    Args:
        frames: 现有 `P09AnswerStream` 产生的 SSE 帧。

    Yields:
        仅含公共 DTO 字段的 SSE 帧。

    Raises:
        ValueError: 既有流产生未知事件或无效内部帧。

    """
    try:
        for frame in frames:
            if frame.startswith(b":"):
                yield frame
                continue
            event_name, payload = _decode_sse(frame)
            projected = _project_event(event_name, payload)
            yield _encode_sse(event_name, projected)
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            close()


def _public_citation(evidence: EvidenceItem) -> dict[str, object]:
    metadata = dict(evidence.metadata)
    citation: dict[str, object] = {
        "document_name": evidence.display_name or evidence.source_label,
        "locator": evidence.source_label,
        "quote": evidence.citation_text,
    }
    department = metadata.get("department")
    if isinstance(department, str) and department:
        citation["department"] = department
    category_path = metadata.get("category_path")
    if isinstance(category_path, str) and category_path:
        citation["category_path"] = category_path
    elif isinstance(category_path, (list, tuple)) and all(
        isinstance(item, str) and item for item in category_path
    ):
        citation["category_path"] = tuple(category_path)
    return citation


def _decode_sse(frame: bytes) -> tuple[str, dict[str, object]]:
    try:
        lines = frame.decode("utf-8").splitlines()
        if (
            len(lines) != _SSE_FRAME_LINE_COUNT
            or not lines[0].startswith("event: ")
            or not lines[1].startswith("data: ")
            or lines[2]
        ):
            raise ValueError("P09AnswerStream 产生了无效 SSE 帧。")
        event_name = lines[0].removeprefix("event: ")
        payload = json.loads(lines[1].removeprefix("data: "))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("P09AnswerStream 产生了无效 SSE 帧。") from error
    if not isinstance(payload, dict) or event_name not in {
        "meta",
        "stage",
        "claim",
        "final",
        "error",
        "cancelled",
    }:
        raise ValueError("P09AnswerStream 产生了未知公共事件。")
    return event_name, cast(dict[str, object], payload)


def _project_event(
    event_name: str, payload: Mapping[str, object]
) -> dict[str, object]:
    if event_name == "claim":
        claim = payload.get("claim")
        if not isinstance(claim, dict) or not isinstance(
            claim.get("text"), str
        ):
            raise ValueError("P09AnswerStream claim 缺少已验证正文。")
        projected = {
            key: payload[key]
            for key in ("type", "trace_id", "sequence", "provisional")
            if key in payload
        } | {
            "claim_index": payload.get("claim_index"),
            "claim": {"text": claim["text"]},
        }
        projected["protocol"] = PUBLIC_STREAM_PROTOCOL
        return projected
    fields = _PUBLIC_EVENT_FIELDS.get(event_name)
    if fields is None:
        raise ValueError("P09AnswerStream 产生了未知公共事件。")
    projected = {key: payload[key] for key in fields if key in payload}
    projected["protocol"] = PUBLIC_STREAM_PROTOCOL
    return projected


def _encode_sse(event_name: str, payload: object) -> bytes:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"event: {event_name}\ndata: {body}\n\n".encode()


__all__ = [
    "PUBLIC_STREAM_PROTOCOL",
    "project_public_stream",
    "render_public_final",
]
