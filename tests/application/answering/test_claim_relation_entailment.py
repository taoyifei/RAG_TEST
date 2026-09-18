"""来源相关性不能替代 Claim 对象、关系与逻辑范围的直接证明。"""

from __future__ import annotations

from unittest.mock import Mock

from rag_app.core.models.query_plan import AtomStatus
from tests.application.answering.test_natural_grounded_answer import (
    _answer,
    _claim,
    _draft,
    _evidence,
    _matrix,
    _plan,
)


def _rejected(source: str, claim_text: str) -> tuple[str, ...]:
    evidence = _evidence(source)
    plan = _plan("甲部门")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1",)),))
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", claim_text, "A1", "S1"),), plan
    )

    result = _answer(generator, evidence, plan, matrix)

    assert result.answer is None
    assert generator.generate.call_count == 2
    return tuple(code for code, _count in result.claim_rejection_codes)


def test_related_annual_text_does_not_certify_cross_period_permission() -> None:
    codes = _rejected(
        "甲部门按年度集中申报。",
        "甲部门跨年仍可申报。",
    )

    assert "CLAIM_INFERENTIAL_LEAP" in codes


def test_claim_cannot_replace_action_with_related_action() -> None:
    codes = _rejected(
        "甲部门负责核对材料。",
        "甲部门负责批准材料。",
    )

    assert "CLAIM_RELATION_UNSUPPORTED" in codes


def test_claim_cannot_change_permission_to_obligation() -> None:
    codes = _rejected(
        "甲部门可以检查材料。",
        "甲部门必须检查材料。",
    )

    assert "CLAIM_MODALITY_MISMATCH" in codes


def test_claim_cannot_replace_the_applicability_condition() -> None:
    codes = _rejected(
        "甲部门在材料齐全时可以检查材料。",
        "甲部门在材料缺失时可以检查材料。",
    )

    assert "CLAIM_CONDITION_UNSUPPORTED" in codes


def test_claim_cannot_answer_for_another_explicit_subject() -> None:
    codes = _rejected(
        "甲部门负责核对材料；乙部门负责归档材料。",
        "乙部门负责归档材料。",
    )

    assert "CLAIM_TARGET_UNSUPPORTED" in codes


def test_claim_cannot_borrow_action_from_sibling_subject() -> None:
    codes = _rejected(
        "甲部门负责核对材料；乙部门负责批准材料。",
        "甲部门负责批准材料。",
    )

    assert "CLAIM_RELATION_UNSUPPORTED" in codes
