"""回答质量分母不把安全限答与拒答当作完整成功。"""

from __future__ import annotations

import pytest

from evaluation.wanshitong.q1_score_comparison import (
    CaseGrade,
    CellGrade,
    score,
)


def _cell(
    name: str,
    *,
    published: bool,
    complete: bool = False,
    accurate: bool = False,
    qualifier_error: bool = False,
) -> CellGrade:
    return CellGrade(
        cell=name,
        answer_published=published,
        correct_complete=complete,
        published_accurate=accurate,
        qualifier_error=qualifier_error,
    )


def test_limited_answer_and_false_refusal_do_not_count_as_complete() -> None:
    grade = CaseGrade(
        case_id="synthetic-scope-question",
        fact_family="synthetic-time-relation",
        truth_basis="VERIFIED",
        answerable=True,
        retrieved_evidence_sufficient=True,
        cells=(
            _cell("A", published=True, complete=True, accurate=True),
            _cell("B", published=True, accurate=True),
            _cell("C", published=False),
            _cell("D", published=True, qualifier_error=True),
        ),
    )

    results = score((grade,))["cells"]

    assert results["A"]["complete_correct_rate"] == 1.0
    assert results["B"]["complete_correct_rate"] == 0.0
    assert results["B"]["limited_or_incomplete_count"] == 1
    assert results["C"]["false_refusal_count"] == 1
    assert results["D"]["qualifier_error_count"] == 1
    assert results["D"]["answered_accuracy_rate"] == 0.0


def test_unobserved_denominator_is_not_reported_as_zero() -> None:
    control = CaseGrade(
        case_id="synthetic-unsupported",
        fact_family="synthetic-control",
        truth_basis="VERIFIED",
        answerable=False,
        retrieved_evidence_sufficient=False,
        cells=tuple(
            _cell(name, published=False) for name in ("A", "B", "C", "D")
        ),
    )

    result = score((control,))["cells"]["A"]

    assert result["complete_correct_rate"] is None
    assert result["answered_accuracy_rate"] is None


def test_inconsistent_human_grade_is_rejected() -> None:
    with pytest.raises(ValueError, match="完整答对"):
        _cell("A", published=False, complete=True)
