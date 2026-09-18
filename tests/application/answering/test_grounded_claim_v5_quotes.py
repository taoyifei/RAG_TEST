"""生成准入与逐字 Quote 发布分离后的定向回归。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    GroundedOutcome,
    _contextual_source_versions,
    _safe_extractive_fallback,
    _validate_short_question_source_anchor,
)
from rag_app.application.retrieval.generation_evidence import (
    EvidenceAdmissionReason,
    EvidenceAdmissionStatus,
    GenerationEvidenceEntry,
    GenerationEvidencePack,
)
from rag_app.core.errors import ValidationFailed
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


def test_yes_no_answer_uses_source_about_asked_action() -> None:
    """同主题的交付句不能替代问题所问的重新启动规则。"""
    evidence = _evidence(
        "任务转为正式交付模式时，需另行满足准入要求，重新启动流程。",
        "正式交付项目应提交验收材料。",
    )
    by_text = {item.citation_text: item.support_id for item in evidence}
    plan = _plan("转正式交付要重启吗？")
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "正式交付项目应提交验收材料。",
                "A1",
                by_text["正式交付项目应提交验收材料。"],
            ),
            _claim(
                "C2",
                "任务转为正式交付模式时，需另行满足准入要求，重新启动流程。",
                "A1",
                by_text[
                    "任务转为正式交付模式时，需另行满足准入要求，重新启动流程。"
                ],
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

    assert outcome.claim_rejection_codes == (("CLAIM_RELATION_UNSUPPORTED", 1),)
    assert outcome.answer is not None
    assert "重新启动流程" in outcome.answer
    assert "提交验收材料" not in outcome.answer


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
    unrelated = _evidence(
        "为进一步提升人才队伍的专业性和岗位匹配度，助力公司战略发展，员工需参加认证。",
        "鼓励员工参加外部任职资格认证考试，提供专业岗位指引。",
        "为员工的专业提升提供更有针对性的指引。",
    )
    grouped = tuple(
        entry.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(entry.metadata),
                        "group_complete": True,
                        "evidence_group_id": "egrp_unrelated",
                    }
                )
            }
        )
        for entry in unrelated
    )
    assert _safe_extractive_fallback(
        plan,
        grouped,
        {"A1": tuple(entry.support_id for entry in grouped)},
        ("egrp_unrelated",),
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


def test_short_followup_rejects_other_document_duration() -> None:
    """历史语境唯一指向直接采购资料时，不发布相似采购指引的时限。"""
    source_items = _evidence(
        "发布采购文件到应答截止时间，不得少于3日。",
        "非招标方式的采购公告发售期一般为3天半。",
    )
    by_text = {item.citation_text: item for item in source_items}
    direct = by_text["发布采购文件到应答截止时间，不得少于3日。"]
    other = by_text["非招标方式的采购公告发售期一般为3天半。"]
    direct = direct.model_copy(
        update={
            "document_version_id": "dver_direct",
            "display_name": "湾区研究院直接采购项目方案决策通过后实施流程V1.0",
        }
    )
    other = other.model_copy(
        update={
            "document_version_id": "dver_general",
            "display_name": "附件1 湾区研究院采购工作实施指引(V2.0)",
        }
    )
    plan = _plan("给供应商留几天？", shape=AtomAnswerShape.DURATION).model_copy(
        update={
            "original_query": "给供应商留几天？",
            "resolved_root_query": (
                "文件 我们在准备直接采购文件 给供应商留几天 "
                "给供应商留几天?"
            ),
            "context_resolution_mode": "RULE_CONTEXT",
        }
    )
    assert _contextual_source_versions(plan, (direct, other)) == frozenset(
        {"dver_direct"}
    )
    assert _safe_extractive_fallback(
        plan,
        (direct, other),
        {"A1": (direct.support_id, other.support_id)},
    ) is not None
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            _claim(
                "C1",
                "非招标方式的采购公告发售期一般为3天半。",
                "A1",
                other.support_id,
            ),
        ),
        plan,
    )
    outcome = _answer_with_pack(
        generator,
        plan,
        (direct, other),
        ((AtomStatus.MISSING, ()),),
    )

    assert outcome.claim_rejection_codes == (
        ("CLAIM_SOURCE_SCOPE_MISMATCH", 1),
    )
    assert outcome.answer is not None
    assert "不得少于3日" in outcome.answer
    assert "3天半" not in outcome.answer
    assert outcome.published_support_ids == (direct.support_id,)


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
        "（二）立项申报阶段，提交项目目标和实施计划，",
        "并附上相关材料。附件须签字盖章。",
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
    assert "附件须签字盖章" in result[0]
    assert "实施计划，并附上相关材料" in result[0]
    assert "- 并附上相关材料" not in result[0]
    assert "- （二）" not in result[0]


def test_fallback_keeps_adjacent_preparation_and_stage_overview() -> None:
    """多子问题回退展示已入包的同章节前序阶段。"""
    evidence = _evidence(
        "项目立项包括准备、申报、审核、决策和系统立项。",
        "立项准备阶段开展可行性分析并编制项目材料。",
        "（二）立项申报阶段提交目标材料和实施计划。",
        "（三）立项审核阶段审查材料并确认步骤。",
        "（四）立项决策阶段提交会议审议。",
    )
    source_positions = {
        "项目立项包括准备": ("egrp_overview", 113),
        "立项准备阶段": ("egrp_preparation", 114),
        "（二）立项申报阶段": ("egrp_later", 117),
        "（三）立项审核阶段": ("egrp_later", 118),
        "（四）立项决策阶段": ("egrp_later", 119),
    }

    def locate(item: EvidenceItem) -> tuple[str, int]:
        return next(
            value
            for prefix, value in source_positions.items()
            if item.citation_text.startswith(prefix)
        )
    grouped = tuple(
        item.model_copy(
            update={
                "chunk_id": f"chunk_{locate(item)[1]:032x}",
                "publishable": locate(item)[1] < 118,
                "source_spans": tuple(
                    span.model_copy(
                        update={
                            "source_anchor": span.source_anchor.model_copy(
                                update={"ordinal": locate(item)[1]}
                            )
                        }
                    )
                    for span in item.source_spans
                    if span.source_anchor is not None
                ),
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": locate(item)[0],
                        "evidence_group_type": (
                            "SECTION_GROUP"
                            if locate(item)[0] == "egrp_later"
                            else "LIST_GROUP"
                        ),
                        "group_complete": True,
                    }
                ),
            }
        )
        for item in evidence
    )
    assert len({item.chunk_id for item in grouped}) == 5
    # 规划器把复合流程误标成 FACT 时，也要遵守问句中的步骤请求。
    plan = _plan("项目立项", "材料和步骤")
    result = _safe_extractive_fallback(
        plan,
        grouped,
        {"A1": tuple(item.support_id for item in grouped)},
        ("egrp_overview", "egrp_preparation", "egrp_later"),
    )
    assert result is not None
    assert "立项准备阶段" in result[0]
    assert "项目立项包括准备" in result[0]
    assert "立项申报阶段" in result[0]
    assert "立项审核阶段" in result[0]
    assert "立项决策阶段" in result[0]


def test_fallback_orders_source_spans_without_start_offset() -> None:
    """派生编号没有来源字符偏移时，摘录排序仍可完成。"""
    evidence = _evidence("立项申报阶段提交材料。", "立项审核阶段审查材料。")
    anchor = evidence[0].source_spans[0].source_anchor
    assert anchor is not None
    adjusted = tuple(
        item.model_copy(
            update={
                "source_spans": tuple(
                    span.model_copy(
                        update={
                            "source_anchor": anchor,
                            "source_start_char": None if index == 0 else 1,
                        }
                    )
                    for span in item.source_spans
                ),
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": "egrp_offset",
                        "evidence_group_type": "LIST_GROUP",
                        "group_complete": True,
                    }
                ),
            }
        )
        for index, item in enumerate(evidence)
    )
    plan = _plan("立项", "阶段", shape=AtomAnswerShape.PROCEDURE)
    result = _safe_extractive_fallback(
        plan,
        adjusted,
        {"A1": tuple(item.support_id for item in adjusted)},
        ("egrp_offset",),
    )
    assert result is not None
    assert "立项申报阶段" in result[0]
    assert "立项审核阶段" in result[0]


def test_short_question_rejects_a_less_relevant_cited_fragment() -> None:
    """同版资料有直接问句锚点时，不把泛主题条款当成答案。"""
    evidence = _evidence(
        "所有认证不可多次报销；",
        "对于员工跨年领到证书的情况，请在证书领取年份进行报销，该费用占用本部门该年度的费用额度；",
    )
    plan = _plan("费用跨年还能报吗？")

    with pytest.raises(ValidationFailed) as error:
        _validate_short_question_source_anchor(plan, evidence, (evidence[0],))

    assert error.value.code == "CLAIM_QUERY_RELATION_UNSUPPORTED"


def test_short_question_fallback_keeps_semicolon_terminated_source() -> None:
    """跨年规则以分号结束时仍可保守摘录原文。"""
    evidence = _evidence(
        "所有认证不可多次报销；",
        "对于员工跨年领到证书的情况，请在证书领取年份进行报销，该费用占用本部门该年度的费用额度；",
    )
    plan = _plan("费用跨年还能报吗？")
    result = _safe_extractive_fallback(
        plan,
        evidence,
        {"A1": tuple(item.support_id for item in evidence)},
    )

    assert result is not None
    assert "证书领取年份进行报销" in result[0]
    assert "不可多次报销" not in result[0]


def test_short_question_uses_relevant_member_of_complete_group() -> None:
    """短问只摘录对应条款，不附带同组其它事项。"""
    evidence = _evidence(
        "所有认证不可多次报销；",
        "对于员工跨年领到证书的情况，请在证书领取年份进行报销，该费用占用本部门该年度的费用额度；",
    )
    grouped = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": "egrp_reimbursement",
                        "evidence_group_type": "LIST_GROUP",
                        "group_complete": True,
                    }
                )
            }
        )
        for item in evidence
    )
    plan = _plan("费用跨年还能报吗？")
    result = _safe_extractive_fallback(
        plan,
        grouped,
        {"A1": tuple(item.support_id for item in grouped)},
        ("egrp_reimbursement",),
    )

    assert result is not None
    assert "证书领取年份进行报销" in result[0]
    assert "不可多次报销" not in result[0]


def test_short_question_keeps_complete_numbered_decision_sequence() -> None:
    """短追问命中编号流程时保留同组审批和生效条件。"""
    evidence = _evidence(
        "1）各团队提出调整需求；",
        "2）牵头部门审核调整需求；",
        "3）主管机构审批调整方案；",
        "4）审批通过后执行调整。",
    )
    grouped = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": "egrp_decision",
                        "evidence_group_type": "PARAGRAPH_GROUP",
                        "group_complete": True,
                    }
                )
            }
        )
        for item in evidence
    )
    plan = _plan("调整能直接执行吗？")
    result = _safe_extractive_fallback(
        plan,
        grouped,
        {"A1": tuple(item.support_id for item in grouped)},
        ("egrp_decision",),
    )

    assert result is not None
    assert "主管机构审批调整方案" in result[0]
    assert "审批通过后执行调整" in result[0]


def test_compound_facts_keep_nearby_complete_source_groups() -> None:
    """跨章节的条件与时间规则各自保留原句和引用。"""
    evidence = _evidence(
        "各部门员工通过认证拿到证书后，在费用额度内提交发票报销。",
        "员工跨年领到证书的，请在证书领取年份报销，占用该年度费用额度；",
    )
    grouped = tuple(
        item.model_copy(
            update={
                "section_id": (
                    "section_notice"
                    if "跨年" in item.citation_text
                    else "section_process"
                ),
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": (
                            "egrp_notice"
                            if "跨年" in item.citation_text
                            else "egrp_process"
                        ),
                        "evidence_group_type": "PARAGRAPH_GROUP",
                        "group_complete": True,
                    }
                ),
            }
        )
        for item in evidence
    )
    plan = _plan("认证费跨年报销", "报销条件").model_copy(
        update={
            "original_query": "员工跨年领证后，认证费报销条件是什么？",
            "resolved_root_query": "员工跨年领证后，认证费报销条件是什么？",
        }
    )
    result = _safe_extractive_fallback(
        plan,
        grouped,
        {atom.atom_id: tuple(item.support_id for item in grouped)
         for atom in plan.atoms},
        ("egrp_process", "egrp_notice"),
    )

    assert result is not None
    assert "费用额度内提交发票报销" in result[0]
    assert "证书领取年份报销" in result[0]


def test_compound_reimbursement_omits_orphan_heading() -> None:
    """半句条件和跨年条款各自引用，不能把相邻标题拼成事实。"""
    evidence = _evidence(
        "员工通过认证拿到证书后，在部门认证费用额度内集中报销，",
        "提交发票至管理部门报销。",
        "、其他注意事项",
        "跨年领到证书的，请在证书领取年份报销，占用本部门该年度额度；",
        "若在认证目录内，部分认证项目通过后需要年检的，也可报销；",
    )
    condition = next(
        item
        for item in evidence
        if item.citation_text.startswith("员工通过认证")
    )
    first_span = condition.source_spans[0]
    assert first_span.source_anchor is not None
    end = len(condition.citation_text)
    grouped = tuple(
        item.model_copy(
            update={
                "source_spans": (
                    item.source_spans[0].model_copy(
                        update={
                            "node_id": first_span.node_id,
                            "source_start_char": end,
                            "source_end_char": end + len(item.citation_text),
                            "source_anchor": item.source_spans[0]
                            .source_anchor.model_copy(
                                update={
                                    "ordinal": first_span.source_anchor.ordinal
                                }
                            ),
                        }
                    ),
                )
                if item.citation_text.startswith("提交发票")
                else item.source_spans,
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "evidence_group_id": (
                            "egrp_conditions"
                            if item.citation_text.startswith(
                                ("员工通过认证", "提交发票", "、其他注意事项")
                            )
                            else "egrp_cross_year"
                        ),
                        "evidence_group_type": "LIST_GROUP",
                        "group_complete": True,
                    }
                )
            }
        )
        for item in evidence
    )
    plan = _plan("认证费跨年报销", "报销条件").model_copy(
        update={
            "original_query": "认证费能跨年报吗，什么条件下才行？",
            "resolved_root_query": "认证费能跨年报吗，什么条件下才行？",
        }
    )
    links = {
        atom.atom_id: tuple(item.support_id for item in grouped)
        for atom in plan.atoms
    }

    result = _safe_extractive_fallback(
        plan, grouped, links, ("egrp_conditions", "egrp_cross_year")
    )

    assert result is not None
    assert "员工通过认证拿到证书后" in result[0]
    assert "集中报销，提交发票至管理部门报销。" in result[0]
    assert "证书领取年份报销" in result[0]
    assert "其他注意事项" not in result[0]
    assert "部分认证项目" not in result[0]
    assert "，、" not in result[0]


def test_single_atom_fallback_restores_one_split_source_paragraph() -> None:
    """单一材料问句也可拼回同一原文节点的两个片段。"""
    evidence = _evidence(
        "立项申报阶段，项目立项材料正式报送，项目申报材料应包括目标、计划、",
        "考核指标等内容，并附上签字盖章的相关附件。",
        "立项决策阶段，审议项目目标和实施计划。",
    )
    node_id = next(
        item.source_spans[0].node_id
        for item in evidence
        if item.citation_text.startswith("立项申报阶段")
    )
    joined = tuple(
        item.model_copy(
            update={
                "source_spans": tuple(
                    span.model_copy(update={"node_id": node_id})
                    for span in item.source_spans
                )
                if item.citation_text.startswith(
                    ("立项申报阶段", "考核指标等内容")
                )
                else item.source_spans
            }
        )
        for item in evidence
    )
    plan = _plan("立项材料还得补哪些？")
    result = _safe_extractive_fallback(
        plan,
        joined,
        {"A1": tuple(item.support_id for item in joined)},
    )

    assert result is not None
    assert "目标、计划、考核指标" in result[0]
    assert "立项决策阶段" not in result[0]


def test_material_enumeration_does_not_expand_into_later_stages() -> None:
    """材料清单已由同一原文节点闭合时，不附带其它立项阶段。"""
    evidence = _evidence(
        "立项申报阶段，项目立项材料正式报送，项目申报材料应包括目标、计划、",
        "考核指标等内容，并附上签字盖章的相关附件。",
        "立项决策阶段，审议项目目标和实施计划。",
    )
    material_node = next(
        item.source_spans[0].node_id
        for item in evidence
        if item.citation_text.startswith("立项申报阶段")
    )
    grouped = tuple(
        item.model_copy(
            update={
                "source_spans": tuple(
                    span.model_copy(update={"node_id": material_node})
                    for span in item.source_spans
                )
                if item.citation_text.startswith(
                    ("立项申报阶段", "考核指标等内容")
                )
                else item.source_spans,
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
        for item in evidence
    )
    plan = _plan("立项材料还得补哪些？", shape=AtomAnswerShape.ENUMERATION)
    result = _safe_extractive_fallback(
        plan,
        grouped,
        {"A1": tuple(item.support_id for item in grouped)},
        ("egrp_stages",),
    )

    assert result is not None
    assert "目标、计划、考核指标" in result[0]
    assert "立项决策阶段" not in result[0]


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


def test_one_repair_fills_admitted_atom_missing_from_first_draft() -> None:
    """准入证据存在时，首轮遗漏的 Atom 可获得唯一一次定向修复。"""
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
    generator.generate.side_effect = (
        _draft(
            (
                _claim(
                    "C1",
                    "乙部门审核记录 3 天。",
                    "A2",
                    by_text["乙部门审核记录 3 天。"],
                ),
            ),
            plan,
        ),
        _draft(
            (
                _claim(
                    "C2",
                    "甲部门保存记录 14 天。",
                    "A1",
                    by_text["甲部门保存记录 14 天。"],
                ),
            ),
            plan,
        ),
    )

    outcome = _answer_with_pack(
        generator,
        plan,
        evidence,
        ((AtomStatus.MISSING, ()), (AtomStatus.MISSING, ())),
        pack=pack,
    )

    assert generator.generate.call_count == 2
    assert generator.generate.call_args.args[0].repair_atom_ids == ("A1",)
    assert outcome.repair_calls == 1
    assert outcome.answer is not None
    assert "甲部门保存记录 14 天" in outcome.answer
    assert "乙部门审核记录 3 天" in outcome.answer


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
