from __future__ import annotations

import math

import pytest

from rag_app.generation.question_profile import (
    PrimaryOperation,
    QuestionProfile,
    RequestedSlot,
    RouteSource,
    extract_structural_signals,
)


def test_profile_rejects_invalid_secondary_and_scores() -> None:
    with pytest.raises(ValueError, match="secondary_operations"):
        QuestionProfile(
            primary_operation=PrimaryOperation.LIST,
            secondary_operations=(PrimaryOperation.LIST,),
            requested_slots=(),
            confidence=1.0,
            margin=1.0,
            route_source=RouteSource.LEGACY,
            scores=((PrimaryOperation.LIST, 1.0),),
            fallback_used=False,
            reason_code="TEST",
        )
    with pytest.raises(ValueError, match="scores 必须按分数降序"):
        QuestionProfile(
            primary_operation=PrimaryOperation.LIST,
            secondary_operations=(),
            requested_slots=(),
            confidence=1.0,
            margin=1.0,
            route_source=RouteSource.LEGACY,
            scores=(
                (PrimaryOperation.LIST, 0.2),
                (PrimaryOperation.GENERAL, 0.8),
            ),
            fallback_used=False,
            reason_code="TEST",
        )


def test_structural_signals_extract_compound_slots_and_choice() -> None:
    signals = extract_structural_signals("由谁输出什么报告，快验还是产品开发")

    assert signals.requested_slots == (
        RequestedSlot.ACTOR,
        RequestedSlot.DELIVERABLE,
    )
    assert signals.binary_choice_candidate is True
    assert signals.binary_choice_left == "快验"
    assert signals.binary_choice_right == "产品开发"


@pytest.mark.parametrize(
    "question",
    (
        "为什么还是失败",
        "项目已经完成，但是报告怎么输出",
        "项目启动后还需要哪些材料",
    ),
)
def test_structural_signals_do_not_classify_bare_connectives(
    question: str,
) -> None:
    signals = extract_structural_signals(question)

    assert isinstance(signals.anchor_count, int)
    assert signals.binary_choice_candidate is False
    assert all(slot in RequestedSlot for slot in signals.requested_slots)


def test_question_profile_prompt_payload_hides_scores_and_reason() -> None:
    profile = QuestionProfile(
        primary_operation=PrimaryOperation.DECISION,
        secondary_operations=(PrimaryOperation.COMPARE,),
        requested_slots=(RequestedSlot.CONDITION,),
        confidence=0.8,
        margin=0.1,
        route_source=RouteSource.SEMANTIC,
        scores=(
            (PrimaryOperation.DECISION, 0.8),
            (PrimaryOperation.COMPARE, 0.7),
        ),
        fallback_used=False,
        reason_code="SEMANTIC_CONFIDENT",
    )

    assert profile.as_prompt_payload() == {
        "primary_operation": "DECISION",
        "secondary_operations": ["COMPARE"],
        "requested_slots": ["CONDITION"],
    }
    assert math.isfinite(profile.confidence)
