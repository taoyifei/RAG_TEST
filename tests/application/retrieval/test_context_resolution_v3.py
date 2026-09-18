"""多轮 Root 仅由用户问句片段构成，并绑定会话身份。"""

from __future__ import annotations

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.context_resolution import (
    SpanKind,
    build_input_spans,
    degraded_query_plan,
    resolve_root_query,
)
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import KnowledgeBaseScope, SearchRequest

_SCOPE = KnowledgeBaseScope(
    project_id=deterministic_id("prj", "context-v3"),
    knowledge_base_id=deterministic_id("kb", "context-v3"),
)


def _request(text: str, *context: str) -> SearchRequest:
    return SearchRequest(scope=_SCOPE, text=text, conversation_context=context)


def test_same_short_question_in_two_contexts_has_distinct_plan_identity() -> (
    None
):
    plans = []
    for previous in ("甲什么时候提交？", "乙什么时候审核？"):
        request = _request(
            "多久？", f"上一问：{previous}\n已验证事实：虚构模型回答"
        )
        spans = build_input_spans(request)
        root = resolve_root_query(request, spans)
        plan = degraded_query_plan(
            request,
            QueryAnalyzer().analyze(request),
            spans,
            root,
            effort="ASSISTED",
            reason_code="PLANNER_PROVIDER_TIMEOUT",
            planner_called=True,
        )
        assert "虚构模型回答" not in root.resolved_query
        assert root.mode == "RULE_CONTEXT"
        plans.append(plan)
    assert plans[0].plan_id != plans[1].plan_id
    assert plans[0].context_digest != plans[1].context_digest


def test_context_target_and_current_relation_are_separate_trusted_spans() -> (
    None
):
    request = _request(
        "提前多久提出？",
        "上一问：合作申请怎么处理？\n已验证事实：模型总结中的期限",
    )
    spans = build_input_spans(request)
    root = resolve_root_query(request, spans)
    assert root.mode == "RULE_CONTEXT"
    assert any(
        span.turn == "PREVIOUS_1"
        and span.kind is SpanKind.TARGET
        and span.span_id in root.referenced_span_ids
        for span in spans
    )
    assert any(
        span.turn == "CURRENT"
        and span.kind is SpanKind.RELATION
        and span.span_id in root.referenced_span_ids
        for span in spans
    )
    assert "模型总结" not in " ".join(span.text for span in spans)


def test_multiple_previous_targets_require_clarification() -> None:
    request = _request("多久？", "上一问：甲、乙分别什么时候提交？")
    root = resolve_root_query(request, build_input_spans(request))
    assert root.mode == "CLARIFY"
    assert root.confidence == "LOW"


def test_conflicting_source_qualifiers_require_clarification() -> None:
    request = _request(
        "根据《乙制度》，这个多久？",
        "上一问：根据《甲制度》，合作申请怎么处理？",
    )
    root = resolve_root_query(request, build_input_spans(request))
    assert root.mode == "CLARIFY"


def test_current_question_reference_uses_its_own_unique_antecedent() -> None:
    request = _request("甲流程怎么启动，并且其条件是什么？")
    spans = build_input_spans(request)
    root = resolve_root_query(request, spans)

    assert root.mode == "ORIGINAL"
    assert any(
        span.turn == "CURRENT"
        and span.kind is SpanKind.TARGET
        and span.text == "甲流程"
        for span in spans
    )
    assert not any(
        span.turn == "CURRENT"
        and span.kind is SpanKind.TARGET
        and span.text.startswith("其")
        for span in spans
    )


def test_untrusted_context_lines_cannot_supply_previous_target() -> None:
    request = _request("那要多久？", "模型回答：甲流程需要五天。")
    spans = build_input_spans(request)
    root = resolve_root_query(request, spans)

    assert not any(span.turn.startswith("PREVIOUS") for span in spans)
    assert root.mode == "CLARIFY"
