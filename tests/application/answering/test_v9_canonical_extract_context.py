"""合法表格交点摘录必须保留已认证的行名、列头和值。"""

from dataclasses import asdict, replace
from unittest.mock import Mock

import pytest

from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    _safe_extractive_fallback,
    _validated_natural_claim,
)
from rag_app.application.retrieval.evidence import (
    _table_intersection_certificate,
)
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import (
    AnswerClaim,
    ClaimSupport,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    QueryAnalysis,
    QuerySemantics,
    RequestedAnswerType,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.evidence_group import EvidenceGroup
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    QueryAtom,
    QueryPlan,
    make_query_plan,
)
from rag_app.core.models.retrieval import NaturalClaim
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _pack,
    _table_cell,
)
from tests.application.answering.test_natural_grounded_answer import _matrix
from tests.application.answering.test_v9_target_coverage_service import (
    _material,
)
from tests.application.retrieval.test_evidence_table_coordinates import _context


def _certified_case(
    mutation: str | None,
) -> tuple[QueryPlan, tuple[EvidenceItem, ...], EvidenceGroup]:
    """复用真实交点认证，保持原文与身份可独立核查。"""
    base, group = _material()
    evidence = (base[3], base[2], base[6])
    query = "开发团队的交付成果有哪些？"
    context = _context(query)
    context = context.model_copy(
        update={
            "analysis": context.analysis.model_copy(
                update={
                    "semantics": QuerySemantics(
                        target="开发团队",
                        relation="交付成果",
                        answer_type=RequestedAnswerType.ENUMERATION,
                    )
                }
            )
        }
    )
    cells = {}
    for coordinate, item in zip(
        ((1, 0), (0, 2), (1, 2)), evidence, strict=True
    ):
        span = item.source_spans[0]
        cells[coordinate] = {
            (
                item.chunk_id,
                span.node_id,
                span.source_start_char,
                span.source_end_char,
                span.span_type.value,
            ): item.citation_text
        }
    proof = _table_intersection_certificate(context, cells, 1, 2)
    assert proof is not None
    certificate = asdict(proof[1])
    certificate["status"] = proof[1].status.value
    certificate["supporting_span_ids"] = list(proof[1].supporting_span_ids)
    evidence = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        "evidence_group_id": group.group_id,
                        "evidence_group_type": "TABLE_ROW_GROUP",
                        "group_complete": True,
                        "answer_support": certificate,
                    }
                )
            }
        )
        for item in evidence
    )
    atom = QueryAtom(
        atom_id="A1",
        target="开发团队",
        relation="交付成果",
        answer_shape=AtomAnswerShape.ENUMERATION,
        original_fragment=query,
    )
    if mutation == "missing_header":
        evidence = (evidence[0], evidence[2])
    elif mutation == "wrong_row":
        evidence = (*evidence[:2], _table_cell(evidence[-1], 2, 2))
    elif mutation == "wrong_column":
        evidence = (*evidence[:2], _table_cell(evidence[-1], 1, 1))
    elif mutation == "wrong_atom":
        atom = atom.model_copy(update={"target": "运营团队"})
    elif mutation == "unknown_atom":
        atom = atom.model_copy(update={"target": "开发团队要弄啥？"})
    plan = make_query_plan(
        standalone_query=query,
        atoms=(atom,),
        intent="FACT",
        effort="DIRECT",
        reason_code="SYNTHETIC",
        planner_called=False,
    )
    return plan, evidence, group


@pytest.mark.parametrize(
    "mutation",
    (
        None,
        "missing_header",
        "wrong_row",
        "wrong_column",
        "wrong_atom",
        "unknown_atom",
    ),
)
def test_canonical_intersection_excerpt_preserves_all_three_supports(
    mutation: str | None,
) -> None:
    """拆成单个单元格会失败，完整原有交点可通过同一事实门。"""
    plan, evidence, group = _certified_case(mutation)
    ids = tuple(item.support_id for item in evidence)
    matrix = _matrix(plan, ((AtomStatus.PARTIAL, ids),))
    accepted: list[AnswerClaim] = []

    def validate(claim: AnswerClaim) -> bool:
        try:
            _validated_natural_claim(
                NaturalClaim(
                    atom_id="A1", text=claim.text, supports=claim.supports
                ),
                plan,
                matrix,
                evidence,
                None,
                trusted_groups=(group,),
            )
        except ValidationFailed:
            return False
        accepted.append(claim)
        return True

    whole = AnswerClaim(
        text=evidence[-1].citation_text,
        supports=tuple(
            ClaimSupport(support_id=item.support_id, quote=item.citation_text)
            for item in evidence
        ),
    )
    assert validate(whole) is (mutation is None)
    accepted.clear()
    result = _safe_extractive_fallback(
        plan,
        evidence,
        {"A1": ids},
        (group.group_id,),
        require_named_row=True,
        validate_claim=validate,
        validate_atom_claim=lambda claim, _atom_id: validate(claim),
    )
    if mutation is not None:
        assert result is None
        assert not accepted
        return
    assert result is not None
    assert set(result[1]) == set(ids)
    assert result[2] == frozenset({"A1"})
    assert "测试报告" in result[0]
    assert len(accepted) == 1
    assert {support.support_id for support in accepted[0].supports} == set(ids)


def test_certified_excerpt_keeps_each_atom_source_scope() -> None:
    """A2 的正确来源不能让继承另一根来源限制的 A1 也算已回答。"""
    plan, evidence, group = _certified_case(None)
    group = group.model_copy(update={"display_name": "乙手册"})
    evidence = tuple(
        item.model_copy(
            update={"source_label": "乙手册", "display_name": "乙手册"}
        )
        for item in evidence
    )
    first = plan.atoms[0]
    second = first.model_copy(
        update={"atom_id": "A2", "source_qualifier": "乙手册"}
    )
    plan = plan.model_copy(update={"atoms": (first, second)})
    ids = tuple(item.support_id for item in evidence)
    pack = replace(
        _pack(plan, evidence),
        trusted_source_groups=(group,),
        complete_group_ids=(group.group_id,),
    )
    generator = Mock()
    outcome = GroundedAnsweringService(generator).answer(
        plan.original_query,
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=plan,
        atom_support_matrix=_matrix(
            plan, ((AtomStatus.PARTIAL, ids), (AtomStatus.PARTIAL, ids))
        ),
        generation_evidence_pack=pack,
        analysis=QueryAnalysis(
            original_query=plan.original_query,
            normalized_query=plan.original_query,
            conversation_fingerprint="sha256:" + "0" * 64,
            semantics=QuerySemantics(
                target="开发团队",
                relation="交付成果",
                source_qualifier="甲手册",
            ),
        ),
    )
    assert dict(outcome.atom_coverage)["A1"] == "MISSING"
    assert dict(outcome.atom_coverage)["A2"] == "SUPPORTED"
    assert outcome.answer is not None
    assert outcome.answer.count("测试报告") == 1
    generator.generate.assert_not_called()
