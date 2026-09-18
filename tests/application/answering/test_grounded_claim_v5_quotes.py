"""生成准入与逐字 Quote 发布分离后的定向回归。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    GroundedOutcome,
    _safe_extractive_fallback,
)
from rag_app.application.retrieval.generation_evidence import (
    EvidenceAdmissionReason,
    EvidenceAdmissionStatus,
    GenerationEvidenceEntry,
    GenerationEvidencePack,
)
from rag_app.core.models import (
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    SourceSpanKind,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    QueryAtom,
    QueryPlan,
)
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _draft,
    _evidence,
    _matrix,
    _plan,
)


def _pack(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    *,
    atom_ids_by_support: dict[str, tuple[str, ...]] | None = None,
) -> GenerationEvidencePack:
    """用真实合成 SourceSpan 构造已通过硬性来源检查的包。"""
    mapping = atom_ids_by_support or {
        item.support_id: tuple(atom.atom_id for atom in plan.atoms)
        for item in evidence
    }
    entries = tuple(
        GenerationEvidenceEntry(
            support_id=item.support_id,
            evidence_item=item,
            source_group_id=None,
            linked_atom_ids=mapping[item.support_id],
            admission_status=EvidenceAdmissionStatus.ADMITTED,
            hard_reject_reasons=(),
            soft_signals=(EvidenceAdmissionReason.ACTIVE_CITABLE,),
            rerank_rank=index,
            source_order=index,
        )
        for index, item in enumerate(evidence, 1)
    )
    per_atom = tuple(
        (
            atom.atom_id,
            tuple(
                item.support_id
                for item in evidence
                if atom.atom_id in mapping[item.support_id]
            ),
        )
        for atom in plan.atoms
    )
    return GenerationEvidencePack(
        original_query=plan.original_query,
        resolved_root_query=plan.resolved_root_query,
        entries=entries,
        rejected_entries=(),
        per_atom_candidate_support_ids=per_atom,
        complete_group_ids=(),
        partial_group_ids=(),
        missing_atom_ids=tuple(atom_id for atom_id, ids in per_atom if not ids),
    )


def _answer_with_pack(
    generator: Mock,
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    matrix_statuses: tuple[tuple[AtomStatus, tuple[str, ...]], ...],
    *,
    pack: GenerationEvidencePack | None = None,
) -> GroundedOutcome:
    """执行真实应用回答链，矩阵可故意与准入结果不同。"""
    return GroundedAnsweringService(generator).answer(
        plan.standalone_query,
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=plan,
        atom_support_matrix=_matrix(plan, matrix_statuses),
        generation_evidence_pack=pack or _pack(plan, evidence),
    )


def test_soft_atom_mismatch_still_reaches_generation_and_publishes_quote() -> (
    None
):
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门")
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", "甲部门保存记录 14 天。", "A1", "S1"),), plan
    )

    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )

    assert generator.generate.call_count == 1
    assert outcome.answer == "甲部门保存记录 14 天。 [S1]"
    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.accepted_claim_count == 1


def test_certified_single_fact_uses_direct_extract_without_model() -> None:
    source = _evidence("甲部门保存记录 14 天。")[0]
    certified = source.model_copy(
        update={
            "source_kind": SourceSpanKind.ORIGINAL_TEXT,
            "metadata": freeze_json_object(
                {
                    **dict(source.metadata),
                    "answer_support": {
                        "status": "SUPPORTED",
                        "support_reason": "SOURCE_RELATION_AND_VALUE",
                        "query_target": "甲部门",
                        "requested_relation_or_attribute": "规定",
                    },
                }
            ),
        }
    )
    plan = _plan("甲部门")
    generator = Mock()

    outcome = _answer_with_pack(
        generator,
        plan,
        (certified,),
        ((AtomStatus.SUPPORTED, ("S1",)),),
    )

    assert outcome.mode == "extractive"
    assert outcome.answer == "根据资料：甲部门保存记录 14 天。 [S1]"
    generator.generate.assert_not_called()


def test_invalid_model_quote_cannot_publish_claim_and_uses_excerpt() -> None:
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门")
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "甲部门保存记录 14 天。",
                "A1",
                "S1",
                "甲部门保存记录 15 天。",
            ),
        ),
        plan,
    )

    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )

    assert outcome.mode == "extractive_fallback"
    assert outcome.accepted_claim_count == 0
    assert outcome.claim_rejection_codes == (("CLAIM_QUOTE_INVALID", 1),)
    assert outcome.answer is not None
    assert "甲部门保存记录 14 天。 [S1]" in outcome.answer
    assert "15 天" not in outcome.answer


def test_zero_model_claims_uses_one_safe_extractive_fallback() -> None:
    evidence = _evidence("甲部门保存记录 14 天。")
    plan = _plan("甲部门")
    generator = Mock()
    generator.generate.return_value = _draft((), plan)

    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )

    assert generator.generate.call_count == 1
    assert outcome.mode == "extractive_fallback"
    assert outcome.reason_code == "EXTRACTIVE_FALLBACK"
    assert outcome.published_support_ids == ("S1",)


def test_fallback_uses_relevant_complete_group_only() -> None:
    """完整结构组已命中时，不混入其它组的事实。"""
    evidence = tuple(
        _evidence(sentence)[0].model_copy(
            update={"evidence_id": f"S{index}"}
        )
        for index, sentence in enumerate(
            (
                "乙部门保存设备记录 30 天。",
                "甲部门保存记录 14 天。",
                "甲部门每周核对记录。",
            ),
            1,
        )
    )
    grouped = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "group_complete": True,
                        "evidence_group_id": (
                            "egrp_unrelated" if index == 0 else "egrp_relevant"
                        ),
                    }
                )
            }
        )
        for index, item in enumerate(evidence)
    )
    plan = _plan("甲部门")
    pack = replace(
        _pack(plan, grouped),
        complete_group_ids=("egrp_unrelated", "egrp_relevant"),
    )
    generator = Mock()
    generator.generate.return_value = _draft((), plan)
    outcome = _answer_with_pack(
        generator,
        plan,
        grouped,
        ((AtomStatus.MISSING, ()),),
        pack=pack,
    )
    assert outcome.mode == "extractive_fallback"
    assert outcome.answer is not None
    assert "甲部门保存记录 14 天。" in outcome.answer
    assert "乙部门保存设备记录 30 天。" not in outcome.answer
    assert len(outcome.published_support_ids) == 2


def test_fallback_ignores_group_with_only_generic_query_overlap() -> None:
    """仅碰到“员工”等泛词的结构证据不能回答食堂菜单。"""
    item = _evidence("为员工的专业提升提供更有针对性的指引。")[0]
    item = item.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    **dict(item.metadata),
                    "group_complete": True,
                    "evidence_group_id": "egrp_unrelated",
                }
            )
        }
    )
    plan = _plan("员工食堂本周三午餐菜品")
    assert _safe_extractive_fallback(
        plan,
        (item,),
        {"A1": (item.support_id,)},
        ("egrp_unrelated",),
    ) is None
    assert _safe_extractive_fallback(
        plan,
        (_evidence("为员工的专业提升提供更有针对性的指引。")[0],),
        {"A1": (item.support_id,)},
    ) is None


def test_duration_fallback_prefers_contextual_deadline() -> None:
    """多轮时限问题优先展示直接时限，不借用同主题归档期限。"""
    deadline = _evidence("发布采购文件到应答截止时间，不得少于3日。")[0]
    archive = _evidence("采购文件需在项目结束后一个月内完成归档。")[0]
    archive = archive.model_copy(
        update={
            "evidence_id": "S2",
            "metadata": freeze_json_object(
                {
                    **dict(archive.metadata),
                    "group_complete": True,
                    "evidence_group_id": "egrp_archive",
                }
            ),
        }
    )
    plan = _plan("给供应商留几天？").model_copy(
        update={
            "resolved_root_query": "我们在准备直接采购文件。给供应商留几天？",
            "atoms": (
                QueryAtom(
                    atom_id="A1",
                    target="供应商应答",
                    relation="截止时限",
                    answer_shape=AtomAnswerShape.DURATION,
                ),
            ),
        }
    )
    result = _safe_extractive_fallback(
        plan,
        (deadline, archive),
        {"A1": (deadline.support_id, archive.support_id)},
        ("egrp_archive",),
    )
    assert result is not None
    assert result[1] == (deadline.support_id,)


def test_fallback_uses_named_complete_table_row() -> None:
    """模式行的输入和启动条件均已入包时，不回退到模式介绍段。"""
    evidence = _evidence(
        "开发中心模式包括需求快验、项目交付和产品开发。",
        "需求快验",
        "需求功能点描述、验收标准；演示目标和用户场景。",
        "经周例会评审通过，以邮件发出会议纪要为准启动。",
    )
    generic = next(
        item for item in evidence
        if item.citation_text.startswith("开发中心模式包括")
    )
    row = [item for item in evidence if item is not generic]
    group_id = "egrp_mode_row"
    row = [
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "group_complete": False,
                        "evidence_group_id": group_id,
                        "evidence_group_type": "TABLE_ROW_GROUP",
                    }
                )
            }
        )
        for item in row
    ]
    plan = _plan(
        "需求快验", "输入和启动", shape=AtomAnswerShape.FACT
    ).model_copy(
        update={
            "original_query": "需求快验模式需要哪些输入内容，启动条件是什么？",
            "resolved_root_query": (
                "需求快验模式需要哪些输入内容，启动条件是什么？"
            ),
        }
    )
    items = (generic, *row)
    result = _safe_extractive_fallback(
        plan,
        items,
        {
            atom.atom_id: (generic.support_id,)
            for atom in plan.atoms
        },
        (group_id,),
    )
    assert result is not None
    assert "需求功能点描述" in result[0]
    assert "会议纪要" in result[0]
    assert "开发中心模式包括" not in result[0]


def test_fallback_rejoins_one_source_paragraph_across_chunks() -> None:
    """同一原文段落分成数块后仍能展示完整人工成本核算步骤。"""
    evidence = _evidence(
        "研发人员记录项目人工工时。",
        "每月底研发项目承担部门审批工时记录。",
        "归口管理部门提交汇总工时表至人力资源岗。",
        "财务岗审核入账。",
    )
    node_id = evidence[0].source_spans[0].node_id
    joined = tuple(
        item.model_copy(
            update={
                "source_spans": tuple(
                    span.model_copy(update={"node_id": node_id})
                    for span in item.source_spans
                )
            }
        )
        for item in evidence
    )
    plan = _plan("研发人工成本核算", shape=AtomAnswerShape.PROCEDURE)
    result = _safe_extractive_fallback(
        plan,
        joined,
        {"A1": tuple(item.support_id for item in joined)},
    )
    assert result is not None
    assert len(result[1]) == 4
    assert "财务岗审核入账" in result[0]


def test_fallback_extends_selected_paragraph_to_complete_list() -> None:
    """段落命中完整流程组时，回退摘录保留同组后续阶段。"""
    evidence = _evidence(
        "立项申报阶段，提交项目目标和实施计划，",
        "并附上相关材料。",
        "立项审核阶段，审查材料完整性。",
        "立项决策阶段，提交会议审议。",
    )
    node_id = evidence[0].source_spans[0].node_id
    grouped: list[EvidenceItem] = []
    for index, item in enumerate(evidence):
        spans = item.source_spans
        if index < 2:
            spans = tuple(
                span.model_copy(update={"node_id": node_id})
                for span in spans
            )
        grouped.append(
            item.model_copy(
                update={
                    "source_spans": spans,
                    "metadata": freeze_json_object(
                        {
                            **dict(item.metadata),
                            "evidence_group_id": "egrp_stages",
                            "evidence_group_type": "LIST_GROUP",
                            "group_complete": True,
                        }
                    ),
                }
            )
        )
    plan = _plan("立项", "材料和步骤", shape=AtomAnswerShape.PROCEDURE)
    result = _safe_extractive_fallback(
        plan,
        tuple(grouped),
        {"A1": tuple(item.support_id for item in grouped)},
        ("egrp_stages",),
    )

    assert result is not None
    assert len(result[1]) == 4
    assert "立项决策阶段" in result[0]


def test_one_accepted_atom_keeps_limited_answer_for_unanswered_atom() -> None:
    evidence = _evidence("甲部门保存记录 14 天。", "乙部门审核记录 3 天。")
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("甲部门", "乙部门")
    pack = _pack(
        plan,
        evidence,
        atom_ids_by_support={
            by_text["甲部门保存记录 14 天。"]: ("A1",),
            by_text["乙部门审核记录 3 天。"]: ("A2",),
        },
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "甲部门保存记录 14 天。",
                "A1",
                by_text["甲部门保存记录 14 天。"],
            ),
        ),
        plan,
    )

    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()), (AtomStatus.MISSING, ())),
        pack=pack,
    )

    assert outcome.answer is not None
    assert "甲部门保存记录 14 天" in outcome.answer
    assert "现有资料中没有找到" not in outcome.answer
    assert outcome.reason_code == "LIMITED_ANSWER"
    assert outcome.atom_coverage == (("A1", "SUPPORTED"), ("A2", "MISSING"))
    assert outcome.missing_atom_reasons == (("A2", "GENERATION_INCOMPLETE"),)


def test_empty_pack_does_not_call_generator() -> None:
    plan = _plan("甲部门")
    generator = Mock()

    outcome = _answer_with_pack(
        generator,
        plan,
        (),
        ((AtomStatus.MISSING, ()),),
    )

    assert outcome.answer is None
    generator.generate.assert_not_called()


@pytest.mark.parametrize(
    ("source", "claim_text", "expected_rejection"),
    [
        (
            "甲部门保存记录 14 天。",
            "甲部门保存记录 15 天。",
            "CLAIM_NUMBER_MISMATCH",
        ),
        (
            "甲部门可以检查材料。",
            "甲部门必须检查材料。",
            "CLAIM_MODALITY_MISMATCH",
        ),
        (
            "甲部门负责核对材料；乙部门负责归档材料。",
            "乙部门负责归档材料。",
            "CLAIM_TARGET_UNSUPPORTED",
        ),
    ],
)
def test_high_risk_claim_drift_is_rejected_after_admission(
    source: str, claim_text: str, expected_rejection: str
) -> None:
    evidence = _evidence(source)
    plan = _plan("甲部门")
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", claim_text, "A1", "S1", source),), plan
    )

    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()),),
    )

    assert outcome.accepted_claim_count == 0
    assert (expected_rejection, 1) in outcome.claim_rejection_codes
    assert generator.generate.call_count == 1
