"""公共合成 DOCX 的职责多片段与完整集合支持回归。"""

from __future__ import annotations

from xml.sax.saxutils import escape

import pytest

from rag_app.application.retrieval.answer_support import evaluate_span_support
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import (
    EvidenceSelectionContext,
    HydratedChunk,
    RankedChunk,
    RetrievalPolicy,
    RrfContribution,
)
from tests.adapters.chunkers.test_docx_structural import _chunk
from tests.adapters.parsers.docx.fixtures import build_package, parse_package
from tests.adapters.parsers.docx.fixtures import context as parse_context
from tests.application.retrieval.test_evidence_table_coordinates import _context

_POLICY = RetrievalPolicy(
    per_document_cap=8, per_section_cap=8, max_evidence_items_per_chunk=8
)
_REMOTE_SPACE = "primary:synthetic:table-grounding:3:l2:1"
_REMOTE_POLICY = _POLICY.model_copy(
    update={
        "dense_semantic_enabled": True,
        "dense_semantic_calibration_state": "ACTIVE_PROFILE",
        "dense_calibrated_vector_spaces": (_REMOTE_SPACE,),
    }
)


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def _list_paragraph(text: str) -> str:
    return (
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/>'
        '<w:numId w:val="7"/></w:numPr></w:pPr><w:r><w:t>'
        + escape(text)
        + "</w:t></w:r></w:p>"
    )


def _heading(text: str, *, level: int = 1) -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
        f"<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"
    )


def _candidates(
    blocks: str,
    *,
    display_name: str = "合成制度.docx",
    document_id: str | None = None,
    numbering: str | None = None,
) -> tuple[RankedChunk, ...]:
    ir = parse_package(
        build_package(blocks, numbering=numbering),
        name=display_name,
        parse_context=parse_context(
            document_id=document_id, display_name=display_name
        ),
    ).document_ir
    return tuple(
        RankedChunk(
            hydrated=HydratedChunk(chunk=chunk, display_name=display_name),
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


def _remote_candidates(
    candidates: tuple[RankedChunk, ...],
) -> tuple[RankedChunk, ...]:
    """给公开合成候选附加真实远程 Dense 与重排身份。"""
    return tuple(
        candidate.model_copy(
            update={
                "contributions": (
                    RrfContribution(
                        channel="dense:primary",
                        rank=index,
                        weight=1.0,
                        contribution=1.0 / (60 + index),
                    ),
                ),
                "rerank_rank": index,
                "rerank_score": 1.0 - index / 100,
            }
        )
        for index, candidate in enumerate(candidates, 1)
    )


def _remote_context(question: str) -> EvidenceSelectionContext:
    """返回允许模型查看远程重排候选的合成上下文。"""
    return _context(question).model_copy(
        update={
            "rerank_mode": "provider",
            "selected_slot": "primary",
            "selected_vector_space": _REMOTE_SPACE,
        }
    )


@pytest.mark.parametrize(
    "role",
    [
        "设备协调员",
        "验收负责人",
        "具体事务协调员",
        "干啥研究员",
        "做什么协调员",
    ],
)
@pytest.mark.parametrize(
    "question",
    [
        "{role}的核心职责是什么",
        "{role}负责哪些工作",
        "{role}需要承担哪些职责",
        "{role}具体负责哪些工作",
        "{role}具体地负责什么事项",
        "合成规范中{role}具体负责哪些工作",
        "{role}的具体职责是什么",
        "{role}具体干什么",
        "{role}具体干啥",
        "{role}具体做什么",
        "{role}具体干些什么",
        "{role}具体做些什么",
        "{role}干什么工作？",
    ],
)
def test_role_table_reaches_model_without_preassembled_rule_answer(
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
    candidates = _remote_candidates(_candidates(blocks))
    selection = EvidenceAssembler().assemble_sets(
        candidates,
        _REMOTE_POLICY,
        context=_remote_context(question.format(role=role)),
        include_model_candidates=True,
    )
    candidate_text = {
        item.citation_text for item in selection.model_evidence_candidates
    }
    assert {role, *duties} <= candidate_text
    assert selection.answer_support_set == ()
    assert not any(
        dict(item.metadata).get("answer_support", {}).get("support_reason")
        == "TABLE_ROW_ATTRIBUTE"
        for item in selection.model_evidence_candidates
    )
    assert not EvidenceAssembler().assemble(
        candidates,
        _REMOTE_POLICY,
        context=_remote_context(f"{role}的手机号是多少"),
    )
    for unsupported in (
        f"{role}的具体手机号是多少",
        f"{role}的具体价格是多少",
        f"{role}具体不负责哪些工作",
        f"{role}不具体负责哪些工作",
        f"{role}具体不干什么",
        f"{role}不具体做什么",
        "外部协调员具体负责哪些工作",
        "外部协调员具体干啥",
    ):
        assert not EvidenceAssembler().assemble(
            candidates,
            _REMOTE_POLICY,
            context=_remote_context(unsupported),
        )
    # 预算只裁剪模型输入，永远不能因此形成规则支持集。
    assert not EvidenceAssembler().assemble(
        candidates,
        _REMOTE_POLICY.model_copy(update={"max_evidence_items": 2}),
        context=_remote_context(question.format(role=role)),
    )


def test_role_table_keeps_raw_cells_for_model_instead_of_combining_answer() -> (
    None
):
    blocks = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/>"
        "</w:tblGrid><w:tr><w:tc>"
        + _paragraph("角色名称")
        + "</w:tc><w:tc>"
        + _paragraph("角色定义")
        + "</w:tc><w:tc>"
        + _paragraph("核心职责")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + _paragraph("合成负责人")
        + "</w:tc><w:tc>"
        + _paragraph("负责公开合成质量保障。")
        + "</w:tc><w:tc>"
        + _paragraph("维护公开合成用例。")
        + _paragraph("闭环公开合成缺陷。")
        + "</w:tc></w:tr></w:tbl>"
    )

    selection = EvidenceAssembler().assemble_sets(
        _remote_candidates(_candidates(blocks)),
        _REMOTE_POLICY,
        context=_remote_context("合成负责人平时主要管哪些事？"),
        include_model_candidates=True,
    )

    candidate_text = {
        item.citation_text for item in selection.model_evidence_candidates
    }
    assert {
        "合成负责人",
        "负责公开合成质量保障。",
        "维护公开合成用例。",
        "闭环公开合成缺陷。",
    } <= candidate_text
    assert selection.answer_support_set == ()


def test_flat_role_sections_use_exact_heading_owner_for_duty_support() -> None:
    blocks = "".join(
        _paragraph(text)
        for text in (
            "4 部门职责",
            "4.1 合成总经理",
            "制定公开合成目标。",
            "协调公开合成资源。",
            "4.2 合成财务",
            "在合成总经理领导下核对公开合成账目。",
        )
    )
    candidates = _remote_candidates(_candidates(blocks))
    selection = EvidenceAssembler().assemble_sets(
        candidates,
        _REMOTE_POLICY,
        context=_remote_context("合成总经理是干嘛的 负责什么的"),
        include_model_candidates=True,
    )
    by_quote = {
        item.citation_text: item.source_label
        for item in selection.model_evidence_candidates
    }

    assert [item.citation_text for item in selection.answer_support_set] == [
        "制定公开合成目标。",
        "协调公开合成资源。",
    ]
    assert [
        item.citation_text for item in selection.model_evidence_candidates[:2]
    ] == ["制定公开合成目标。", "协调公开合成资源。"]
    assert "制定公开合成目标。" in by_quote
    assert "协调公开合成资源。" in by_quote
    assert "合成总经理" in by_quote["制定公开合成目标。"]
    assert "合成总经理" in by_quote["协调公开合成资源。"]
    assert "合成财务" in by_quote["在合成总经理领导下核对公开合成账目。"]
    manager_candidates = tuple(
        item
        for item in selection.model_evidence_candidates
        if item.citation_text in {"制定公开合成目标。", "协调公开合成资源。"}
    )
    assert len(manager_candidates) == 2
    assert all(
        dict(item.metadata)["answer_support"]["support_reason"]
        == "SECTION_HEADING_BODY"
        for item in manager_candidates
    )


@pytest.mark.parametrize(
    ("quote", "supported"),
    [
        ("总经理负责制定质量方针。", True),
        ("总经理主持质量评审。", True),
        ("在公司治理中，总经理履行临机处理权。", True),
        ("在总经理领导下，财务部负责会计核算。", False),
        ("负责贯彻总经理的各项决策。", False),
        ("协助总经理制定质量方针。", False),
    ],
)
def test_duty_text_requires_target_as_grammatical_subject(
    quote: str, supported: bool
) -> None:
    analysis = _context("总经理负责什么").analysis

    result = evaluate_span_support(analysis, quote)

    assert (result.status.value == "SUPPORTED") is supported


def test_duty_heading_does_not_borrow_nested_or_sibling_role_body() -> None:
    blocks = "".join(
        _paragraph(text)
        for text in (
            "4 部门职责",
            "4.1 总经理",
            "制定公司质量目标。",
            "4.1.1 生产经理",
            "主持生产调度会。",
            "4.2 副总经理",
            "组织设备验收。",
        )
    )

    evidence = EvidenceAssembler().assemble(
        _candidates(blocks),
        _POLICY,
        context=_context("总经理干嘛的负责什么"),
    )

    assert [item.citation_text for item in evidence] == ["制定公司质量目标。"]


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
        "维修组的工作模式是啥",
        "维修组有哪些工作模式",
        "维修组是哪几种工作模式",
        "维修组的工作模式分别指什么",
        "请把维修组的工作模式列出来",
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


def test_enumeration_reports_source_count_when_question_premise_differs() -> (
    None
):
    statement = "维修组现有工作模式，分为“轮值维护”、“专项修理”。"
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(statement)),
        _POLICY,
        context=_context("维修组的三种工作模式是什么"),
    )

    assert [item.citation_text for item in evidence] == [statement]
    support = dict(evidence[0].metadata)["answer_support"]
    assert support["support_reason"] == "SOURCE_CORRECTS_COUNT_PREMISE"


def test_source_qualifier_filters_model_candidates_without_rule_answer() -> (
    None
):
    def table(duties: tuple[str, ...]) -> str:
        return (
            "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
            "<w:tr><w:tc>"
            + _paragraph("角色名称")
            + "</w:tc><w:tc>"
            + _paragraph("核心职责")
            + "</w:tc></w:tr><w:tr><w:tc>"
            + _paragraph("项目经理")
            + "</w:tc><w:tc>"
            + "".join(_paragraph(item) for item in duties)
            + "</w:tc></w:tr></w:tbl>"
        )

    selected = ("统筹蓝熊计划。", "跟踪蓝熊风险。")
    candidates = _remote_candidates(
        (
            *_candidates(
                table(selected),
                display_name="蓝熊交付规范.docx",
                document_id="doc_" + "4" * 32,
            ),
            *_candidates(
                table(("统筹白鹭计划。", "跟踪白鹭风险。")),
                display_name="白鹭研发制度.docx",
                document_id="doc_" + "5" * 32,
            ),
        )
    )
    assembler = EvidenceAssembler()

    selection = assembler.assemble_sets(
        candidates,
        _REMOTE_POLICY,
        context=_remote_context("蓝熊规范中项目经理负责什么"),
        include_model_candidates=True,
    )

    candidate_text = {
        item.citation_text for item in selection.model_evidence_candidates
    }
    assert {"项目经理", *selected} <= candidate_text
    assert "统筹白鹭计划。" not in candidate_text
    assert selection.answer_support_set == ()

    ambiguous = assembler.assemble_sets(
        candidates,
        _REMOTE_POLICY,
        context=_remote_context("项目经理负责什么"),
        include_model_candidates=True,
    )
    ambiguous_text = {
        item.citation_text for item in ambiguous.model_evidence_candidates
    }
    ambiguous_sources = {
        item.display_name for item in ambiguous.model_evidence_candidates
    }
    assert "统筹蓝熊计划。" in ambiguous_text
    assert {"蓝熊交付规范.docx", "白鹭研发制度.docx"} <= ambiguous_sources
    assert ambiguous.answer_support_set == ()
    assert not assembler.assemble_sets(
        candidates,
        _REMOTE_POLICY,
        context=_remote_context("不存在规范中项目经理负责什么"),
        include_model_candidates=True,
    ).model_evidence_candidates


def test_project_context_is_left_for_model_to_resolve_across_documents() -> (
    None
):
    def table(duties: tuple[str, ...]) -> str:
        return (
            "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
            "<w:tr><w:tc>"
            + _paragraph("角色名称")
            + "</w:tc><w:tc>"
            + _paragraph("核心职责")
            + "</w:tc></w:tr><w:tr><w:tc>"
            + _paragraph("测试协调员")
            + "</w:tc><w:tc>"
            + "".join(_paragraph(item) for item in duties)
            + "</w:tc></w:tr></w:tbl>"
        )

    selected = ("维护蓝熊用例。", "跟踪蓝熊缺陷。")
    candidates = _remote_candidates(
        (
            *_candidates(
                table(selected),
                display_name="蓝熊交付规范.docx",
                document_id="doc_" + "6" * 32,
            ),
            *_candidates(
                table(("维护白鹭用例。", "跟踪白鹭缺陷。")),
                display_name="白鹭运维规范.docx",
                document_id="doc_" + "7" * 32,
            ),
        )
    )

    selection = EvidenceAssembler().assemble_sets(
        candidates,
        _REMOTE_POLICY,
        context=_remote_context(
            "做蓝熊交付项目时，测试协调员平时主要管哪些事？"
        ),
        include_model_candidates=True,
    )

    candidate_text = {
        item.citation_text for item in selection.model_evidence_candidates
    }
    candidate_sources = {
        item.display_name for item in selection.model_evidence_candidates
    }
    assert {"测试协调员", *selected} <= candidate_text
    assert {"蓝熊交付规范.docx", "白鹭运维规范.docx"} <= candidate_sources
    assert selection.answer_support_set == ()


@pytest.mark.parametrize(
    "duty_header",
    ("主要工作职责说明", "角色定义", "岗位角色描述"),
)
def test_project_context_does_not_preassemble_same_role_table_answer(
    duty_header: str,
) -> None:
    def table(duties: tuple[str, ...]) -> str:
        return (
            "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
            "<w:tr><w:tc>"
            + _paragraph("角色名称")
            + "</w:tc><w:tc>"
            + _paragraph(duty_header)
            + "</w:tc></w:tr><w:tr><w:tc>"
            + _paragraph("测试协调员")
            + "</w:tc><w:tc>"
            + "".join(_paragraph(item) for item in duties)
            + "</w:tc></w:tr></w:tbl>"
        )

    selected = ("维护蓝熊用例。", "跟踪蓝熊缺陷。")
    candidates = _remote_candidates(
        _candidates(
            _heading("蓝熊交付")
            + table(selected)
            + _heading("白鹭运维")
            + table(("维护白鹭用例。", "跟踪白鹭缺陷。")),
            display_name="综合项目规范.docx",
        )
    )

    selection = EvidenceAssembler().assemble_sets(
        candidates,
        _REMOTE_POLICY,
        context=_remote_context(
            "做蓝熊交付项目时，测试协调员平时主要管哪些事？"
        ),
        include_model_candidates=True,
    )

    candidate_text = {
        item.citation_text for item in selection.model_evidence_candidates
    }
    assert {"测试协调员", *selected} <= candidate_text
    assert selection.answer_support_set == ()


def test_source_qualifier_filters_direct_relation_evidence() -> None:
    candidates = (
        *_candidates(
            _paragraph("星环登记表由白鹭协调员负责。"),
            display_name="白鹭研发制度.docx",
            document_id="doc_" + "4" * 32,
        ),
        *_candidates(
            _paragraph("星环登记表由蓝熊协调员负责。"),
            display_name="蓝熊交付规范.docx",
            document_id="doc_" + "5" * 32,
        ),
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("白鹭制度中，星环登记表谁负责"),
    )

    assert [item.citation_text for item in evidence] == [
        "星环登记表由白鹭协调员负责。"
    ]
    assert not EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("不存在制度中，星环登记表谁负责"),
    )
    assert not EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("不存在制度中，星环登记表谁负责"),
        allow_uncertain=True,
    )


def test_responsible_party_does_not_relax_a_named_artifact_target() -> None:
    candidates = _candidates(
        _paragraph("所有快速验证任务由蓝熊协调员接收报备，并完成项目登记。"),
        display_name="蓝熊快速验证规范.docx",
        document_id="doc_" + "6" * 32,
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("蓝熊快速验证规范中，项目报备登记表谁负责"),
    )

    assert not evidence


def test_uncertain_sources_reach_model_without_rule_gate() -> None:
    """有界原文先交模型判断，确定性规则只标记直接支持集。"""
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
    phone_selection = assembler.assemble_sets(
        candidates,
        _POLICY,
        context=_context("阀门检查员的手机号是多少"),
        include_model_candidates=True,
    )
    assert phone_selection.answer_support_set == ()
    assert phone_selection.model_evidence_candidates


def test_weak_topic_overlap_stays_out_of_direct_support_set() -> None:
    """弱命中可供模型判读，但不能冒充确定性直接支持。"""
    candidates = _candidates(
        _paragraph("公开守则说明常规审批要求与工作日归档安排。")
    )

    selection = EvidenceAssembler().assemble_sets(
        candidates,
        _POLICY,
        context=_context("这些公开守则有没有要求周末必须值班？"),
        include_model_candidates=True,
    )

    assert selection.answer_support_set == ()
    assert selection.model_evidence_candidates


def test_role_title_without_assignment_is_not_direct_answer_support() -> None:
    """角色职责原文可供模型判断，但不能直接证明具体人员。"""
    candidates = _candidates(
        _paragraph("巡检负责人需要核对设备清单并归档检查记录。")
    )

    selection = EvidenceAssembler().assemble_sets(
        candidates,
        _POLICY,
        context=_context("巡检负责人是谁？"),
        include_model_candidates=True,
    )

    assert selection.answer_support_set == ()
    assert selection.model_evidence_candidates


@pytest.mark.parametrize(
    "question",
    [
        "档案受潮后应先做什么？",
        "档案受潮后应该先具体做什么？",
        "档案受潮后做什么？",
        "如果档案受潮，管理员做什么？",
    ],
)
def test_colloquial_action_preserves_conditions_and_sequence(
    question: str,
) -> None:
    """带条件或顺序的操作问法继续走事实支持，不能变成角色职责。"""
    context = _context(question)
    assert evaluate_span_support(context.analysis, "").answer_type == "FACT"


def test_first_action_question_keeps_its_supported_source() -> None:
    statement = "档案受潮时先转移材料，再通知管理员。"
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(statement)),
        _POLICY,
        context=_context("档案受潮后应先做什么？"),
    )
    assert [item.citation_text for item in evidence] == [statement]


@pytest.mark.parametrize(
    "question",
    (
        "设备入库流程有哪些步骤？",
        "设备入库流程是啥",
        "请把设备入库流程列出来",
    ),
)
def test_procedure_lead_in_keeps_the_complete_ordered_list(
    question: str,
) -> None:
    intro = "设备入库流程包括以下步骤："
    steps = ("核对交接清单。", "完成双人复核。", "按顺序登记入库。")
    candidates = _candidates(
        _paragraph(intro) + "".join(_list_paragraph(step) for step in steps)
    )

    evidence = EvidenceAssembler().assemble(
        candidates, _POLICY, context=_context(question)
    )

    assert [item.citation_text for item in evidence] == [intro, *steps]
    assert all(
        dict(item.metadata)["answer_support"]["support_reason"]
        == "STRUCTURED_LIST_RELATION"
        for item in evidence
    )
    assert not EvidenceAssembler().assemble(
        candidates, _POLICY, context=_context("设备出库流程有哪些步骤？")
    )


def test_enumeration_lead_in_does_not_publish_without_its_list() -> None:
    intro = "纸鸢团队的协作方式具体如下："
    methods = ("结对处理。", "集中会审。", "轮流值守。")
    candidates = _candidates(
        _paragraph(intro)
        + "".join(_list_paragraph(method) for method in methods)
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("纸鸢团队的协作方式是啥？"),
    )

    assert [item.citation_text for item in evidence] == [intro, *methods]
    assert not EvidenceAssembler().assemble(
        candidates[:1],
        _POLICY,
        context=_context("纸鸢团队的协作方式是啥？"),
    )


def test_ordinal_request_selects_only_the_requested_list_item() -> None:
    intro = "设备入库流程包括以下步骤："
    steps = ("核对交接清单。", "完成双人复核。", "按顺序登记入库。")
    candidates = _candidates(
        _paragraph(intro) + "".join(_list_paragraph(step) for step in steps)
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("设备入库流程的第三步是什么？"),
    )

    assert [item.citation_text for item in evidence] == [intro, steps[2]]


def test_document_stage_question_closes_all_numbered_sections() -> None:
    blocks = _paragraph("蓝熊软件全流程规范") + _heading("全流程管控要求")
    for number, title in enumerate(
        ("准入与启动", "设计与开发", "验收与归档"), 1
    ):
        blocks += _heading(f"3.{number} {title}", level=2)
        blocks += _paragraph(f"{title}阶段执行公开合成要求。")

    evidence = EvidenceAssembler().assemble(
        _candidates(blocks, display_name="蓝熊产品说明.docx"),
        _POLICY,
        context=_context("蓝熊软件全流程规范都分哪些阶段？"),
    )

    assert len(evidence) == 3
    assert [item.heading_path[-1] for item in evidence] == [
        "3.1 准入与启动",
        "3.2 设计与开发",
        "3.3 验收与归档",
    ]
    assert all(
        dict(item.metadata)["answer_support"]["support_reason"]
        == "SECTION_STAGE_SET"
        for item in evidence
    )
    assert all(item.source_spans[0].is_citable for item in evidence)


def test_missing_ordinal_stage_does_not_fall_back_to_all_stages() -> None:
    blocks = _paragraph("蓝熊交付全流程规范") + _heading("全流程管控要求")
    for number, title in enumerate(
        ("准入与启动", "设计与开发", "验收与归档"), 1
    ):
        blocks += _heading(f"3.{number} {title}", level=2)
        blocks += _paragraph(f"{title}阶段执行公开合成要求。")

    evidence = EvidenceAssembler().assemble(
        _candidates(blocks, display_name="蓝熊交付说明.docx"),
        _POLICY,
        context=_context("蓝熊交付流程的第九阶段是什么？"),
    )

    assert evidence == ()


def test_document_deliverables_use_section_body_structure() -> None:
    statement = "任务完成后必须输出两类交付物：①可运行演示；②操作说明。"
    evidence = EvidenceAssembler().assemble(
        _candidates(
            _heading("交付出口") + _paragraph(statement),
            display_name="蓝熊快速验证规范.docx",
        ),
        _POLICY,
        context=_context("蓝熊快速验证完成后要交哪些东西？"),
    )

    assert [item.citation_text for item in evidence] == [statement]
    assert (
        dict(evidence[0].metadata)["answer_support"]["support_reason"]
        == "SECTION_HEADING_BODY"
    )


def test_source_qualified_procedure_uses_matching_heading_and_body() -> None:
    statement = "先下载 CSV 模板，再填写测试用例，然后上传文件完成导入。"
    candidates = (
        *_candidates(
            _heading("测试用例导入") + _paragraph(statement),
            display_name="蓝熊操作手册.docx",
            document_id="doc_" + "7" * 32,
        ),
        *_candidates(
            _heading("测试用例导入")
            + _paragraph("先创建空白记录，再由管理员批量录入。"),
            display_name="白鹭操作手册.docx",
            document_id="doc_" + "8" * 32,
        ),
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("蓝熊手册中，测试用例如何导入？"),
    )

    assert [item.citation_text for item in evidence] == [statement]


@pytest.mark.parametrize(
    ("question", "statement", "answer_type"),
    (
        (
            "什么是星环协议",
            "星环协议是指用于同步设备状态的通信约定。",
            "DEFINITION",
        ),
        (
            "星环工程的目的是什么",
            "星环工程的目的是统一设备登记和交接记录。",
            "PURPOSE",
        ),
        (
            "星环登记表谁负责",
            "星环登记表由资料协调员负责维护。",
            "RESPONSIBLE_PARTY",
        ),
        (
            "第五章管理要求是什么",
            "第五章管理要求规定交接记录必须由双方复核。",
            "SECTION_SUMMARY",
        ),
    ),
)
def test_new_typed_relations_require_target_and_relation_support(
    question: str,
    statement: str,
    answer_type: str,
) -> None:
    evidence = EvidenceAssembler().assemble(
        _candidates(_paragraph(statement)),
        _POLICY,
        context=_context(question),
    )

    assert [item.citation_text for item in evidence] == [statement]
    support = dict(evidence[0].metadata)["answer_support"]
    assert support["answer_type"] == answer_type
    assert support["status"] == "SUPPORTED"


def test_new_typed_relations_do_not_borrow_another_relation() -> None:
    candidates = _candidates(
        _paragraph("星环登记表由资料协调员负责维护。")
        + _paragraph("星环登记表是指设备的进场确认单。")
    )

    responsible = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("星环登记表谁负责"),
    )
    definition = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("星环登记表是什么"),
    )

    assert [item.citation_text for item in responsible] == [
        "星环登记表由资料协调员负责维护。"
    ]
    assert [item.citation_text for item in definition] == [
        "星环登记表是指设备的进场确认单。"
    ]


def test_document_purpose_uses_heading_and_first_body_source() -> None:
    candidates = _candidates(
        _heading("目的") + _paragraph("统一设备交接记录，并保留双方复核结果。"),
        display_name="蓝熊工作规范.docx",
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("蓝熊工作规范的目的是什么"),
    )

    assert [item.citation_text for item in evidence] == [
        "统一设备交接记录，并保留双方复核结果。"
    ]
    assert (
        dict(evidence[0].metadata)["answer_support"]["support_reason"]
        == "SECTION_HEADING_BODY"
    )


def test_source_scoped_exact_section_keeps_its_complete_body_only() -> None:
    selected = (
        "出库前核对领料凭证。",
        "发放时由领发双方签字。",
        "完成后登记批次与数量。",
    )
    candidates = (
        *_candidates(
            _heading("出库领发")
            + "".join(_paragraph(item) for item in selected)
            + _heading("特殊放行", level=2)
            + _paragraph("特殊放行须另行审批。"),
            display_name="蓝熊管理制度.docx",
            document_id="doc_" + "a" * 32,
        ),
        *_candidates(
            _heading("出库领发") + _paragraph("白鹭仓库采用单人领料。"),
            display_name="白鹭管理制度.docx",
            document_id="doc_" + "b" * 32,
        ),
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("根据《蓝熊管理制度》，出库领发具体有哪些要求？"),
    )

    assert [item.citation_text for item in evidence] == list(selected)
    assert all(item.display_name == "蓝熊管理制度.docx" for item in evidence)
    supports = [dict(item.metadata)["answer_support"] for item in evidence]
    assert all(
        item["support_reason"] == "SECTION_HEADING_BODY" for item in supports
    )
    assert all(len(item["supporting_span_ids"]) == 3 for item in supports)


def test_source_scoped_restriction_requires_target_in_the_clause() -> None:
    selected = "已审核的任务禁止手动修改。"
    evidence = EvidenceAssembler().assemble(
        _candidates(
            _paragraph("任务执行中不得断电。") + _paragraph(selected),
            display_name="蓝熊管理制度.docx",
        ),
        _POLICY,
        context=_context(
            "依据《蓝熊管理制度》，文档对“手动修改”有什么禁止或限制性要求？"
        ),
    )

    assert [item.citation_text for item in evidence] == [selected]


def test_source_scoped_quoted_explanation_accepts_the_exact_source_clause() -> (
    None
):
    selected = "注：品质部为公司质量管理部门。"
    evidence = EvidenceAssembler().assemble(
        _candidates(
            _paragraph(selected),
            display_name="GM-01 质量管理机构图.docx",
        ),
        _POLICY,
        context=_context(
            "根据《GM-01 质量管理机构图》，"
            "文档对“注：品质部为公司质量管理部门”作了什么具体说明？"
        ),
    )

    assert [item.citation_text for item in evidence] == [selected]


def test_source_scoped_table_row_returns_all_non_label_cells() -> None:
    values = ("扣减两分。", "三个工作日内整改。", "保留复核记录。")
    blocks = (
        _heading("安全事故考核")
        + "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/>"
        "<w:gridCol/></w:tblGrid><w:tr><w:tc>"
        + _paragraph("等级")
        + "</w:tc><w:tc>"
        + _paragraph("扣分")
        + "</w:tc><w:tc>"
        + _paragraph("处理")
        + "</w:tc><w:tc>"
        + _paragraph("记录")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + _paragraph("一般")
        + "</w:tc><w:tc>"
        + _paragraph(values[0])
        + "</w:tc><w:tc>"
        + _paragraph(values[1])
        + "</w:tc><w:tc>"
        + _paragraph(values[2])
        + "</w:tc></w:tr></w:tbl>"
    )

    evidence = EvidenceAssembler().assemble(
        _candidates(blocks, display_name="蓝熊管理制度.docx"),
        _POLICY,
        context=_context(
            "根据《蓝熊管理制度》的“安全事故考核”，“一般”对应的内容或要求是什么？"
        ),
    )

    assert {item.citation_text for item in evidence} == set(values)
    assert len(evidence) == len(values)
    supports = [dict(item.metadata)["answer_support"] for item in evidence]
    assert all(
        item["support_reason"] == "TABLE_ROW_CONTENT" for item in supports
    )
    assert all(
        len(item["supporting_span_ids"]) == len(values) for item in supports
    )


def test_document_purpose_prefers_shallow_scope_over_local_purpose() -> None:
    candidates = _candidates(
        _heading("规范目的")
        + _paragraph("统一公开设备的交接记录。")
        + _heading("实施管理")
        + _heading("局部任务目的", level=2)
        + _paragraph("缩短单次演示的准备时间。"),
        display_name="蓝熊工作规范.docx",
    )
    candidates = tuple(
        sorted(
            candidates,
            key=lambda item: len(item.hydrated.chunk.heading_path),
            reverse=True,
        )
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("蓝熊工作规范主要是为了什么"),
    )

    assert [item.citation_text for item in evidence] == [
        "统一公开设备的交接记录。"
    ]


def test_document_purpose_heading_beats_same_depth_incidental_word() -> None:
    candidates = _candidates(
        _heading("概述")
        + _heading("目的", level=2)
        + _paragraph("面向一次性公开交付任务，统一验收和归档边界。")
        + _heading("实施要求")
        + _heading("启动检查", level=2)
        + _paragraph("启动检查的作用是确认本次资源是否齐备。"),
        display_name="青鸟交付工作规范.docx",
    )
    candidates = tuple(reversed(candidates))

    evidence = EvidenceAssembler().assemble(
        candidates,
        _POLICY,
        context=_context("青鸟交付工作规范主要是为了什么"),
    )

    assert [item.citation_text for item in evidence] == [
        "面向一次性公开交付任务，统一验收和归档边界。"
    ]
