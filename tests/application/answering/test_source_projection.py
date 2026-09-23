"""结构来源的最终事实文本必须由服务端按物理来源形成。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from rag_app.application.answering.evidence_binding import bind_wire_claim
from rag_app.application.answering.source_projection import (
    SourceProjectionError,
    project_bound_claim,
)
from rag_app.core.models import EvidenceReadUnit, GroundedWireClaim
from rag_app.core.models.common import freeze_json_object
from tests.application.answering.test_evidence_binding import _fixture
from tests.application.answering.test_natural_grounded_answer import _evidence


def test_table_projection_removes_model_invented_timing_relation() -> None:
    evidence, fact, unit, binding = _fixture()
    replacements = {
        "S1": "需求快验",
        "S2": "产品需求文档",
        "S3": "交互原型",
        "S4": "业务规则",
        "S5": "接口清单",
        "S6": "数据字典",
        "S7": "验收标准",
        "S8": "参与人员",
        "S9": "输入",
        "S10": "（业务团队 / 外部单位需提供）",
    }
    evidence = tuple(
        item.model_copy(update={"citation_text": replacements[item.support_id]})
        for item in evidence
    )
    assert evidence[0].document_id is not None
    assert evidence[0].document_version_id is not None
    fact = fact.model_copy(
        update={
            "document_id": evidence[0].document_id,
            "document_version_id": evidence[0].document_version_id,
        }
    )
    bound = bind_wire_claim(
        GroundedWireClaim(
            atom_id="A1",
            text="做快验之前必须备齐全部输入。",
            refs=("E1",),
        ),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )

    projected = project_bound_claim(
        bound,
        read_units=(unit,),
        evidence=evidence,
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )

    assert projected.render_origin == "physical_table_fact"
    assert "需求快验" in projected.text
    assert "输入（业务团队 / 外部单位需提供）" in projected.text
    assert "之前" not in projected.text
    assert "必须备齐" not in projected.text
    assert not projected.relation_complete
    assert projected.relation_gap_reason == "TABLE_RELATION_UNDETERMINED"
    assert projected.draft_text_sha256 != projected.published_text_sha256


def test_explicit_source_header_relation_is_preserved() -> None:
    evidence, fact, unit, binding = _fixture()
    replacements = {
        "S1": "发布审批",
        "S2": "审批单",
        "S3": "风险清单",
        "S4": "回滚方案",
        "S5": "测试报告",
        "S6": "变更记录",
        "S7": "负责人",
        "S8": "发布时间",
        "S9": "开始前",
        "S10": "必须提交",
    }
    evidence = tuple(
        item.model_copy(update={"citation_text": replacements[item.support_id]})
        for item in evidence
    )
    assert evidence[0].document_id is not None
    assert evidence[0].document_version_id is not None
    fact = fact.model_copy(
        update={
            "document_id": evidence[0].document_id,
            "document_version_id": evidence[0].document_version_id,
        }
    )
    binding = binding.model_copy(update={"relation_status": "SUPPORTED"})
    bound = bind_wire_claim(
        GroundedWireClaim(atom_id="A1", text="任意草稿", refs=("E1",)),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )

    projected = project_bound_claim(
        bound,
        read_units=(unit,),
        evidence=evidence,
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )

    assert "开始前必须提交" in projected.text
    assert projected.relation_complete
    assert projected.relation_gap_reason is None


def test_semantically_verified_table_fact_keeps_natural_duration() -> None:
    evidence, fact, unit, binding = _fixture()
    evidence = tuple(
        item.model_copy(
            update={
                "citation_text": {
                    "S1": "业务团队",
                    "S2": "收到设计文档后2个工作日内反馈并确认",
                    "S9": "反馈时效",
                }.get(item.support_id, item.citation_text)
            }
        )
        for item in evidence
    )
    fact = fact.model_copy(
        update={
            "document_id": evidence[0].document_id,
            "document_version_id": evidence[0].document_version_id,
        }
    )
    claim = "业务团队收到设计文档后2个工作日内反馈并确认。"
    bound = bind_wire_claim(
        GroundedWireClaim(atom_id="A1", text=claim, refs=("E1",)),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )

    projected = project_bound_claim(
        bound,
        read_units=(unit,),
        evidence=evidence,
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
        semantic_relation_supported=True,
        question_fragment="设计文档几天内反馈？",
    )
    invented = project_bound_claim(
        replace(bound, text="业务团队必须先反馈设计文档。"),
        read_units=(unit,),
        evidence=evidence,
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
        semantic_relation_supported=True,
        question_fragment="设计文档几天内反馈？",
    )

    assert projected.text == claim
    assert projected.relation_complete
    assert "必须先" not in invented.text
    assert not invented.relation_complete


def test_table_fact_cannot_borrow_another_explicit_level_row() -> None:
    evidence, fact, unit, binding = _fixture()
    evidence = tuple(
        item.model_copy(
            update={
                "citation_text": {
                    "S1": "甲类事件（Ⅰ级）",
                    "S2": "10分钟",
                    "S9": "报送时限",
                }.get(item.support_id, item.citation_text)
            }
        )
        for item in evidence
    )
    fact = fact.model_copy(
        update={
            "document_id": evidence[0].document_id,
            "document_version_id": evidence[0].document_version_id,
        }
    )
    bound = bind_wire_claim(
        GroundedWireClaim(
            atom_id="A1", text="乙类事件（Ⅱ级）须在10分钟内报送。", refs=("E1",)
        ),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
    )

    with pytest.raises(
        SourceProjectionError, match="QUESTION_TABLE_ROW_LEVEL_CONFLICT"
    ):
        project_bound_claim(
            bound,
            read_units=(unit,),
            evidence=evidence,
            physical_table_facts=(fact,),
            atom_fact_bindings=(binding,),
            semantic_relation_supported=True,
            original_query="乙类事件（Ⅱ级）的报送时限是多少？",
        )

    with pytest.raises(
        SourceProjectionError, match="QUESTION_TABLE_ROW_LEVEL_CONFLICT"
    ):
        project_bound_claim(
            bound,
            read_units=(unit,),
            evidence=evidence,
            physical_table_facts=(fact,),
            atom_fact_bindings=(binding,),
            semantic_relation_supported=True,
            original_query="甲类事件（Ⅰ级）的报送时限是多少？",
        )

    same_row = project_bound_claim(
        replace(bound, text="甲类事件（Ⅰ级）须在10分钟内报送。"),
        read_units=(unit,),
        evidence=evidence,
        physical_table_facts=(fact,),
        atom_fact_bindings=(binding,),
        semantic_relation_supported=True,
        original_query="甲类事件（Ⅰ级）的报送时限是多少？",
    )
    assert same_row.relation_complete


def test_semantic_review_cannot_supply_an_action_absent_from_source() -> None:
    evidence = _evidence("开发中心需在2个工作日内提交配置库")
    item = evidence[0]
    unit = EvidenceReadUnit(
        unit_id="E1",
        kind="paragraph",
        text=item.citation_text,
        support_ids=(item.support_id,),
        source_complete=True,
    )
    bound = bind_wire_claim(
        GroundedWireClaim(
            atom_id="A1",
            text="设计文档在2个工作日内反馈。",
            refs=("E1",),
        ),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
    )

    with pytest.raises(
        SourceProjectionError, match="QUESTION_ACTION_NOT_IN_SOURCE"
    ):
        project_bound_claim(
            bound,
            read_units=(unit,),
            evidence=evidence,
            semantic_relation_supported=True,
            question_fragment="设计文档几天内反馈？",
        )


def test_table_fragment_can_only_publish_literal_fragment() -> None:
    evidence = _evidence("输入（业务团队需提供）")
    item = evidence[0]
    unit = EvidenceReadUnit(
        unit_id="E1",
        kind="paragraph",
        text=item.citation_text,
        source_context=freeze_json_object(
            {
                "structure_scope": "literal_table_fragment",
                "table_relation_complete": False,
            }
        ),
        support_ids=(item.support_id,),
        source_complete=True,
    )
    bound = bind_wire_claim(
        GroundedWireClaim(
            atom_id="A1",
            text="开始之前必须提供这些材料。",
            refs=("E1",),
        ),
        claim_id="C1",
        read_units=(unit,),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1"}),
    )

    projected = project_bound_claim(
        bound, read_units=(unit,), evidence=evidence
    )

    assert projected.text == "资料相关片段记载：‘输入（业务团队需提供）’。"
    assert projected.render_origin == "literal_table_fragment"
    assert not projected.relation_complete
    assert "开始之前" not in projected.text


def test_protected_projection_drops_unused_mixed_reference() -> None:
    evidence = _evidence("输入字段", "另一段普通说明。")
    fragment, prose = evidence
    fragment_unit = EvidenceReadUnit(
        unit_id="E1",
        kind="paragraph",
        text=fragment.citation_text,
        source_context=freeze_json_object(
            {"structure_scope": "literal_table_fragment"}
        ),
        support_ids=(fragment.support_id,),
        source_complete=True,
    )
    prose_unit = EvidenceReadUnit(
        unit_id="E2",
        kind="paragraph",
        text=prose.citation_text,
        support_ids=(prose.support_id,),
        source_complete=True,
    )
    bound = bind_wire_claim(
        GroundedWireClaim(
            atom_id="A1", text="模型混合推断。", refs=("E1", "E2")
        ),
        claim_id="C1",
        read_units=(fragment_unit, prose_unit),
        evidence=evidence,
        allowed_unit_ids=frozenset({"E1", "E2"}),
    )

    projected = project_bound_claim(
        bound,
        read_units=(fragment_unit, prose_unit),
        evidence=evidence,
    )

    assert projected.selected_unit_ids == ("E1",)
    assert tuple(item.support_id for item in projected.provenance) == (
        fragment.support_id,
    )
    assert prose.citation_text not in projected.text
