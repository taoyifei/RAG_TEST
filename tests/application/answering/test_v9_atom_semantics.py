"""同一 Atom 的语义对象、三态判定与请求隔离回归。"""

from __future__ import annotations

import pytest

from rag_app.application.answering.atom_semantics import current_atom_analysis
from rag_app.application.answering.grounded import _validated_natural_claim
from rag_app.application.answering.request_relation import (
    RequestRelationStatus,
    RequestRelationUndetermined,
    decide_request_relation,
    has_relation_review,
    record_relation_review,
    relation_review_scope,
)
from rag_app.core.models import QueryAnalysis, QuerySemantics
from rag_app.core.models.query_plan import AtomStatus
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _evidence,
    _matrix,
    _plan,
)


@pytest.mark.parametrize("provide_analysis", [False, True])
def test_full_clause_uses_identical_atom_semantics(
    provide_analysis: bool,
) -> None:
    plan = _plan("谁负责制定考核标准和频次？")
    source = "各指标牵头部门负责制定考核标准、考核分数和考核频次。"
    analysis = (
        QueryAnalysis(
            original_query=plan.original_query,
            normalized_query=plan.original_query,
            conversation_fingerprint="sha256:" + "0" * 64,
        )
        if provide_analysis
        else None
    )
    claim = _validated_natural_claim(
        _claim("C1", source, "A1", "S1"),
        plan,
        _matrix(plan, ((AtomStatus.MISSING, ()),)),
        _evidence(source),
        analysis,
    )
    assert claim.text == source


def test_unknown_parser_does_not_prove_irrelevance() -> None:
    atom = _plan("这个该咋整呀？").atoms[0]
    result = decide_request_relation(
        current_atom_analysis(atom, None), "管理员负责核对记录。"
    )
    assert result.status is RequestRelationStatus.UNDETERMINED
    assert result.reason == "LEXICAL_RELATION_NOT_PROVED"


def test_explicit_different_subject_is_hard_irrelevant() -> None:
    atom = _plan("甲部门").atoms[0]
    result = decide_request_relation(
        current_atom_analysis(atom, None), "乙部门负责保存记录。"
    )
    assert result.status is RequestRelationStatus.CONTRADICTED_OR_IRRELEVANT


def test_subquestion_does_not_inherit_other_atoms_number() -> None:
    atom = _plan("乙部门").atoms[0]
    analysis = QueryAnalysis(
        original_query="甲部门保存14天；乙部门职责？",
        normalized_query="甲部门保存14天；乙部门职责？",
        numbers=("14",),
        units=("天",),
        semantics=QuerySemantics(target="甲部门", context_qualifier="验收后"),
        conversation_fingerprint="sha256:" + "0" * 64,
    )
    derived = current_atom_analysis(atom, analysis)
    assert derived.numbers == ()
    assert derived.units == ()
    assert derived.semantics.context_qualifier is None
    assert derived.original_query == analysis.original_query


def test_equivalent_analysis_preserves_trusted_source_and_context() -> None:
    atom = _plan("甲部门").atoms[0]
    analysis = QueryAnalysis(
        original_query="验收后甲部门职责？",
        normalized_query="验收后甲部门职责？",
        semantics=QuerySemantics(
            target="甲部门",
            source_qualifier="审核手册",
            context_qualifier="验收后",
        ),
        conversation_fingerprint="sha256:" + "1" * 64,
    )
    derived = current_atom_analysis(atom, analysis)
    assert derived.semantics.source_qualifier == "审核手册"
    assert derived.semantics.context_qualifier == "验收后"
    assert derived.conversation_fingerprint == analysis.conversation_fingerprint


def test_relation_signal_never_leaks_across_requests() -> None:
    @relation_review_scope
    def first() -> None:
        record_relation_review("first")
        assert has_relation_review("first")

    @relation_review_scope
    def second() -> None:
        assert not has_relation_review("first")

    first()
    second()
    assert not has_relation_review("first")


def test_unknown_keeps_original_claim_without_publishing() -> None:
    source = "管理员负责核对记录。"
    plan = _plan("这个该咋整呀？")
    with pytest.raises(RequestRelationUndetermined) as error:
        _validated_natural_claim(
            _claim("C1", source, "A1", "S1"),
            plan,
            _matrix(plan, ((AtomStatus.MISSING, ()),)),
            _evidence(source),
            None,
        )
    assert error.value.claim.text == source
    assert "管理员" not in str(error.value.details)
