"""通过真实服务验证最终职责成员覆盖与 SAFE 身份，不替换验证结果。"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from unittest.mock import Mock

from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    GroundedOutcome,
)
from rag_app.application.retrieval.evidence import (
    EvidenceAssembler,
    _table_intersection_certificate,
)
from rag_app.core.models import (
    ClaimSupport,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    QueryAnalysis,
    QuerySemantics,
    RequestedAnswerType,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.evidence_group import EvidenceGroup, EvidenceGroupKind
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    QueryAtom,
    make_query_plan,
)
from tests.application.answering.source_contract_fixtures import (
    trusted_list_group,
)
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _pack,
    _table_cell,
)
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _draft,
    _matrix,
)
from tests.application.retrieval.test_descriptive_answers import (
    _POLICY,
    _candidates,
    _paragraph,
)
from tests.application.retrieval.test_evidence_table_coordinates import _context


def _material() -> tuple[tuple[EvidenceItem, ...], EvidenceGroup]:
    specs = (
        (0, 0, "角色"),
        (0, 1, "主要职责"),
        (0, 2, "交付成果"),
        (1, 0, "开发团队"),
        (1, 1, "开发团队负责设计与开发。"),
        (1, 1, "开发团队负责现场调试与问题修复。"),
        (1, 2, "开发团队提供测试报告。"),
        (2, 0, "运营团队"),
        (2, 1, "运营团队负责联系客户。"),
    )
    candidates = _candidates("".join(_paragraph(text) for _, _, text in specs))
    by_text = {
        item.citation_text: item
        for item in EvidenceAssembler().assemble(
            candidates,
            _POLICY.model_copy(
                update={
                    "max_evidence_items": 16,
                    "max_evidence_items_per_chunk": 16,
                    "per_document_cap": 16,
                    "per_section_cap": 16,
                }
            ),
        )
    }
    evidence = tuple(
        _table_cell(by_text[text], row, column) for row, column, text in specs
    )
    group = trusted_list_group(evidence).model_copy(
        update={
            "kind": EvidenceGroupKind.TABLE_ROW_GROUP,
            "heading_path": ("协同方案", "首单交付阶段"),
            "metadata": freeze_json_object(
                {
                    "canonical_header_node_ids": [
                        item.source_spans[0].node_id for item in evidence[:3]
                    ],
                }
            ),
        }
    )
    return evidence, group


def _run(  # noqa: PLR0913
    evidence: tuple[EvidenceItem, ...],
    groups: tuple[EvidenceGroup, ...],
    facts: tuple[EvidenceItem, ...],
    *,
    target: str = "开发团队",
    relation: str = "职责",
    shape: AtomAnswerShape = AtomAnswerShape.DUTIES,
    context_supports: tuple[EvidenceItem, ...] = (),
    stage: str | None = None,
) -> tuple[GroundedOutcome, Mock]:
    atom = QueryAtom(
        atom_id="A1",
        target=target,
        relation=relation,
        answer_shape=shape,
        original_fragment=f"{stage or ''}{target}的{relation}有哪些？",
    )
    plan = make_query_plan(
        standalone_query=atom.original_fragment,
        atoms=(atom,),
        intent="FACT",
        effort="DIRECT",
        reason_code="SYNTHETIC",
        planner_called=False,
    )
    generator = Mock()
    generator.generate.return_value = _draft(
        tuple(
            _claim(
                f"C{index}", item.citation_text, atom.atom_id, item.support_id
            ).model_copy(
                update={
                    "supports": tuple(
                        ClaimSupport(
                            support_id=proof.support_id,
                            quote=proof.citation_text,
                        )
                        for proof in (*context_supports, item)
                    )
                }
            )
            for index, item in enumerate(facts, start=1)
        ),
        plan,
    )
    pack = replace(
        _pack(plan, evidence),
        trusted_source_groups=groups,
        complete_group_ids=tuple(group.group_id for group in groups),
    )
    outcome = GroundedAnsweringService(generator).answer(
        plan.standalone_query,
        evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=plan,
        atom_support_matrix=_matrix(
            plan,
            (
                (
                    AtomStatus.PARTIAL,
                    tuple(item.support_id for item in evidence),
                ),
            ),
        ),
        generation_evidence_pack=pack,
        analysis=QueryAnalysis(
            original_query=plan.standalone_query,
            normalized_query=plan.standalone_query,
            conversation_fingerprint="sha256:" + "0" * 64,
            semantics=QuerySemantics(
                target=target,
                relation=relation,
                context_qualifier=stage,
                answer_type=RequestedAnswerType(shape.value),
            ),
        )
        if stage
        else None,
    )
    return outcome, generator


def test_service_complete_duties_ignore_unasked_output_column() -> None:
    evidence, group = _material()
    outcome, generator = _run(evidence, (group,), evidence[4:6])
    assert outcome.accepted_claim_count == 2
    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.answer is not None
    assert "未查到" not in outcome.answer
    assert "测试报告" not in outcome.answer
    assert generator.generate.call_count == 1


def test_service_same_role_wrong_stage_cannot_complete_target() -> None:
    evidence, group = _material()
    outcome, _ = _run(
        evidence,
        (group,),
        evidence[4:6],
        stage="需求评审阶段",
    )
    assert outcome.atom_coverage != (("A1", "SUPPORTED"),)


def test_service_target_member_trace_has_stable_keys_only() -> None:
    evidence, group = _material()
    before, _ = _run(evidence, (group,), evidence[4:6])
    assert len(before.target_member_coverage) == 1
    first = dict(before.target_member_coverage[0])
    renamed = tuple(
        item.model_copy(update={"evidence_id": f"S{99 - index}"})
        for index, item in enumerate(evidence)
    )
    after, _ = _run(renamed, (group,), renamed[4:6])
    assert len(after.target_member_coverage) == 1
    last = dict(after.target_member_coverage[0])
    assert before.atom_coverage == after.atom_coverage == (("A1", "SUPPORTED"),)
    assert first["required_member_keys"] == last["required_member_keys"]
    assert first["covered_member_keys"] == last["covered_member_keys"]
    assert last["source_complete"] is True
    assert last["complete"] is True
    assert last["missing_member_keys"] == ()
    assert all(
        key.startswith("sha256:") for key in last["required_member_keys"]
    )
    safe = json.dumps(last, ensure_ascii=False)
    assert all(item.citation_text not in safe for item in evidence)


def test_service_two_target_groups_only_one_answered_remains_partial() -> None:
    evidence, group = _material()
    groups = tuple(
        trusted_list_group(
            (*evidence[:4], fact), group_id=f"egrp_{n:032x}"
        ).model_copy(
            update={
                "kind": group.kind,
                "heading_path": group.heading_path,
                "metadata": group.metadata,
            }
        )
        for n, fact in enumerate(evidence[4:6], start=1)
    )
    outcome, _ = _run(evidence, groups, (evidence[4],))
    assert outcome.accepted_claim_count == 1
    assert outcome.atom_coverage == (("A1", "PARTIAL"),)
    assert outcome.answer is not None
    assert "现场调试" not in outcome.answer


def test_service_other_role_complete_list_cannot_fill_missing_duty() -> None:
    evidence, group = _material()
    outcome, _ = _run(evidence, (group,), (evidence[4], evidence[-1]))
    assert outcome.atom_coverage != (("A1", "SUPPORTED"),)
    assert outcome.answer is not None
    assert "运营团队负责联系客户" not in outcome.answer


def test_service_complete_requested_column_needs_no_other_duties() -> None:
    evidence, group = _material()
    context = _context("开发团队的交付成果有哪些？")
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
        ((1, 0), (0, 2), (1, 2)),
        (evidence[3], evidence[2], evidence[6]),
        strict=True,
    ):
        span = item.source_spans[0]
        key = (
            item.chunk_id,
            span.node_id,
            span.source_start_char,
            span.source_end_char,
            span.span_type.value,
        )
        cells[coordinate] = {key: item.citation_text}
    # 必须运行生产交点认证；不在测试中手写 SUPPORTED 证书。
    result = _table_intersection_certificate(context, cells, 1, 2)
    assert result is not None
    certificate = asdict(result[1])
    certificate["status"] = result[1].status.value
    certificate["supporting_span_ids"] = list(result[1].supporting_span_ids)
    evidence = tuple(
        item.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        **dict(item.metadata),
                        "answer_support": certificate,
                    }
                )
            }
        )
        if item in (evidence[3], evidence[2], evidence[6])
        else item
        for item in evidence
    )
    outcome, _ = _run(
        evidence,
        (group,),
        (evidence[6],),
        relation="交付成果",
        shape=AtomAnswerShape.ENUMERATION,
        context_supports=(evidence[3], evidence[2]),
    )
    assert outcome.accepted_claim_count == 1
    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.answer is not None
    assert "现场调试" not in outcome.answer
    assert "未查到" not in outcome.answer
