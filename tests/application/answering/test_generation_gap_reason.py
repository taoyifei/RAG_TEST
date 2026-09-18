"""完整来源与模型输出遗漏分别计账，避免误报资料缺失。"""

from __future__ import annotations

from unittest.mock import Mock

from rag_app.core.models import EvidenceItem
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import AtomAnswerShape, AtomStatus
from tests.application.answering.test_natural_grounded_answer import (
    _answer,
    _claim,
    _draft,
    _evidence,
    _matrix,
    _plan,
)


def _complete_group() -> tuple[EvidenceItem, ...]:
    texts = ("甲类工作如下：", "1.核对材料。", "2.归档记录。")
    items = _evidence(*texts)
    return tuple(
        item.model_copy(
            update={
                "chunk_id": f"chunk_{index:032x}",
                "metadata": freeze_json_object(
                    {
                        "evidence_group_id": "egrp_" + "0" * 31 + "1",
                        "group_complete": True,
                        "group_member_count": 3,
                        "group_member_index": index,
                    }
                ),
            }
        )
        for index, item in enumerate(items, 1)
    )


def test_complete_group_omission_is_generation_gap_after_one_repair() -> None:
    evidence = _complete_group()
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("甲类工作", shape=AtomAnswerShape.ENUMERATION)
    matrix = _matrix(
        plan,
        ((AtomStatus.SUPPORTED, tuple(item.support_id for item in evidence)),),
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "1.核对材料。",
                "A1",
                by_text["1.核对材料。"],
            ),
        ),
        plan,
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.atom_coverage == (("A1", "PARTIAL"),)
    assert outcome.missing_atom_reasons == (("A1", "GENERATION_INCOMPLETE"),)
    assert outcome.generation_gap_count == 1
    assert outcome.repair_calls == 1
    assert outcome.answer is not None
    assert "已检索到相关资料，但本次未能完整组织全部内容" in outcome.answer
    assert "现有资料没有明确说明" not in outcome.answer


def test_complete_group_all_members_closes_without_repair() -> None:
    evidence = _complete_group()
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("甲类工作", shape=AtomAnswerShape.ENUMERATION)
    matrix = _matrix(
        plan,
        ((AtomStatus.SUPPORTED, tuple(item.support_id for item in evidence)),),
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "1.核对材料。",
                "A1",
                by_text["1.核对材料。"],
            ),
            _claim(
                "C2",
                "2.归档记录。",
                "A1",
                by_text["2.归档记录。"],
            ),
        ),
        plan,
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.missing_atom_reasons == ()
    assert outcome.repair_calls == 0
    assert outcome.answer is not None
    assert "完整列表" not in outcome.answer


def test_scalar_partial_certificate_can_close_without_list_disclaimer() -> None:
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门", shape=AtomAnswerShape.DURATION)
    matrix = _matrix(plan, ((AtomStatus.PARTIAL, ("S1",)),))
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", "甲部门保存记录 14 天。", "A1", "S1"),), plan
    )

    outcome = _answer(generator, evidence, plan, matrix)

    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.answer == "甲部门保存记录 14 天。 [S1]"
    assert "完整列表" not in outcome.answer
    assert outcome.false_limited_detected
