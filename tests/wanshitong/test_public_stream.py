"""WB-03 公共 SSE 白名单投影与背压保持门禁。"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from rag_app.wanshitong.public_stream import (
    project_public_stream,
    render_public_final,
)
from tests.wanshitong.support import synthetic_answer

_TRACE_ID = "trace_" + "1" * 32
_PROJECT_ID = "prj_" + "2" * 32
_KNOWLEDGE_BASE_ID = "kb_" + "3" * 32


def _sse(event: str, payload: dict[str, object]) -> bytes:
    return (
        f"event: {event}\ndata: "
        + json.dumps(payload, ensure_ascii=False)
        + "\n\n"
    ).encode()


def _payload(frame: bytes) -> dict[str, object]:
    return json.loads(frame.decode().splitlines()[1].removeprefix("data: "))


@pytest.mark.parametrize(
    ("event_name", "specific"),
    [
        ("meta", {"delivery": "incremental_or_final_only"}),
        (
            "stage",
            {
                "stage": "snapshot",
                "attributes": {"active_index_revision_id": "irev_" + "4" * 32},
            },
        ),
        (
            "claim",
            {
                "claim_index": 0,
                "claim": {
                    "text": "已验证事实",
                    "supports": [{"support_id": "S1", "quote": "事实"}],
                },
                "active_index_revision_id": "irev_" + "4" * 32,
                "provisional": True,
            },
        ),
        (
            "error",
            {
                "code": "SYNTHETIC_ERROR",
                "message": "安全错误。",
                "stage": "answer.stream",
                "retryable": False,
                "partial": True,
            },
        ),
        (
            "cancelled",
            {
                "cancel_requested": True,
                "upstream_close_attempted": True,
                "upstream_stopped": "unknown",
            },
        ),
    ],
)
def test_public_projection_preserves_events_without_scope_or_admin_details(
    event_name: str, specific: dict[str, object]
) -> None:
    internal = {
        "protocol": "rag-answer-sse-v1",
        "type": event_name,
        "trace_id": _TRACE_ID,
        "sequence": 1,
        "project_id": _PROJECT_ID,
        "knowledge_base_id": _KNOWLEDGE_BASE_ID,
        "provider": "secret-provider",
        **specific,
    }
    source = iter([_sse(event_name, internal)])
    projected = next(iter(project_public_stream(source)))
    payload = _payload(projected)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["protocol"] == "wanshitong-public-sse-v1"
    assert payload["type"] == event_name
    assert payload["trace_id"] == _TRACE_ID
    assert "project_id" not in serialized
    assert "knowledge_base_id" not in serialized
    assert "active_index_revision_id" not in serialized
    assert "provider" not in serialized
    assert "attributes" not in payload
    if event_name == "claim":
        assert payload["claim"] == {"text": "已验证事实"}


def test_public_final_contains_only_answer_and_citation_dto() -> None:
    final = render_public_final(synthetic_answer(_TRACE_ID))
    serialized = json.dumps(final, ensure_ascii=False)

    assert set(final) == {
        "trace_id",
        "status",
        "reason_code",
        "answer",
        "citations",
    }
    assert "chunk_id" not in serialized
    assert "score" not in serialized
    assert "vector" not in serialized
    assert "provider" not in serialized
    assert set(final["citations"][0]) == {
        "document_name",
        "department",
        "department_name",
        "category_path",
        "document_title",
        "locator",
        "quote",
        "source_relative_path",
    }


def test_projection_passes_heartbeat_and_closes_upstream() -> None:
    closed = False

    def _frames() -> Iterator[bytes]:
        nonlocal closed
        try:
            yield b": heartbeat 10\n\n"
            yield _sse(
                "meta",
                {
                    "protocol": "rag-answer-sse-v1",
                    "type": "meta",
                    "trace_id": _TRACE_ID,
                    "sequence": 0,
                    "project_id": _PROJECT_ID,
                    "knowledge_base_id": _KNOWLEDGE_BASE_ID,
                },
            )
        finally:
            closed = True

    stream = project_public_stream(_frames())
    assert next(stream) == b": heartbeat 10\n\n"
    stream.close()
    assert closed is True


def test_projection_rejects_unknown_internal_event() -> None:
    frame = _sse("token", {"token": "未经验证正文"})
    with pytest.raises(ValueError, match="未知公共事件"):
        next(iter(project_public_stream(iter([frame]))))


@pytest.mark.parametrize("terminal", ["final", "error", "cancelled"])
def test_public_projection_preserves_one_terminal_event(terminal: str) -> None:
    payload: dict[str, object] = {
        "protocol": "rag-answer-sse-v1",
        "type": terminal,
        "trace_id": _TRACE_ID,
        "sequence": 1,
    }
    if terminal == "final":
        payload.update(status="INSUFFICIENT_EVIDENCE", answer="", citations=[])
    elif terminal == "error":
        payload.update(
            code="SYNTHETIC", message="安全错误", stage="answer.stream"
        )
    else:
        payload.update(
            cancel_requested=True,
            upstream_close_attempted=False,
            upstream_stopped="confirmed",
        )
    frames = iter([_sse(terminal, payload), _sse("error", payload)])
    projected = list(project_public_stream(frames))
    assert len(projected) == 1
    assert projected[0].startswith(f"event: {terminal}\n".encode())
