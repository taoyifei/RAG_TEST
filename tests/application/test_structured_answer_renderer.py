"""确定性结构化 renderer 的公开合成回归。"""

from __future__ import annotations

import pytest

from rag_app.application.answering.structured import (
    DeterministicAnswerRenderer,
    RenderedAnswer,
    validate_rendered_answer,
)
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import EvidenceItem, QueryAnalysis
from tests.application.retrieval.test_descriptive_answers import (
    _POLICY,
    _candidates,
    _heading,
    _list_paragraph,
    _paragraph,
)
from tests.application.retrieval.test_evidence_table_coordinates import _context


def _render(
    question: str,
    blocks: str,
    *,
    name: str = "合成规范.docx",
) -> tuple[RenderedAnswer, QueryAnalysis, tuple[EvidenceItem, ...]]:
    context = _context(question)
    evidence = EvidenceAssembler().assemble(
        _candidates(blocks, display_name=name),
        _POLICY,
        context=context,
    )
    assert evidence
    return (
        DeterministicAnswerRenderer().render(context.analysis, evidence),
        context.analysis,
        evidence,
    )


def test_renderer_uses_stage_headings_and_every_verified_body() -> None:
    blocks = _paragraph("青鸟交付全流程规范") + _heading("全流程要求")
    stages = ("受理与启动", "实施与核对", "验收与归档")
    for number, stage in enumerate(stages, 1):
        blocks += _heading(f"3.{number} {stage}", level=2)
        blocks += _paragraph(f"{stage}阶段执行公开验证要求。")

    rendered, _analysis, evidence = _render(
        "青鸟交付全流程规范有哪些阶段？",
        blocks,
        name="青鸟产品说明.docx",
    )

    assert all(stage in rendered.text for stage in stages)
    assert all(item.citation_text in rendered.text for item in evidence)
    assert rendered.published_support_ids == tuple(
        item.support_id for item in evidence
    )


def test_renderer_joins_complete_definition_cell_fragments() -> None:
    blocks = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
        "<w:tr><w:tc>"
        + _paragraph("术语")
        + "</w:tc><w:tc>"
        + _paragraph("定义说明")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + _paragraph("NWC")
        + "</w:tc><w:tc>"
        + _paragraph("Network")
        + _paragraph("Work")
        + _paragraph("Cell")
        + "</w:tc></w:tr></w:tbl>"
    )

    rendered, _analysis, evidence = _render("什么是 NWC？", blocks)

    assert "NWC｜定义说明：Network Work Cell" in rendered.text
    assert set(rendered.published_support_ids) == {
        item.support_id for item in evidence
    }


def test_renderer_lists_every_duty_from_the_same_role_row() -> None:
    duties = ("核对公开任务。", "协调验证资源。", "保存验收记录。")
    blocks = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
        "<w:tr><w:tc>"
        + _paragraph("角色名称")
        + "</w:tc><w:tc>"
        + _paragraph("核心职责")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + _paragraph("青鸟协调员")
        + "</w:tc><w:tc>"
        + "".join(_paragraph(item) for item in duties)
        + "</w:tc></w:tr></w:tbl>"
    )

    rendered, _analysis, _evidence = _render(
        "青鸟协调员平时主要管哪些事？", blocks
    )

    assert all(duty in rendered.text for duty in duties)
    assert "青鸟协调员｜1." in rendered.text
    assert "青鸟协调员｜3." in rendered.text


def test_renderer_removes_dangling_list_intro_and_keeps_all_steps() -> None:
    intro = "设备借用流程包括以下步骤："
    steps = ("登记借用信息。", "完成双人复核。", "归还后核对状态。")
    blocks = _paragraph(intro) + "".join(
        _list_paragraph(step) for step in steps
    )

    rendered, _analysis, _evidence = _render("设备借用流程该怎么做？", blocks)

    assert intro not in rendered.text
    assert all(step in rendered.text for step in steps)
    assert "步骤 1" in rendered.text
    assert "步骤 3" in rendered.text


def test_renderer_counts_real_structure_instead_of_question_premise() -> None:
    intro = "维修组的工作模式具体如下："
    modes = ("现场巡检。", "远程复核。")
    blocks = _paragraph(intro) + "".join(
        _list_paragraph(mode) for mode in modes
    )

    rendered, _analysis, _evidence = _render(
        "维修组的三种工作模式是什么？", blocks
    )

    assert "共 2 项。" in rendered.text
    assert all(mode in rendered.text for mode in modes)


def test_renderer_recalculation_rejects_tampered_output() -> None:
    rendered, analysis, evidence = _render(
        "青鸟制度的目的是什么？",
        _heading("目的") + _paragraph("统一公开验证方法并保留核验记录。"),
        name="青鸟制度.docx",
    )
    tampered = RenderedAnswer(
        text=rendered.text.replace("公开验证", "秘密采购"),
        published_support_ids=rendered.published_support_ids,
    )

    with pytest.raises(ValidationFailed) as error:
        validate_rendered_answer(tampered, analysis, evidence)

    assert error.value.code == "STRUCTURED_RENDER_VALIDATION_FAILED"


def test_renderer_rejects_evidence_without_supported_decision() -> None:
    """可引用原文若未经过直接支持门，也不能交给本地 renderer 发布。"""
    _rendered, analysis, evidence = _render(
        "青鸟制度的目的是什么？",
        _heading("目的") + _paragraph("统一公开验证方法并保留核验记录。"),
        name="青鸟制度.docx",
    )
    unverified = evidence[0].model_copy(update={"metadata": ()})

    with pytest.raises(ValidationFailed) as error:
        DeterministicAnswerRenderer().render(analysis, (unverified,))

    assert error.value.code == "STRUCTURED_SUPPORT_INVALID"
