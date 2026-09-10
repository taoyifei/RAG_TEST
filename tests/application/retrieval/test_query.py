from __future__ import annotations

import pytest

from rag_app.application.retrieval import QueryAnalyzer, QueryPlanner
from rag_app.application.retrieval.answer_support import (
    SupportStatus,
    evaluate_span_support,
)
from rag_app.application.retrieval.expansion import RuleBasedNormalizer
from rag_app.application.retrieval.lexical import question_search_terms
from rag_app.core.models import (
    KnowledgeBaseScope,
    QueryAnalysis,
    QueryKind,
    RequestedAnswerType,
    RetrievalPolicy,
    SearchRequest,
)

_SCOPE = KnowledgeBaseScope(
    project_id=f"prj_{'1' * 32}",
    knowledge_base_id=f"kb_{'2' * 32}",
)


def _analyze(text: str) -> QueryAnalysis:
    return QueryAnalyzer().analyze(SearchRequest(scope=_SCOPE, text=text))


def test_analyzer_preserves_mixed_width_numeric_and_negation_signals() -> None:
    analysis = _analyze(
        "查表格“额定值”中 ＧＢ／Ｔ 19001-2016 不得低于 -12.5％，日期 2026-09-03"
    )

    assert analysis.normalized_query.startswith("查表格")
    assert "额定值" in analysis.quoted_phrases
    assert any("GB/T 19001-2016" in value for value in analysis.identifiers)
    assert "-12.5%" in analysis.numbers
    assert "不得" in analysis.negation_signals
    assert "zh" in analysis.language_hints
    assert analysis.structural_table_signals


@pytest.mark.parametrize("value", ("2026", "138-0013-8000", "3.14159"))
def test_analyzer_does_not_treat_plain_numbers_as_identifiers(
    value: str,
) -> None:
    assert _analyze(value).identifiers == ()


def test_analyzer_preserves_mixed_script_identifier() -> None:
    analysis = _analyze("MiXeD-部件-7 向量检索")

    assert analysis.identifiers == ("MiXeD-部件-7",)
    assert any(
        constraint.normalized_value == "mixed-部件-7"
        for constraint in analysis.semantics.constraints
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("订单号 ABC-123 是什么", QueryKind.EXACT_IDENTIFIER),
        ("交付地点在哪里", QueryKind.SIMPLE_FACT),
        ("表格第几行是 12 kg", QueryKind.TABLE_NUMERIC),
        ("它", QueryKind.AMBIGUOUS),
        ("比较方案一和方案二的影响", QueryKind.COMPLEX),
    ),
)
def test_planner_covers_five_query_kinds(
    text: str, expected: QueryKind
) -> None:
    analysis = _analyze(text)
    variants = RuleBasedNormalizer().expand(analysis)
    plan = QueryPlanner().plan(analysis, variants, RetrievalPolicy())

    assert plan.query_kind is expected
    assert plan.variants[0].text == text
    assert len(plan.variants) <= 2
    assert plan.channels


def test_search_request_bounds_conversation_context() -> None:
    with pytest.raises(ValueError):
        SearchRequest(
            scope=_SCOPE,
            text="query",
            conversation_context=tuple(str(index) for index in range(9)),
        )


@pytest.mark.parametrize(
    (
        "question",
        "target",
        "relation",
        "answer_type",
        "expected_count",
        "ordinal",
    ),
    (
        (
            "蓝鹊小组的三种工作模式是什么",
            "蓝鹊小组",
            "工作模式",
            RequestedAnswerType.ENUMERATION,
            3,
            None,
        ),
        (
            "蓝鹊小组的三种工作模式是啥？",
            "蓝鹊小组",
            "工作模式",
            RequestedAnswerType.ENUMERATION,
            3,
            None,
        ),
        (
            "蓝鹊小组是哪三种工作模式",
            "蓝鹊小组",
            "工作模式",
            RequestedAnswerType.ENUMERATION,
            3,
            None,
        ),
        (
            "蓝鹊小组有多少种工作模式",
            "蓝鹊小组",
            "工作模式",
            RequestedAnswerType.COUNT,
            None,
            None,
        ),
        (
            "蓝鹊小组的第三种工作模式是什么",
            "蓝鹊小组",
            "工作模式",
            RequestedAnswerType.ORDINAL_ITEM,
            None,
            3,
        ),
        (
            "请把蓝鹊小组的工作模式列出来",
            "蓝鹊小组",
            "工作模式",
            RequestedAnswerType.ENUMERATION,
            None,
            None,
        ),
        (
            "蓝鹊小组的工作模式有三种，分别是什么",
            "蓝鹊小组",
            "工作模式",
            RequestedAnswerType.ENUMERATION,
            3,
            None,
        ),
        (
            "蓝鹊小组的三种工作模式具体是怎么说的",
            "蓝鹊小组",
            "工作模式",
            RequestedAnswerType.ENUMERATION,
            3,
            None,
        ),
        (
            "设备入库流程有哪些步骤？",
            "设备入库",
            "流程",
            RequestedAnswerType.PROCEDURE,
            None,
            None,
        ),
        (
            "设备入库流程的第三步是什么？",
            "设备入库",
            "流程",
            RequestedAnswerType.ORDINAL_ITEM,
            None,
            3,
        ),
        (
            "项目交付流程的第九阶段是什么？",
            "项目交付",
            "流程",
            RequestedAnswerType.ORDINAL_ITEM,
            None,
            9,
        ),
        (
            "设备入库流程有多少步骤？",
            "设备入库",
            "流程",
            RequestedAnswerType.COUNT,
            None,
            None,
        ),
    ),
)
def test_analyzer_builds_shared_descriptive_semantics(  # noqa: PLR0913, PLR0917
    question: str,
    target: str,
    relation: str,
    answer_type: RequestedAnswerType,
    expected_count: int | None,
    ordinal: int | None,
) -> None:
    analysis = _analyze(question)

    assert analysis.resolved_query == analysis.normalized_query
    assert analysis.semantics.target == target
    assert analysis.semantics.relation == relation
    assert analysis.semantics.answer_type is answer_type
    assert analysis.semantics.expected_count == expected_count
    assert analysis.semantics.ordinal == ordinal


def test_plain_process_keywords_remain_literal_lookup() -> None:
    """没有问句槽位的“流程”是搜索词，不能被误判成步骤问答。"""
    analysis = _analyze("青岛啤酒采购流程")

    assert analysis.semantics.answer_type is RequestedAnswerType.UNKNOWN
    assert (
        evaluate_span_support(
            analysis, "青岛啤酒采购流程使用公开合成文本。"
        ).status
        is SupportStatus.SUPPORTED
    )


def test_duty_query_preserves_dynamic_source_qualifier() -> None:
    analysis = _analyze("蓝熊规范中项目经理具体负责哪些工作")

    assert analysis.semantics.target == "项目经理"
    assert analysis.semantics.source_qualifier == "蓝熊规范"
    assert analysis.semantics.answer_type is RequestedAnswerType.DUTIES
    assert (
        question_search_terms(analysis.normalized_query, analysis.semantics)
        == "蓝熊规范 项目经理"
    )


@pytest.mark.parametrize(
    "name",
    (
        "美的中心",
        "开发中心",
        "研发和质量组",
        "“啥都有”研究组",
        "API-Gateway",
    ),
)
def test_analyzer_does_not_strip_question_words_inside_entity_names(
    name: str,
) -> None:
    analysis = _analyze(f"{name}的工作模式是啥")

    assert analysis.semantics.target == name


def test_analyzer_strips_only_boundary_discourse_particles() -> None:
    analysis = _analyze("蓝鹊小组嘛，都有啥工作模式呀？")

    assert analysis.semantics.target == "蓝鹊小组"
    assert analysis.semantics.answer_type is RequestedAnswerType.ENUMERATION


@pytest.mark.parametrize(
    ("question", "target", "relation", "answer_type"),
    (
        ("什么是OPC", "OPC", "定义", RequestedAnswerType.DEFINITION),
        ("OPC是什么", "OPC", "定义", RequestedAnswerType.DEFINITION),
        ("啥是OPC", "OPC", "定义", RequestedAnswerType.DEFINITION),
        ("OPC是啥", "OPC", "定义", RequestedAnswerType.DEFINITION),
        (
            "蓝熊工作规范的目的是什么",
            "蓝熊工作规范",
            "目的",
            RequestedAnswerType.PURPOSE,
        ),
        (
            "流程控制的作用是什么",
            "流程控制",
            "作用",
            RequestedAnswerType.PURPOSE,
        ),
        (
            "项目负责人是干嘛的",
            "项目负责人",
            "职责",
            RequestedAnswerType.DUTIES,
        ),
        (
            "测试负责人干啥的",
            "测试负责人",
            "职责",
            RequestedAnswerType.DUTIES,
        ),
        (
            "项目报备登记表谁负责",
            "项目报备登记表",
            "责任角色",
            RequestedAnswerType.RESPONSIBLE_PARTY,
        ),
        (
            "谁牵头蓝熊迁移",
            "蓝熊迁移",
            "责任角色",
            RequestedAnswerType.RESPONSIBLE_PARTY,
        ),
        (
            "项目交付全流程有哪些",
            "项目交付全流程",
            "主要阶段",
            RequestedAnswerType.ENUMERATION,
        ),
        (
            "第五章管理要求是什么",
            "第五章管理要求",
            "章节内容",
            RequestedAnswerType.SECTION_SUMMARY,
        ),
    ),
)
def test_analyzer_supports_general_typed_question_semantics(
    question: str,
    target: str,
    relation: str,
    answer_type: RequestedAnswerType,
) -> None:
    semantics = _analyze(question).semantics

    assert semantics.target == target
    assert semantics.relation == relation
    assert semantics.answer_type is answer_type
    assert semantics.source == "RULE"


def test_typed_semantics_preserve_internal_question_characters() -> None:
    semantics = _analyze("谁的咖啡由谁负责").semantics

    assert semantics.target == "谁的咖啡"
    assert semantics.answer_type is RequestedAnswerType.RESPONSIBLE_PARTY


def test_document_target_does_not_invent_an_explicit_source_qualifier() -> None:
    semantics = _analyze("蓝熊工作规范的目的是什么").semantics

    assert semantics.source_qualifier is None


def test_duty_question_preserves_leading_project_context() -> None:
    semantics = _analyze(
        "做蓝熊交付项目时，测试负责人平时主要管哪些事？"
    ).semantics

    assert semantics.target == "测试负责人"
    assert semantics.context_qualifier == "蓝熊交付项目"
    assert semantics.source_qualifier is None


@pytest.mark.parametrize(
    ("question", "target", "relation", "answer_type", "source"),
    (
        (
            "蓝熊工作规范主要是为了什么？",
            "蓝熊工作规范",
            "目的",
            RequestedAnswerType.PURPOSE,
            None,
        ),
        (
            "蓝熊规范里，项目经理平时都要干些啥？",
            "项目经理",
            "职责",
            RequestedAnswerType.DUTIES,
            "蓝熊规范",
        ),
        (
            "蓝熊规范里，交付登记表这件事到底谁负责？",
            "交付登记表",
            "责任角色",
            RequestedAnswerType.RESPONSIBLE_PARTY,
            "蓝熊规范",
        ),
        (
            "设备迁移从启动一直到归档，都要走哪些阶段？",
            "设备迁移",
            "主要阶段",
            RequestedAnswerType.ENUMERATION,
            None,
        ),
        (
            "快速验证做完以后，必须交哪些东西才能闭环？",
            "快速验证",
            "交付物",
            RequestedAnswerType.ENUMERATION,
            None,
        ),
        (
            "我手上有个 CSV，要怎么把测试用例导进去？",
            "测试用例",
            "导入",
            RequestedAnswerType.PROCEDURE,
            None,
        ),
        (
            "蓝熊手册中，测试用例如何导入？",
            "测试用例",
            "导入",
            RequestedAnswerType.PROCEDURE,
            "蓝熊手册",
        ),
        (
            "蓝熊模式文档里，快速验证说白了是啥？",
            "快速验证",
            "定义",
            RequestedAnswerType.DEFINITION,
            "蓝熊模式文档",
        ),
        (
            "测试负责人平时主要管哪些事？",
            "测试负责人",
            "职责",
            RequestedAnswerType.DUTIES,
            None,
        ),
        (
            "做交付项目时，测试负责人平时主要管哪些事？",
            "测试负责人",
            "职责",
            RequestedAnswerType.DUTIES,
            None,
        ),
    ),
)
def test_analyzer_handles_natural_spoken_typed_questions(
    question: str,
    target: str,
    relation: str,
    answer_type: RequestedAnswerType,
    source: str | None,
) -> None:
    semantics = _analyze(question).semantics

    assert semantics.target == target
    assert semantics.relation == relation
    assert semantics.answer_type is answer_type
    assert semantics.source_qualifier == source
    assert semantics.source == "RULE"
