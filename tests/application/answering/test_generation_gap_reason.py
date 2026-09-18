"""完整来源与模型输出遗漏分别计账，避免误报资料缺失。"""

from __future__ import annotations

from unittest.mock import Mock

from rag_app.core.models import EvidenceItem
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupportMatrix,
    QueryPlan,
)
from rag_app.core.models.retrieval import ClaimSupport, NaturalClaim
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
                        "evidence_group_type": "LIST_GROUP",
                        "group_complete": True,
                        "group_member_count": 3,
                        "group_member_index": index,
                    }
                ),
            }
        )
        for index, item in enumerate(items, 1)
    )


def _group_claim(
    item_text: str,
    by_text: dict[str, str],
) -> NaturalClaim:
    """同一完整结构组的事实同时引用关系导语和原句。"""
    intro = "甲类工作如下："
    claim = _claim("C1", f"{intro}{item_text}", "A1", by_text[item_text])
    return claim.model_copy(
        update={
            "supports": (
                ClaimSupport(support_id=by_text[intro], quote=intro),
                ClaimSupport(support_id=by_text[item_text], quote=item_text),
            )
        }
    )


def _certified_matrix(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
) -> AtomSupportMatrix:
    """模拟检索阶段已经核对完整组关系的证书。"""
    matrix = _matrix(
        plan,
        ((AtomStatus.SUPPORTED, tuple(item.support_id for item in evidence)),),
    )
    group_id = dict(evidence[0].metadata)["evidence_group_id"]
    support = matrix.atoms[0].model_copy(
        update={"relation_certified_group_ids": (group_id,)}
    )
    return matrix.model_copy(update={"atoms": (support,)})


def test_complete_group_omission_is_generation_gap_after_one_repair() -> None:
    evidence = _complete_group()
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("甲类工作", shape=AtomAnswerShape.ENUMERATION)
    matrix = _certified_matrix(plan, evidence)
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _group_claim("1.核对材料。", by_text),
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
    matrix = _certified_matrix(plan, evidence)
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _group_claim("1.核对材料。", by_text),
            _group_claim("2.归档记录。", by_text),
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
