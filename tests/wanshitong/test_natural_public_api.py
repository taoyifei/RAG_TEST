"""普通入口自然协议协商和管理员草稿隔离。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import cast

import pytest

from rag_app.application.answering.natural_answer import NaturalAnswerResult
from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import QueryCancelled
from rag_app.core.models import KnowledgeBaseScope
from rag_app.core.models.usage_audit import QueryAuditContext
from rag_app.query_executor import QueryExecutor
from rag_app.wanshitong.natural_stream import (
    NATURAL_PUBLIC_PROTOCOL,
    NaturalPublicStream,
    public_natural_final,
)
from rag_app.wanshitong.public_api import (
    PUBLIC_CAPABILITIES_PATH,
    PUBLIC_CHAT_PATH,
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
