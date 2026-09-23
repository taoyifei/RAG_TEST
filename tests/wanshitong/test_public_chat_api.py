"""WB-03 公共路由直接委托 Universal 问答链的合同。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from rag_app.api.p09_stream import P09AnswerStream
from rag_app.core.models import (
    AnswerClaim,
    AnswerStreamClaimEvent,
    AnswerStreamFinalEvent,
    AnswerStreamMetaEvent,
    AnswerStreamStageEvent,
    ClaimSupport,
    KnowledgeBaseScope,
)
from rag_app.core.ports import CancellationPort
from rag_app.tracing import TraceMode
from rag_app.wanshitong.public_api import (
    PUBLIC_CAPABILITIES_PATH,
    PUBLIC_CHAT_PATH,
    PUBLIC_FEEDBACK_PATH,
    PUBLIC_SESSION_PATH,
)
from tests.wanshitong.support import (
    PublicHarness,
    public_principal,
    synthetic_answer,
)


def _events(response_text: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for frame in response_text.split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        lines = frame.splitlines()
        events.append(
            (
                lines[0].removeprefix("event: "),
                json.loads(lines[1].removeprefix("data: ")),
            )
        )
    return events


def test_capabilities_publish_only_fixed_public_policy(
    public_harness: PublicHarness,
) -> None:
    response = public_harness.client.get(PUBLIC_CAPABILITIES_PATH)
    assert response.status_code == 200
    assert response.json() == {
        "mode": "wanshitong",
        "stream": True,
        "stream_protocol": "wanshitong-public-sse-v1",
        "trace_mode": "SAFE",
        "history_mode": "full",
        "document_visibility": "all_internal",
        "conversation_delete": True,
        "feedback": True,
        "feedback_details": True,
        "request_usage_context": True,
        "shortcuts": [],
    }


def test_actual_public_error_stream_uses_shared_history_and_safe_trace(
    public_harness: PublicHarness,
) -> None:
    response = public_harness.client.post(
        PUBLIC_CHAT_PATH,
        headers=public_harness.headers,
        json={"query": "尚未建索引时如何办理？"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, no-transform"
    events = _events(response.text)
    assert [name for name, _ in events] == ["meta", "stage", "error"]
    trace_id = str(events[0][1]["trace_id"])
    binding = public_harness.scope_service.binding()
    owner = public_principal(
        public_harness,
        public_harness.client,
        public_harness.csrf,
    ).owner_id

    history = public_harness.product.runtime.history.detail(
        trace_id,
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
        owner_id=owner,
    )
    assert history["status"] == "FAILED"
    assert history["body_saved"] is True
    public_harness.product.runtime.traces.flush()
    trace = public_harness.product.runtime.traces.detail(trace_id)
    assert trace.trace.mode is TraceMode.SAFE
    assert trace.trace.status.value == "FAILED"


def test_chat_delegates_fixed_scope_owner_and_policies_to_p09_stream(
    public_harness: PublicHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    prepared: list[tuple[str, TraceMode]] = []
    original_prepare = public_harness.product.runtime.p09.prepare_trace
    original_stream_post_init = P09AnswerStream.__post_init__

    def _capture_stream_limits(stream: P09AnswerStream) -> None:
        captured.update(
            first_content_seconds=stream.first_content_seconds,
            idle_seconds=stream.idle_seconds,
            total_seconds=stream.total_seconds,
        )
        original_stream_post_init(stream)

    def _prepare(trace_id: str, mode: TraceMode) -> None:
        prepared.append((trace_id, mode))
        if original_prepare is not None:
            original_prepare(trace_id, mode)

    def _answer_stream(
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        emit: Callable[[object], None],
        cancellation: CancellationPort,
        **options: object,
    ) -> object:
        del cancellation
        trace_id = str(options["trace_id"])
        captured.update(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            text=text,
            limit=options["limit"],
            include_related_content=options["include_related_content"],
            history_mode=options["history_mode"],
            owner_id=options["owner_id"],
            trace_id=trace_id,
            conversation_id=options["conversation_id"],
        )
        emit(
            AnswerStreamMetaEvent(
                trace_id=trace_id,
                sequence=0,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
        )
        emit(
            AnswerStreamStageEvent.create(
                trace_id=trace_id,
                sequence=1,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                stage="snapshot",
                attributes={
                    "active_index_revision_id": "irev_" + "9" * 32,
                    "serving_fingerprint": "sha256:" + "8" * 64,
                },
            )
        )
        emit(
            AnswerStreamClaimEvent(
                trace_id=trace_id,
                sequence=2,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                claim_index=0,
                claim=AnswerClaim(
                    text="材料应在五个工作日内核验。",
                    supports=(
                        ClaimSupport(
                            support_id="S1",
                            quote="五个工作日内完成核验",
                        ),
                    ),
                ),
                active_index_revision_id="irev_" + "9" * 32,
            )
        )
        result = synthetic_answer(trace_id)
        emit(
            AnswerStreamFinalEvent(
                trace_id=trace_id,
                sequence=3,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                result=result,
            )
        )
        return result

    monkeypatch.setattr(
        public_harness.product.runtime.p09, "prepare_trace", _prepare
    )
    monkeypatch.setattr(
        P09AnswerStream, "__post_init__", _capture_stream_limits
    )
    monkeypatch.setattr(
        public_harness.product.runtime.sdk, "answer_stream", _answer_stream
    )

    response = public_harness.client.post(
        PUBLIC_CHAT_PATH,
        headers=public_harness.headers,
        json={"query": "材料多久核验？", "conversation_id": "case-1"},
    )
    assert response.status_code == 200
    binding = public_harness.scope_service.binding()
    assert captured["project_id"] == binding.project_id
    assert captured["knowledge_base_id"] == binding.knowledge_base_id
    assert captured["history_mode"] == "full"
    assert captured["include_related_content"] is False
    assert captured["limit"] == 10
    assert captured["conversation_id"] == "case-1"
    assert captured["first_content_seconds"] == 120.0
    assert captured["idle_seconds"] == 120.0
    assert captured["total_seconds"] == 180.0
    assert str(captured["owner_id"]).startswith("wanshitong-public:")
    assert captured["owner_id"] != "local-admin"
    assert prepared == [(captured["trace_id"], TraceMode.SAFE)]

    events = _events(response.text)
    assert [name for name, _ in events] == [
        "meta",
        "stage",
        "claim",
        "final",
    ]
    forbidden = (
        "project_id",
        "knowledge_base_id",
        "owner_id",
        "active_index_revision_id",
        "index_revision_id",
        "chunk_id",
        "score",
        "vector",
        "provider",
        "serving_fingerprint",
    )
    serialized = json.dumps(events, ensure_ascii=False)
    assert all(field not in serialized for field in forbidden)
    claim = events[2][1]
    assert claim["claim"] == {"text": "材料应在五个工作日内核验。"}
    assert "supports" not in claim
    final = events[-1][1]
    assert final["answer"] == "办理材料应在五个工作日内完成核验。"
    assert final["citations"] == [
        {
            "document_name": "湾事通办事指南",
            "department": "政务服务部",
            "department_name": "政务服务部",
            "category_path": ["办事服务", "材料办理"],
            "document_title": "湾事通办事指南",
            "locator": "申请指南 > 材料核验",
            "quote": "办理材料应在五个工作日内完成核验。",
            "source_relative_path": ("政务服务部/办事服务/湾事通办事指南.docx"),
        }
    ]
    assert all(
        payload["protocol"] == "wanshitong-public-sse-v1"
        for _, payload in events
    )


def test_cross_owner_conversation_delete_and_feedback_fail(
    public_harness: PublicHarness,
) -> None:
    binding = public_harness.scope_service.binding()
    scope = KnowledgeBaseScope(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
    )
    owner = public_principal(
        public_harness,
        public_harness.client,
        public_harness.csrf,
    ).owner_id
    now = datetime.now(UTC)
    with public_harness.product.runtime.connections.transaction(
        write=True
    ) as connection:
        connection.execute(
            "INSERT INTO product_conversations(owner_id, project_id, "
            "knowledge_base_id, conversation_id, next_ordinal, "
            "content_chars, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)",
            (
                owner,
                scope.project_id,
                scope.knowledge_base_id,
                "private-conversation",
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(hours=1)).isoformat(),
            ),
        )
    trace_id = "trace_" + "7" * 32
    result = synthetic_answer(trace_id, include_evidence=False)
    public_harness.product.runtime.history.start(
        trace_id,
        scope,
        "反馈测试",
        owner_id=owner,
        save_body=True,
    )
    public_harness.product.runtime.history.finish(
        trace_id,
        result=result,
        error=None,
        cancelled=False,
    )

    with TestClient(public_harness.app) as other:
        issued = other.post(PUBLIC_SESSION_PATH)
        issued.raise_for_status()
        other_headers = {"X-CSRF-Token": str(issued.json()["csrf_token"])}
        denied_delete = other.delete(
            "/api/public/conversations/private-conversation",
            headers=other_headers,
        )
        denied_feedback = other.post(
            PUBLIC_FEEDBACK_PATH,
            headers=other_headers,
            json={"trace_id": trace_id, "useful": False},
        )

    assert denied_delete.status_code == 404
    assert denied_feedback.status_code == 403
    own_delete = public_harness.client.delete(
        "/api/public/conversations/private-conversation",
        headers=public_harness.headers,
    )
    own_feedback = public_harness.client.post(
        PUBLIC_FEEDBACK_PATH,
        headers=public_harness.headers,
        json={"trace_id": trace_id, "useful": True},
    )
    assert own_delete.status_code == 200
    assert own_delete.json() == {
        "conversation_id": "private-conversation",
        "deleted": True,
        "deleted_turns": 0,
    }
    assert own_feedback.status_code == 200
    assert own_feedback.json()["trace_id"] == trace_id
    assert "project_id" not in own_feedback.json()
    assert "knowledge_base_id" not in own_feedback.json()
    assert "owner_id" not in own_feedback.json()
