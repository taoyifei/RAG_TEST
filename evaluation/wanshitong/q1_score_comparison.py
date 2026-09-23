"""按人工标注计算受控对照的回答质量与复核误判，拒答不算答对。"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from rag_app.core.models.common import FrozenModel

_CELLS = ("A", "B", "C", "D")


class CellGrade(FrozenModel):
    """人工检查来源与答案后给出的最小质量标注。"""

    cell: Literal["A", "B", "C", "D"]
    answer_published: bool
    correct_complete: bool
    published_accurate: bool
    qualifier_error: bool

    @model_validator(mode="after")
    def _validate_grade(self) -> CellGrade:
        if self.correct_complete and (
            not self.answer_published
            or not self.published_accurate
            or self.qualifier_error
        ):
            raise ValueError("完整答对必须已发布且准确、限定无误。")
        if not self.answer_published and (
            self.published_accurate or self.qualifier_error
        ):
            raise ValueError("未发布时不能标注发布准确性或限定错误。")
        return self


class CaseGrade(FrozenModel):
    """一个事实族中本次固定问题的四格人工判分。"""

    case_id: str = Field(min_length=1)
    fact_family: str = Field(min_length=1)
    truth_basis: Literal["VERIFIED", "PROVISIONAL"]
    answerable: bool
    retrieved_evidence_sufficient: bool
    cells: tuple[CellGrade, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def _validate_cells(self) -> CaseGrade:
        if tuple(item.cell for item in self.cells) != _CELLS:
            raise ValueError("必须按 A/B/C/D 一次提供全部人工判分。")
        if not self.answerable and any(
            item.correct_complete for item in self.cells
        ):
            raise ValueError("无依据控制题不能标为完整答对。")
        return self


def _ratio(numerator: int, denominator: int) -> float | None:
    """分母为零时保持未观察，不冒充 0% 或 100%。"""
    return round(numerator / denominator, 4) if denominator else None


def score(grades: tuple[CaseGrade, ...]) -> dict[str, object]:
    """逐格统计完整答对、发布准确、误拒及限定错误。"""
    if not grades or len({grade.case_id for grade in grades}) != len(grades):
        raise ValueError("题集不能为空，case_id 不能重复。")
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for grade in grades:
        for cell in grade.cells:
            item = totals[cell.cell]
            item["case_count"] += 1
            item["answerable_count"] += int(grade.answerable)
            item["published_count"] += int(cell.answer_published)
            item["complete_correct_count"] += int(cell.correct_complete)
            item["accurate_published_count"] += int(
                cell.answer_published and cell.published_accurate
            )
            item["false_refusal_count"] += int(
                grade.answerable and not cell.answer_published
            )
            item["qualifier_error_count"] += int(cell.qualifier_error)
            item["unsupported_publication_count"] += int(
                not grade.answerable and cell.answer_published
            )
            item["limited_or_incomplete_count"] += int(
                grade.answerable
                and cell.answer_published
                and not cell.correct_complete
            )
    return {
        "case_count": len(grades),
        "truth_basis_counts": {
            basis: sum(item.truth_basis == basis for item in grades)
            for basis in ("VERIFIED", "PROVISIONAL")
        },
        "fact_family_count": len({grade.fact_family for grade in grades}),
        "cells": {
            cell: {
                **dict(totals[cell]),
                "complete_correct_rate": _ratio(
                    totals[cell]["complete_correct_count"],
                    totals[cell]["answerable_count"],
                ),
                "answered_accuracy_rate": _ratio(
                    totals[cell]["accurate_published_count"],
                    totals[cell]["published_count"],
                ),
                "false_refusal_rate": _ratio(
                    totals[cell]["false_refusal_count"],
                    totals[cell]["answerable_count"],
                ),
            }
            for cell in _CELLS
        },
    }


def main() -> None:
    """只读取人工判分，不按答案字符串推断正确性。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grades", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    raw = json.loads(arguments.grades.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("人工判分必须是 JSON 数组。")
    grades = tuple(CaseGrade.model_validate(item) for item in raw)
    result = score(grades)
    with arguments.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
