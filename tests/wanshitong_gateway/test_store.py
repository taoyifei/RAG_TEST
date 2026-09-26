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


def test_ops_uses_native_turns_for_asker_history_feedback_and_frequency(
    tmp_path: Path,
) -> None:
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    for user_id in ("u1", "u2"):
        store.bind_session(
            deployment_id="candidate",
            owner_id=f"rdms:candidate:{user_id}",
            conversation_id=f"conversation-{user_id}",
            native_session_id=f"native-{user_id}",
        )
        store.start_turn(
            trace_id=f"trace-{user_id}",
            deployment_id="candidate",
            owner_id=f"rdms:candidate:{user_id}",
            asker_name="张三" if user_id == "u1" else "李四",
            conversation_id=f"conversation-{user_id}",
            native_session_id=f"native-{user_id}",
            question="开发中心是干嘛的？",
        )
    store.finish_turn(
        trace_id="trace-u1",
        status="completed",
        answer="原生回答",
        references=(),
        native_message_id="message-1",
        native_request_id="request-1",
        truncated=False,
        finish_reason="stop",
    )
    store.save_feedback(
        trace_id="trace-u1",
        owner_id="rdms:candidate:u1",
        useful=False,
        reason_code=None,
        reason_detail="内容不准确",
        comment=None,
    )
    store.save_feedback_review(
        trace_id="trace-u1",
        status="in_review",
        note="等待核对原文",
        root_cause="来源错误",
        fix_reference="原生重解析任务 123",
        verification_references=["测试问答 456"],
        evaluation_candidate=True,
    )

    summary = store.ops_summary(deployment_id="candidate")
    assert summary == {
        "turns": 2,
        "users": 2,
        "completed": 1,
        "failed": 0,
        "negative_feedback": 1,
        "feedback_count": 1,
        "helpful_feedback": 0,
        "pending_feedback": 1,
    }
    frequent = store.frequent_questions(deployment_id="candidate")
    assert len(frequent) == 1
    assert frequent[0]["count"] == 2
    assert frequent[0]["user_count"] == 2
    assert frequent[0]["negative_feedback"] == 1
    assert store.count_traces(deployment_id="candidate") == 2
    assert (
        store.count_traces(deployment_id="candidate", feedback_only=True) == 1
    )
    assert store.count_traces(deployment_id="candidate", query="李四") == 1
    assert store.list_traces(deployment_id="candidate", query="u1")[0][
        "question"
    ] == "开发中心是干嘛的？"
    assert store.list_traces(deployment_id="candidate", limit=1, offset=1)

    overview = store.trace_overview(
        trace_id="trace-u1", deployment_id="candidate"
    )
    assert overview is not None
    assert overview["asker_id"] == "u1"
    assert overview["asker_name"] == "张三"
    assert overview["question"] == "开发中心是干嘛的？"
    assert overview["answer"] == "原生回答"
    assert overview["review"]["note"] == "等待核对原文"
    assert overview["review"]["root_cause"] == "来源错误"
    assert overview["review"]["evaluation_candidate"] is True
    assert overview["review"]["verification_references"] == ["测试问答 456"]
    assert (
        store.trace_overview(trace_id="trace-u1", deployment_id="other")
        is None
    )
