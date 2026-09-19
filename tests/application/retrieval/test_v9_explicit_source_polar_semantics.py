"""明确来源和极性问句只解释任务，不为事实或来源提供证明。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.answer_support import (
    SupportStatus,
    evaluate_span_support,
)
from rag_app.application.retrieval.semantics import (
    parse_query_semantics,
    split_explicit_source_scope,
)
from rag_app.core.models import QueryAnalysis, RequestedAnswerType


@pytest.mark.parametrize(
    "preposition", ["根据", "依据", "依照", "按照", "参照"]
)
def test_unquoted_preposition_document_preserves_source_and_body(
    preposition: str,
) -> None:
    query = f"{preposition}甲手册，费用可以报销吗？"
    source, body, start = split_explicit_source_scope(query)
    assert source == "甲手册"
    assert body == "费用可以报销吗？"
    assert query[start:] == body
    semantics = parse_query_semantics(query)
    assert semantics.source_qualifier == "甲手册"
    assert semantics.target == "费用"
    assert semantics.relation == "报销"


def test_source_prefix_with_polite_request_keeps_correct_offset() -> None:
    query = "请问根据设备维护规范,甲部门要归档吗？"
    source, body, start = split_explicit_source_scope(query)
    assert source == "设备维护规范"
    assert query[start:] == body == "甲部门要归档吗？"


@pytest.mark.parametrize(
    "query",
    [
        "根据实际情况，甲部门要归档吗？",
        "甲部门编写甲手册，乙部门要归档吗？",
        "根据甲手册的名称可以推测流程吗？",
        "根据甲手册费用可以报销吗？",
    ],
)
def test_non_source_or_unbounded_preamble_is_not_a_document_qualifier(
    query: str,
) -> None:
    source, body, start = split_explicit_source_scope(query)
    assert source is None
    assert body == query
    assert start == 0


@pytest.mark.parametrize(
    "query,target,relation",
    [
        ("甲部门要归档吗？", "甲部门", "归档"),
        ("乙团队需要核对记录吗", "乙团队", "核对记录"),
        ("管理人员是否可以提交材料", "管理人员", "提交材料"),
        ("申请人能否登记", "申请人", "登记"),
        ("甲部门可以不归档吗？", "甲部门", "不归档"),
    ],
)
def test_polar_question_retains_explicit_subject_and_action(
    query: str, target: str, relation: str
) -> None:
    semantics = parse_query_semantics(query)
    assert semantics.target == target
    assert semantics.relation == relation
    assert semantics.answer_type is RequestedAnswerType.FACT
    assert semantics.source == "RULE"


@pytest.mark.parametrize(
    "query", ["甲部门要归档。", "是否可以归档？", "甲部门主要内容是什么？"]
)
def test_statement_missing_subject_and_open_question_do_not_use_polar_rule(
    query: str,
) -> None:
    assert (
        "POLAR_SUBJECT_ACTION_QUESTION_SYNTAX"
        not in parse_query_semantics(query).reason_codes
    )


def test_parsed_question_does_not_prove_another_subjects_fact() -> None:
    query = "甲部门要归档吗？"
    semantics = parse_query_semantics(query)
    analysis = QueryAnalysis(
        original_query=query,
        normalized_query=query,
        semantics=semantics,
        conversation_fingerprint="sha256:" + "0" * 64,
    )
    assert (
        evaluate_span_support(analysis, "乙部门负责归档记录。").status
        is not SupportStatus.SUPPORTED
    )
