"""新引擎映射与引用的逐用户隔离。"""

from __future__ import annotations

from pathlib import Path

from wanshitong_gateway.store import GatewayStore


def test_conversation_and_turns_are_user_scoped(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    assert (
        store.bind_session(
            deployment_id="candidate",
            owner_id="rdms:candidate:u1",
            conversation_id="same-browser-id",
            native_session_id="native-u1",
        )
        == "native-u1"
    )
    store.bind_session(
        deployment_id="candidate",
        owner_id="rdms:candidate:u2",
        conversation_id="same-browser-id",
        native_session_id="native-u2",
    )
    assert (
        store.native_session(
            deployment_id="candidate",
            owner_id="rdms:candidate:u2",
            conversation_id="same-browser-id",
        )
        == "native-u2"
    )
    assert (
        store.bind_session(
            deployment_id="candidate",
            owner_id="rdms:candidate:u1",
            conversation_id="same-browser-id",
            native_session_id="unused-session",
        )
        == "native-u1"
    )
    store.start_turn(
        trace_id="trace-u1",
        deployment_id="candidate",
        owner_id="rdms:candidate:u1",
        conversation_id="same-browser-id",
        native_session_id="native-u1",
        question="问题",
    )
    assert (
        store.get_turn(
            trace_id="trace-u1", owner_id="rdms:candidate:u2"
        )
        is None
    )
    assert store.get_turn(trace_id="trace-u1", owner_id="rdms:candidate:u1")


def test_reference_requires_trace_mapping(tmp_path: Path) -> None:
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    store.bind_session(
        deployment_id="candidate",
        owner_id="owner",
        conversation_id="conversation",
        native_session_id="native",
    )
    store.start_turn(
        trace_id="trace",
        deployment_id="candidate",
        owner_id="owner",
        conversation_id="conversation",
        native_session_id="native",
        question="问题",
    )
    store.add_reference(
        reference_id="ref_123",
        trace_id="trace",
        source={"id": "chunk-1", "knowledge_id": "document-1"},
        resource_handle="resource://verified-handle",
    )
    assert store.get_reference(reference_id="ref_123", trace_id="other") is None
    reference = store.get_reference(reference_id="ref_123", trace_id="trace")
    assert reference is not None
    assert reference["resource_handle"] == "resource://verified-handle"


def test_trace_export_keeps_native_events_and_redacts_by_default(
    tmp_path: Path,
) -> None:
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    store.bind_session(
        deployment_id="candidate",
        owner_id="owner",
        conversation_id="conversation",
        native_session_id="native",
    )
    store.start_turn(
        trace_id="trace-export",
        deployment_id="candidate",
        owner_id="owner",
        conversation_id="conversation",
        native_session_id="native",
        question="原始问题  ",
        client_context={"entrypoint": "retry"},
        kb_scope=("kb-1",),
    )
    store.record_event(
        trace_id="trace-export",
        sequence=0,
        event_type="answer_delta",
        payload={"text": "原生回答", "native_request_id": "request-1"},
    )
    store.finish_turn(
        trace_id="trace-export",
        status="completed",
        answer="原生回答",
        references=(),
        native_message_id="message-1",
        native_request_id="request-1",
        truncated=False,
        finish_reason="stop",
    )
    redacted = store.trace_export(
        trace_id="trace-export", include_content=False
    )
    full = store.trace_export(trace_id="trace-export", include_content=True)
    assert redacted is not None and full is not None
    assert redacted["question"] is None
    assert redacted["answer"] is None
    assert redacted["events"][0]["payload"] is None
    assert full["question"] == "原始问题  "
    assert full["answer"] == "原生回答"
    assert full["events"][0]["payload"]["text"] == "原生回答"
    assert full["client_context"] == {"entrypoint": "retry"}
    assert full["kb_scope"] == ["kb-1"]
