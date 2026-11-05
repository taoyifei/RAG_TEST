"""回答来源标识只依据可核对的检索及引用，不改变回答。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from wanshitong_gateway.weknora.answer_origin import (
    MODEL_GENERATED_NOTICE,
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
            "knowledge",
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
    """引用可以晚于正文到达，最终标识必须回到有资料来源。"""
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
    assert wire[-1]["answer_origin"] == "knowledge"
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
        (3, [{"document_name": "原引用.docx"}], "knowledge"),
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


@pytest.mark.parametrize("count", [0, 3])
def test_no_basis_notice_overrides_candidates(
    count: int,
) -> None:
    """模型明确说明没有依据时，即使检索到了候选也提示模型生成。"""
    references = [{"document_name": "参考资料.docx"}]
    answer = MODEL_GENERATED_NOTICE + "\n\n资料没有提供所问的信息。"
    assert (
        answer_origin_from_events(
            [{"payload": {"native": _result(count)}}],
            citations=references,
            status="completed",
            answer=answer,
        )
        == "model_unreferenced"
    )


def test_normal_answer_keeps_native_knowledge_origin() -> None:
    """普通业务回答不因缺少正文标号而被改标为模型自行生成。"""
    assert (
        answer_origin_from_events(
            [{"payload": {"native": _result(3)}}],
            citations=[{"document_name": "园区服务手册.docx"}],
            status="completed",
            answer="员工食堂位于服务中心A座1楼。",
        )
        == "knowledge"
    )
