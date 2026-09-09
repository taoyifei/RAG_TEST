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
