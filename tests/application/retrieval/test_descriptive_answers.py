"""公共合成 DOCX 的职责多片段与完整集合支持回归。"""

from __future__ import annotations

from xml.sax.saxutils import escape

import pytest

from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import (
    HydratedChunk,
    RankedChunk,
    RetrievalPolicy,
    RrfContribution,
)
from tests.adapters.chunkers.test_docx_structural import _chunk
from tests.adapters.parsers.docx.fixtures import build_package, parse_package
from tests.application.retrieval.test_evidence_table_coordinates import _context

_POLICY = RetrievalPolicy(
    per_document_cap=8, per_section_cap=8, max_evidence_items_per_chunk=8
)


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def _candidates(blocks: str) -> tuple[RankedChunk, ...]:
    ir = parse_package(build_package(blocks)).document_ir
    return tuple(
        RankedChunk(
            hydrated=HydratedChunk(chunk=chunk, display_name="合成制度.docx"),
            fusion_rank=i,
            contributions=(
                RrfContribution(
                    channel="lexical:fts5",
                    rank=i,
                    weight=1.0,
                    contribution=1.0 / (60 + i),
                ),
            ),
        )
        for i, chunk in enumerate(_chunk(ir).chunks, 1)
    )


@pytest.mark.parametrize("role", ["设备协调员", "验收负责人"])
@pytest.mark.parametrize(
    "question",
    ["{role}的核心职责是什么", "{role}负责哪些工作", "{role}需要承担哪些职责"],
)
def test_role_table_keeps_every_duty_from_the_selected_row(
    role: str, question: str
) -> None:
    duties = (
        "核对任务目标与交付标准。",
        "协调维护资源并确认时间安排。",
        "组织最终验收并保存记录。",
    )
    blocks = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
        "<w:tr><w:tc>"
        + _paragraph("角色名称")
        + "</w:tc><w:tc>"
        + _paragraph("核心职责")
        + "</w:tc></w:tr>"
        "<w:tr><w:tc>"
        + _paragraph(role)
        + "</w:tc><w:tc>"
        + "".join(_paragraph(d) for d in duties)
        + "</w:tc></w:tr>"
        "<w:tr><w:tc>"
        + _paragraph("外部协助员")
        + "</w:tc><w:tc>"
        + _paragraph("仅登记物料清单。")
        + "</w:tc></w:tr></w:tbl>"
    )
    candidates = _candidates(blocks)
    evidence = EvidenceAssembler().assemble(
        candidates, _POLICY, context=_context(question.format(role=role))
    )
    assert [item.citation_text for item in evidence] == [role, *duties]
    assert all(
        dict(item.metadata)["answer_support"]["support_reason"]
        == "TABLE_ROW_ATTRIBUTE"
        for item in evidence
    )
    assert not EvidenceAssembler().assemble(
        candidates, _POLICY, context=_context(f"{role}的手机号是多少")
    )
    # 无法装下整行时不能截掉后一项职责后声称已经完整回答。
    assert not EvidenceAssembler().assemble(
        candidates,
        _POLICY.model_copy(update={"max_evidence_items": 2}),
        context=_context(question.format(role=role)),
    )


@pytest.mark.parametrize(
    "modes",
    [
        ("轮值维护", "专项修理"),
        ("常规巡检", "专项修理", "计划改造", "临时保障"),
    ],
)
@pytest.mark.parametrize(
    "question",
    [
        "维修组的工作模式是什么",
        "维修组有哪些工作模式",
        "请列举维修组采用的模式",
    ],
)
def test_enumeration_preserves_the_complete_source_set(
    modes: tuple[str, ...], question: str
) -> None:
    statement = (
        "维修组现有工作模式，分为" + "、".join(f"“{m}”" for m in modes) + "。"
    )
    candidates = _candidates(_paragraph(statement))
    evidence = EvidenceAssembler().assemble(
        candidates, _POLICY, context=_context(question)
    )
    assert [item.citation_text for item in evidence] == [statement]
    assert all(mode in evidence[0].citation_text for mode in modes)


def test_uncertain_sources_require_explicit_grounded_generation_path() -> None:
    """规则无法覆盖的概括请求可交生成器核验，缺失号码仍不能借值。"""
    candidates = _candidates(
        _paragraph("阀门检查要求包括确认开度、核对记录并保存照片。")
    )
    context = _context("说明阀门检查要求并概括注意事项")
    assembler = EvidenceAssembler()
    assert not assembler.assemble(candidates, _POLICY, context=context)
    evidence = assembler.assemble(
        candidates, _POLICY, context=context, allow_uncertain=True
    )
    assert evidence
    assert dict(evidence[0].metadata)["answer_support"]["status"] == "UNCERTAIN"
    assert not assembler.assemble(
        candidates,
        _POLICY,
        context=_context("阀门检查员的手机号是多少"),
        allow_uncertain=True,
    )
