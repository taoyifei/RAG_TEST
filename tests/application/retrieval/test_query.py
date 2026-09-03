from __future__ import annotations

import pytest

from rag_app.application.retrieval import QueryAnalyzer, QueryPlanner
from rag_app.application.retrieval.expansion import RuleBasedNormalizer
from rag_app.core.models import (
    KnowledgeBaseScope,
    QueryAnalysis,
    QueryKind,
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
        '查表格“额定值”中 ＧＢ／Ｔ 19001-2016 不得低于 -12.5％，日期 2026-09-03'
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
