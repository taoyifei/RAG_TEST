"""回答来源标识只依据可核对的检索及引用，不改变回答。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from wanshitong_gateway.weknora.answer_origin import (
    answer_cites_references,
    answer_origin_from_events,
)
from wanshitong_gateway.weknora.stream import BridgeStream, NativeEvent


def _result(count: Any, *, success: bool = True) -> dict[str, Any]:  # noqa: ANN401 - 故意覆盖损坏的上游计数。
    return {
        "response_type": "tool_result",
        "data": {
            "tool_name": "knowledge_search",
            "success": success,
            "count": count,
        },
    }


@pytest.mark.parametrize(
    ("payloads", "citations", "status", "answer", "expected"),
    [
        ([_result(0)], [], "completed", "资料不足", "model_no_results"),
        ([], [], "completed", "加油", "model_unreferenced"),
        ([_result(3)], [], "completed", "回答", "model_unreferenced"),
        (
            [_result(3), _result(0)],
            [],
            "completed",
            "回答",
            "model_unreferenced",
        ),
        (
            [_result(0)],
            [{"document_name": "文件.docx"}],
            "completed",
            "回答",
            "model_unreferenced",
        ),
        ([_result(False)], [], "completed", "回答", "model_unreferenced"),
        ([_result(-1)], [], "completed", "回答", "model_unreferenced"),
        (
            [_result(0, success=False)],
            [],
            "completed",
            "回答",
            "model_unreferenced",
        ),
        ([_result(0)], [], "failed", "部分内容", "unknown"),
        ([_result(0)], [], "cancelled", "部分内容", "unknown"),
        ([_result(0)], [], "completed", " ", "unknown"),
    ],
)
def test_history_uses_retrieval_evidence_without_guessing(
    payloads: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    status: str,
    answer: str,
    expected: str,
) -> None:
    events = [{"payload": {"native": item}} for item in payloads]
    assert (
        answer_origin_from_events(
            events, citations=citations, status=status, answer=answer
        )
        == expected
    )


def test_stream_and_history_agree_for_no_results_and_late_references() -> None:
    """晚到的检索候选不会把未引用的回答变成知识库回答。"""
    bridge = BridgeStream(
        trace_id="trace-origin",
        session_id="session-origin",
        reference_mapper=lambda item: item,
    )
    wire: list[dict[str, Any]] = []
    for payload in [
        _result(0),
        {"response_type": "answer", "content": "第一段"},
        {
            "response_type": "references",
            "knowledge_references": [{"document_name": "补充引用.docx"}],
        },
        {"response_type": "complete"},
    ]:
        wire.extend(
            json.loads(frame.decode().split("data: ", 1)[1])
            for frame in bridge.accept(
                NativeEvent(event="message", payload=payload)
            )
        )
    assert wire[1]["answer_origin"] == "model_no_results"
    assert wire[-1]["answer_origin"] == "model_unreferenced"
    assert (
        answer_origin_from_events(
            [{"payload": item} for item in wire],
            citations=list(bridge.references),
            status="completed",
            answer=bridge.answer,
        )
        == wire[-1]["answer_origin"]
    )


def test_nested_native_retrieval_count_is_supported() -> None:
    payload = _result(0)
    del payload["data"]["count"]
    payload["data"]["data"] = {"count": 0}
    assert (
        answer_origin_from_events(
            [{"payload": {"native": payload}}],
            citations=[],
            status="completed",
            answer="回答",
        )
        == "model_no_results"
    )


@pytest.mark.parametrize(
    ("count", "references", "expected"),
    [
        (0, [], "model_no_results"),
        (3, [], "model_unreferenced"),
        (3, [{"document_name": "原引用.docx"}], "model_unreferenced"),
    ],
)
def test_recovery_keeps_prior_evidence_without_repeating_answer(
    count: int, references: list[dict[str, Any]], expected: str
) -> None:
    bridge = BridgeStream(
        trace_id="trace-origin",
        session_id="session-origin",
        reference_mapper=lambda item: item,
        initial_events=[{"payload": {"native": _result(count)}}],
        initial_references=references,
    )
    bridge.accept(
        NativeEvent(
            event="message",
            payload={"response_type": "answer", "content": "恢复后正文"},
        )
    )
    frames = bridge.accept(
        NativeEvent(event="message", payload={"response_type": "complete"})
    )
    final = json.loads(frames[0].decode().split("data: ", 1)[1])
    assert final["answer"] == "恢复后正文"
    assert final["answer_origin"] == expected
    assert final["citations"] == references


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('流程按要求提交。<kb chunk_id="chunk-a" kb_id="kb-a"/>', True),
        ('<kb chunk_id="other" kb_id="kb-a"/>', False),
        ('<kb chunk_id="child-a" kb_id="kb-a"/>', True),
        ('<kb chunk_id="chunk-a" kb_id="other-kb"/>', False),
        ('<kb doc="文件.docx"/>', False),
        ('`<kb chunk_id="chunk-a"/>`', False),
        ('```xml\n<kb chunk_id="chunk-a"/>\n```', False),
        ('~~~\n<kb chunk_id="chunk-a"/>\n~~~', False),
        ('```\n示例\n```\n真正来源<kb chunk_id="chunk-a"/>', True),
        ("![原图](resource://original-image)", True),
        ("![其他图](resource://other-image)", False),
        ("原图地址：resource://original-image", False),
        ("![未完成的图片](resource://original-image", False),
        ("`![图片示例](resource://original-image)`", False),
    ],
)
def test_source_ids_and_displayed_images_match_current_references(
    answer: str, expected: bool
) -> None:
    references = [
        {
            "document_name": "文件.docx",
            "native_chunk_id": "chunk-a",
            "native_reference": {
                "knowledge_base_id": "kb-a",
                "sub_chunk_id": ["child-a"],
                "image_info": '[{"url":"resource://original-image"}]',
            },
        }
    ]
    assert answer_cites_references(answer, references) is expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('有依据的回答。<kb chunk_id="chunk-a"/>', "knowledge"),
        ("没有这个问题的资料。", "model_unreferenced"),
    ],
)
def test_stream_and_history_use_the_same_actual_reference_rule(
    answer: str, expected: str
) -> None:
    references = [{"document_name": "文件.docx", "native_chunk_id": "chunk-a"}]
    bridge = BridgeStream(
        trace_id="trace-reference",
        session_id="session-reference",
        reference_mapper=lambda item: item,
    )
    payloads = [
        _result(3),
        {"response_type": "references", "knowledge_references": references},
        {"response_type": "answer", "content": answer},
        {"response_type": "complete"},
    ]
    frames = []
    for payload in payloads:
        frames.extend(
            bridge.accept(NativeEvent(event="message", payload=payload))
        )
    final = json.loads(frames[-1].decode().split("data: ", 1)[1])
    assert final["answer_origin"] == expected
    assert final["answer"] == answer
    assert final["citations"] == references
    assert (
        answer_origin_from_events(
            [{"payload": {"native": payload}} for payload in payloads],
            citations=references,
            status="completed",
            answer=answer,
        )
        == expected
    )
