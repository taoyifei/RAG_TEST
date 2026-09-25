"""普通入口自然协议协商和管理员草稿隔离。"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from rag_app.application.answering.natural_answer import (
    NaturalAnswerResult,
    NaturalReference,
)
from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import QueryCancelled
from rag_app.core.models import KnowledgeBaseScope, SourceAnchor, SourceSpan
from rag_app.core.models.usage_audit import QueryAuditContext
from rag_app.query_executor import QueryExecutor
from rag_app.wanshitong.natural_stream import (
    NATURAL_PUBLIC_PROTOCOL,
    NATURAL_PUBLIC_PROTOCOL_V2,
    NaturalPublicStream,
    NaturalStreamExpiredError,
    NaturalStreamRegistry,
    public_natural_final,
    public_natural_references,
)
from rag_app.wanshitong.public_api import (
    PUBLIC_CAPABILITIES_PATH,
    PUBLIC_CHAT_CONTINUE_PATH,
    PUBLIC_CHAT_PATH,
    PUBLIC_CHAT_STOP_PATH,
)
from tests.wanshitong.support import build_public_harness


def _result(**updates: object) -> NaturalAnswerResult:
    baseline = NaturalAnswerResult(
        trace_id="trace_" + "a" * 32,
        engine_id="wk-standard-pc-v1",
        answer=None,
        draft="管理员可见的草稿[S9]",
        reason_code="CITATION_INVALID",
        citation_status="invalid",
        active_index_revision_id="irev_" + "b" * 32,
        index_fingerprint="sha256:" + "1" * 64,
        serving_fingerprint="sha256:" + "2" * 64,
        rerank_execution_mode="rerank",
        finish_reason="stop",
    )
    return baseline.model_copy(update=updates)


def test_public_projection_never_contains_administrator_draft() -> None:
    for result in (
        _result(),
        _result(reason_code="CITATION_MISSING", citation_status="missing"),
        _result(finish_reason="length"),
    ):
        projected = public_natural_final(result)
        assert projected["answer"] is None
        assert projected["published"] is False
        assert projected["citations"] == []
        assert "草稿" not in json.dumps(projected, ensure_ascii=False)
    assert public_natural_final(_result(finish_reason="length"))["status"] == (
        "TRUNCATED"
    )


def test_published_projection_omits_absent_optional_fields() -> None:
    """发布终态和引用不能把前端可选字符串编码成 null。"""
    reference = NaturalReference(
        alias="S1",
        document_id="doc_" + "1" * 32,
        document_version_id="dver_" + "2" * 32,
        document_title="询价流程.docx",
        chunk_ids=("chunk_" + "3" * 32,),
        source_spans=(),
        citation_basis="original",
        source_complete=True,
    )
    projected = public_natural_final(
        _result(
            answer="询价至少三家。[S1]",
            draft=None,
            reason_code="ANSWERED",
            citation_status="valid",
            references=(reference,),
        )
    )
    assert projected["published"] is True
    assert "user_message" not in projected
    citation = projected["citations"][0]
    assert "locator" not in citation
    assert "quote" not in citation


def test_reviewed_projection_fails_closed_and_preserves_table_locator() -> None:
    paths = (("表 2", "表头"), ("表 2", "第 4 行"))
    spans = tuple(
        SourceSpan(
            node_id="node_" + str(index) * 32,
            source_anchor=SourceAnchor(
                part_uri="/word/document.xml",
                story_kind="body",
                structural_path=path,
                ordinal=index,
            ),
            structural_path=path,
            chunk_start_char=index,
            chunk_end_char=index + 1,
            source_start_char=index,
            source_end_char=index + 1,
        )
        for index, path in enumerate(paths)
    )
    reference = NaturalReference(
        alias="S1",
        document_id="doc_" + "1" * 32,
        document_version_id="dver_" + "2" * 32,
        document_title="补助标准.docx",
        chunk_ids=("chunk_" + "3" * 32,),
        source_spans=spans,
        citation_basis="original",
        source_complete=True,
        excerpt="表头及第 4 行的准确摘录。",
        answer_unit_id="u1",
    )
    result = _result(
        answer="该档次为 4 万元。[S1]",
        draft=None,
        reason_code="GROUNDED_ANSWER",
        citation_status="valid",
        references=(reference,),
        publication_status="GROUNDED_ANSWER",
    )
    assert public_natural_final(result)["published"] is False
    reviewed = result.model_copy(
        update={
            "validation_level": "source_binding_and_automated_support_review"
        }
    )
    assert public_natural_final(reviewed)["published"] is True
    incomplete = reviewed.model_copy(update={"finish_reason": "length"})
    assert public_natural_final(incomplete)["status"] == "EXECUTION_ERROR"
    assert public_natural_final(incomplete)["answer"] is None
    citation = public_natural_references(reviewed.trace_id, (reference,))[0]
    assert citation["locator"] == "表 2 / 表头；表 2 / 第 4 行"
    assert citation["quote"] == reference.excerpt
    assert citation["answer_unit_id"] == "u1"


def _v2_stream() -> NaturalPublicStream:
    return NaturalPublicStream(
        runtime=cast(ProductRuntime, object()),
        executor=cast(QueryExecutor, object()),
        scope=KnowledgeBaseScope(
            project_id="prj_" + "1" * 32,
            knowledge_base_id="kb_" + "2" * 32,
        ),
        question="当前标准是什么？",
        conversation_id="conversation-a",
        owner_id="owner-a",
        trace_id="trace_" + "3" * 32,
        engine_id="wk-standard-pc-v1",
        audit_context=cast(QueryAuditContext, object()),
        authorization_guard=lambda: None,
        protocol=NATURAL_PUBLIC_PROTOCOL_V2,
    )


def _events(iterator: Iterator[bytes]) -> list[dict[str, object]]:
    return [
        json.loads(frame.split(b"data: ", 1)[1])
        for frame in iterator
        if frame.startswith(b"event:")
    ]


def test_v2_disconnect_replay_stop_and_bounded_event_window() -> None:
    stream = _v2_stream()
    stream._append_event("meta", {"conversation_id": stream.conversation_id})
    first_reader = stream.iterate_after(-1)
    assert next(first_reader).startswith(b"event: meta")
    first_reader.close()
    assert not stream.cancellation.is_cancelled()
    stream._on_delta("未审核草稿不得出现")
    stream._on_stage("retrieval")
    assert stream.cancel()
    assert not stream.cancel()
    assert [item["type"] for item in _events(stream.iterate_after(-1))] == [
        "meta",
        "stage",
        "cancelled",
    ]
    assert [item["sequence"] for item in _events(stream.iterate_after(0))] == [
        1,
        2,
    ]
    assert not stream.cancel()

    registry = NaturalStreamRegistry()
    registry.register(stream)
    assert (
        registry.get_owned(
            stream.trace_id,
            owner_id="owner-a",
            conversation_id="conversation-a",
            scope=stream.scope,
        )
        is stream
    )
    assert (
        registry.get_owned(
            stream.trace_id,
            owner_id="owner-b",
            conversation_id="conversation-a",
            scope=stream.scope,
        )
        is None
    )
    stream._finished_at = time.monotonic() - 601
    assert (
        registry.get_owned(
            stream.trace_id,
            owner_id="owner-a",
            conversation_id="conversation-a",
            scope=stream.scope,
        )
        is None
    )

    completed = _v2_stream()
    with completed._terminal_lock:
        completed._append_event(
            "final", {"status": "INSUFFICIENT_EVIDENCE", "answer": None}
        )
        completed._terminal = "FINAL"
    assert completed.cancel() is False
    assert [item["type"] for item in _events(completed.iterate_after(-1))] == [
        "final"
    ]

    overflow = _v2_stream()
    for _ in range(260):
        overflow._append_event("stage", {"stage": "retrieval"})
    with pytest.raises(NaturalStreamExpiredError):
        overflow.assert_replayable(-1)
    overflow.cancel()


def test_public_route_requires_explicit_natural_negotiation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_WANSHITONG_NATURAL_PUBLIC_ENABLED", "true")
    harness = build_public_harness(tmp_path, monkeypatch)
    try:
        capabilities = harness.client.get(PUBLIC_CAPABILITIES_PATH)
        assert capabilities.status_code == 200
        assert capabilities.json()["natural_stream_protocol"] == (
            NATURAL_PUBLIC_PROTOCOL
        )
        unsupported = harness.client.post(
            PUBLIC_CHAT_PATH,
            headers={
                **harness.headers,
                "X-Wanshitong-Stream-Protocol": "unknown-v1",
            },
            json={"query": "如何办理？", "conversation_id": "c1"},
        )
        assert unsupported.status_code == 406
        assert (
            harness.client.post(
                PUBLIC_CHAT_PATH,
                headers={
                    **harness.headers,
                    "X-Wanshitong-Stream-Protocol": NATURAL_PUBLIC_PROTOCOL_V2,
                },
                json={"query": "如何办理？", "conversation_id": "c1"},
            ).status_code
            == 409
        )

        def fake_run(stream: NaturalPublicStream) -> None:
            stream._on_delta("第一段")
            stream._put(
                "final",
                {
                    "status": "CITATION_MISSING",
                    "published": False,
                    "answer": None,
                    "user_message": "本次未形成可核对引用的答案。",
                    "citation_status": "missing",
                    "validation_level": "citation_binding_only",
                    "citations": [],
                },
            )
            stream._end()

        monkeypatch.setattr(NaturalPublicStream, "_run", fake_run)
        response = harness.client.post(
            PUBLIC_CHAT_PATH,
            headers={
                **harness.headers,
                "X-Wanshitong-Stream-Protocol": NATURAL_PUBLIC_PROTOCOL,
            },
            json={"query": "如何办理？", "conversation_id": "c1"},
        )
        assert response.status_code == 200
        frames = [
            json.loads(frame.split("data: ", 1)[1])
            for frame in response.text.split("\n\n")
            if frame.startswith("event:")
        ]
        assert [item["type"] for item in frames] == [
            "meta",
            "stage",
            "answer_delta",
            "final",
        ]
        assert [item["sequence"] for item in frames] == list(range(4))
        assert frames[2]["text"] == "第一段"
        assert frames[2]["provisional"] is True
        assert frames[-1]["answer"] is None
        assert "草稿" not in response.text
    finally:
        harness.close()


def test_v2_route_replays_same_run_without_restarting_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_WANSHITONG_NATURAL_PUBLIC_ENABLED", "true")
    monkeypatch.setenv("RAG_WANSHITONG_NATURAL_PUBLIC_V2_ENABLED", "true")
    harness = build_public_harness(tmp_path, monkeypatch)
    executions: list[bool] = []
    try:
        capabilities = harness.client.get(PUBLIC_CAPABILITIES_PATH)
        assert capabilities.json()["natural_stream_protocol"] == (
            NATURAL_PUBLIC_PROTOCOL_V2
        )

        def fake_run(stream: NaturalPublicStream) -> None:
            executions.append(stream.ignore_mentioned_sources)
            stream._on_delta("未经审查的草稿")
            stream._on_stage("retrieval")
            with stream._terminal_lock:
                stream._append_event(
                    "final",
                    {
                        "status": "SOURCE_CLARIFICATION",
                        "published": False,
                        "answer": None,
                        "user_message": "请明确资料后重试。",
                        "citations": [],
                    },
                )
                stream._terminal = "FINAL"
                stream._finished_at = time.monotonic()

        monkeypatch.setattr(NaturalPublicStream, "_run", fake_run)
        body = {
            "query": "《规范》的标准是什么？",
            "conversation_id": "conversation-a",
            "source_mode": "open",
        }
        response = harness.client.post(
            PUBLIC_CHAT_PATH,
            headers={
                **harness.headers,
                "X-Wanshitong-Stream-Protocol": NATURAL_PUBLIC_PROTOCOL_V2,
            },
            json=body,
        )
        assert response.status_code == 200
        trace_id = response.headers["X-Trace-Id"]
        events = [
            json.loads(frame.split("data: ", 1)[1])
            for frame in response.text.split("\n\n")
            if frame.startswith("event:")
        ]
        assert [event["type"] for event in events] == ["meta", "stage", "final"]
        assert [event["sequence"] for event in events] == [0, 1, 2]
        assert "草稿" not in response.text
        assert executions == [True]

        path = PUBLIC_CHAT_CONTINUE_PATH.format(trace_id=trace_id)
        replay = harness.client.get(
            path,
            headers=harness.headers,
            params={"conversation_id": "conversation-a", "last_sequence": 0},
        )
        assert replay.status_code == 200
        assert '"sequence": 0' not in replay.text
        assert '"sequence": 1' in replay.text
        assert '"sequence": 2' in replay.text
        assert executions == [True]
        assert (
            harness.client.get(
                path,
                headers=harness.headers,
                params={"conversation_id": "conversation-b"},
            ).status_code
            == 410
        )
        assert (
            harness.client.get(
                path,
                params={"conversation_id": "conversation-a"},
            ).status_code
            == 403
        )
        assert harness.client.post(
            PUBLIC_CHAT_STOP_PATH.format(trace_id=trace_id),
            headers=harness.headers,
            json={"conversation_id": "conversation-a"},
        ).json() == {"cancelled": False}
        assert (
            harness.client.post(
                PUBLIC_CHAT_PATH,
                headers={
                    **harness.headers,
                    "X-Wanshitong-Stream-Protocol": NATURAL_PUBLIC_PROTOCOL,
                },
                json=body,
            ).status_code
            == 422
        )
    finally:
        harness.close()


def test_slow_reader_backpressure_releases_on_cancel() -> None:
    stream = NaturalPublicStream(
        runtime=cast(ProductRuntime, object()),
        executor=cast(QueryExecutor, object()),
        scope=KnowledgeBaseScope(
            project_id="prj_" + "1" * 32,
            knowledge_base_id="kb_" + "2" * 32,
        ),
        question="是否可取消？",
        conversation_id="slow-reader",
        owner_id="owner-a",
        trace_id="trace_" + "3" * 32,
        engine_id="wk-standard-pc-v1",
        audit_context=cast(QueryAuditContext, object()),
        authorization_guard=lambda: None,
    )
    for _ in range(stream.messages.maxsize):
        stream._put("answer_delta", {"text": "x", "provisional": True})
    started = threading.Event()
    failures: list[BaseException] = []

    def blocked_producer() -> None:
        started.set()
        try:
            stream._put("answer_delta", {"text": "y", "provisional": True})
        except QueryCancelled as error:
            failures.append(error)

    worker = threading.Thread(target=blocked_producer)
    worker.start()
    assert started.wait(timeout=1)
    assert worker.is_alive()
    stream.cancel()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(failures) == 1


def test_natural_stop_registry_checks_owner_and_conversation() -> None:
    stream = NaturalPublicStream(
        runtime=cast(ProductRuntime, object()),
        executor=cast(QueryExecutor, object()),
        scope=KnowledgeBaseScope(
            project_id="prj_" + "1" * 32,
            knowledge_base_id="kb_" + "2" * 32,
        ),
        question="如何停止？",
        conversation_id="conversation-a",
        owner_id="owner-a",
        trace_id="trace_" + "3" * 32,
        engine_id="wk-standard-pc-v1",
        audit_context=cast(QueryAuditContext, object()),
        authorization_guard=lambda: None,
    )
    registry = NaturalStreamRegistry()
    registry.register(stream)
    assert not registry.cancel_owned(
        stream.trace_id, owner_id="owner-b", conversation_id="conversation-a"
    )
    assert not registry.cancel_owned(
        stream.trace_id, owner_id="owner-a", conversation_id="conversation-b"
    )
    assert not stream.cancellation.is_cancelled()
    assert registry.cancel_owned(
        stream.trace_id, owner_id="owner-a", conversation_id="conversation-a"
    )
    assert stream.cancellation.is_cancelled()
    assert not registry.cancel_owned(
        stream.trace_id, owner_id="owner-a", conversation_id="conversation-a"
    )
    registry.discard(stream)


def test_public_stop_route_requires_session_and_csrf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_WANSHITONG_NATURAL_PUBLIC_ENABLED", "true")
    harness = build_public_harness(tmp_path, monkeypatch)
    try:
        path = PUBLIC_CHAT_STOP_PATH.format(trace_id="trace_" + "a" * 32)
        body = {"conversation_id": "conversation-a"}
        assert harness.client.post(path, json=body).status_code == 403
        assert (
            harness.client.post(
                path,
                headers={"X-CSRF-Token": "invalid"},
                json=body,
            ).status_code
            == 403
        )
        stopped = harness.client.post(path, headers=harness.headers, json=body)
        assert stopped.status_code == 200
        assert stopped.json() == {"cancelled": False}
    finally:
        harness.close()
