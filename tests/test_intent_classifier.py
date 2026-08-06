from __future__ import annotations

import json
from types import SimpleNamespace

from rag_app.clients.intent_classifier import IntentClassifier
from rag_app.generation.question_profile import (
    PrimaryOperation,
    QuestionProfile,
    RequestedSlot,
    RouteSource,
    extract_structural_signals,
)


class _FakeLlm:
    """记录分类调用的最小 LLM 替身。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def generate(self, messages: object, **kwargs: object) -> object:
        self.calls.append({"messages": messages, **kwargs})
        return SimpleNamespace(content=self.content, call=None)


def test_classifier_returns_stably_ordered_slots_and_bounded_request() -> None:
    fake = _FakeLlm(
        json.dumps(
            {
                "primary_operation": "LIST",
                "secondary_operations": [],
                "requested_slots": ["ACTOR"],
                "confidence": 0.9,
            }
        )
    )
    classifier = IntentClassifier(fake, max_output_tokens=96)  # type: ignore[arg-type]

    result = classifier.classify(
        "输出什么报告",
        semantic_profile=_uncertain_profile(),
        structural_signals=extract_structural_signals("输出什么报告"),
    )

    assert result.profile.primary_operation is PrimaryOperation.LIST
    assert result.profile.requested_slots == (
        RequestedSlot.ACTOR,
        RequestedSlot.DELIVERABLE,
    )
    assert result.profile.route_source is RouteSource.LLM
    assert len(fake.calls) == 1
    request = fake.calls[0]
    assert request["max_output_tokens"] == 96
    messages = request["messages"]
    assert isinstance(messages, tuple)
    payload = json.loads(messages[1].content)
    assert set(payload) == {
        "binary_choice_candidate",
        "operations",
        "requested_slots",
        "resolved_query",
        "semantic_top3",
    }
    assert "evidence" not in json.dumps(payload)


def test_classifier_schema_failure_returns_general_without_refusal() -> None:
    fake = _FakeLlm('{"primary_operation":"NOT_AN_OPERATION"}')
    classifier = IntentClassifier(fake, max_output_tokens=96)  # type: ignore[arg-type]

    result = classifier.classify(
        "如何处理",
        semantic_profile=_uncertain_profile(),
        structural_signals=extract_structural_signals("如何处理"),
    )

    assert result.profile.primary_operation is PrimaryOperation.GENERAL
    assert result.profile.reason_code == "LLM_FALLBACK_UNAVAILABLE"
    assert result.call is None


def _uncertain_profile() -> QuestionProfile:
    return QuestionProfile(
        primary_operation=PrimaryOperation.GENERAL,
        secondary_operations=(),
        requested_slots=(),
        confidence=0.4,
        margin=0.01,
        route_source=RouteSource.GENERAL,
        scores=(
            (PrimaryOperation.DECISION, 0.4),
            (PrimaryOperation.COMPARE, 0.39),
        ),
        fallback_used=True,
        reason_code="SEMANTIC_UNCERTAIN",
    )
