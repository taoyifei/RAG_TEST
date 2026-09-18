"""Span Reference Atom 必须以本原子的对象和关系检查来源。"""

from __future__ import annotations

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.answer_support import (
    SupportStatus,
    evaluate_span_support,
)
from rag_app.core.models import (
    KnowledgeBaseScope,
    QueryAnalysis,
    RequestedAnswerType,
    SearchRequest,
)


def _atom_analysis(
    relation: str, answer_type: RequestedAnswerType
) -> QueryAnalysis:
    request = SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=f"prj_{'1' * 32}",
            knowledge_base_id=f"kb_{'2' * 32}",
        ),
        text=f"甲设备 {relation}",
    )
    analysis = QueryAnalyzer().analyze(request)
    return analysis.model_copy(
        update={
            "semantics": analysis.semantics.model_copy(
                update={
                    "target": "甲设备",
                    "relation": relation,
                    "answer_type": answer_type,
                    "source": "SPAN_REFERENCED",
                }
            )
        }
    )


def test_atom_duration_does_not_borrow_another_duration_relation() -> None:
    analysis = _atom_analysis("维护期限", RequestedAnswerType.DURATION)

    assert (
        evaluate_span_support(analysis, "甲设备维护期限为五天。").status
        is SupportStatus.SUPPORTED
    )
    assert (
        evaluate_span_support(analysis, "甲设备采购期限为五天。").status
        is not SupportStatus.SUPPORTED
    )


def test_atom_fact_does_not_borrow_another_action() -> None:
    analysis = _atom_analysis("复核", RequestedAnswerType.FACT)

    assert (
        evaluate_span_support(analysis, "甲设备复核由甲岗负责。").status
        is SupportStatus.SUPPORTED
    )
    assert (
        evaluate_span_support(analysis, "甲设备登记由甲岗负责。").status
        is not SupportStatus.SUPPORTED
    )
