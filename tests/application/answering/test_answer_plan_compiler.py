"""统一问答计划必须冻结来源、结构选择、义务和覆盖。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from rag_app.application.answering.executor import (
    AnswerExecutionError,
    execute_deterministic_tasks,
    render_deterministic_answer,
)
from rag_app.application.answering.grounded import (
    GroundedAnsweringService,
    _generation_plan_artifacts,
)
from rag_app.application.answering.natural_renderer import (
    ValidatedNaturalClaim,
)
from rag_app.application.answering.plan_compiler import compile_answer_plan
from rag_app.application.answering.plan_coverage import (
    AnswerPlanContractError,
    ValidatedPlanArtifact,
    reduce_plan_coverage,
)
from rag_app.application.retrieval.generation_evidence import (
    EvidenceAdmissionReason,
    EvidenceAdmissionStatus,
    GenerationEvidenceEntry,
    GenerationEvidencePack,
)
from rag_app.application.retrieval.source_scope import build_resolved_query_view
from rag_app.core.errors import ProviderInputTooLarge
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    AnswerClaim,
    AtomFactBinding,
    ClaimSupport,
    ConfidenceDecision,
    ConfidenceStatus,
    EvidenceItem,
    PhysicalTableFact,
    PhysicalTableHeader,
)
from rag_app.core.models.answer_plan import (
    AnswerOperation,
    AnswerQualifierKind,
    AnswerTaskMode,
    SourceMentionRole,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    QueryAtom,
    QueryPlan,
    SourceContentRequirement,
    SourceDocumentIdentity,
    SourceIntent,
    SourceResolution,
    SourceScopeDecision,
    make_query_plan,
)
from tests.application.answering.source_contract_fixtures import (
    trusted_list_group,
)
from tests.application.answering.test_natural_grounded_answer import (
    _evidence,
    _matrix,
)
from tests.application.answering.test_target_member_coverage import _input_table


def _scope(
    atom_id: str,
    evidence: tuple[EvidenceItem, ...],
    *,
    resolved: bool = True,
) -> SourceScopeDecision:
    item = evidence[0]
    assert item.document_id is not None
    assert item.document_version_id is not None
    return SourceScopeDecision(
        atom_id=atom_id,
        source_intent=SourceIntent.DOCUMENT_AUTHORITY,
        resolution=(
            SourceResolution.RESOLVED
            if resolved
            else SourceResolution.UNRESOLVED
        ),
        allowed_documents=(
            (
                SourceDocumentIdentity(
                    document_id=item.document_id,
                    document_version_id=item.document_version_id,
                ),
            )
            if resolved
            else ()
        ),
        required_content=SourceContentRequirement.BODY,
        mention_sha256=canonical_sha256("开发中心三种工作模式"),
        registry_revision="test-registry-v1",
        scope_digest=canonical_sha256((atom_id, resolved)),
    )


def _plan(
    query: str,
    evidence: tuple[EvidenceItem, ...],
    *,
    relations: tuple[str, ...] = ("输入",),
    resolved: bool = True,
    source_qualifier: str | None = "开发中心三种工作模式",
) -> QueryPlan:
    atoms = tuple(
        QueryAtom(
            atom_id=f"A{index}",
            target="需求快验" if index == 1 else "需求快验限定",
            relation=relation,
            answer_shape=AtomAnswerShape.FACT,
            source_qualifier=source_qualifier,
            original_fragment=query,
            source_scope=_scope(f"A{index}", evidence, resolved=resolved),
        )
        for index, relation in enumerate(relations, 1)
    )
    return make_query_plan(
        standalone_query=query,
        original_query=query,
        intent="FACT",
        effort="DIRECT",
        atoms=atoms,
        reason_code="TEST",
        planner_called=False,
    )


def _facts(
    evidence: tuple[EvidenceItem, ...],
) -> tuple[PhysicalTableFact, PhysicalTableFact]:
    item = evidence[3]
    assert item.document_id is not None
    assert item.document_version_id is not None
    table_key = canonical_sha256("input-table")
    common = {
        "table_key": table_key,
        "document_id": item.document_id,
        "document_version_id": item.document_version_id,
        "table_node_id": item.source_spans[0].node_id,
        "row_index": 1,
        "row_label_column_index": 0,
        "row_label_support_ids": (evidence[3].support_id,),
    }
    return (
        PhysicalTableFact(
            fact_id=canonical_sha256("input-fact"),
            value_column_index=1,
            value_support_ids=(
                evidence[4].support_id,
                evidence[5].support_id,
            ),
            headers=(
                PhysicalTableHeader(
                    row_index=0,
                    column_indexes=(1,),
                    support_ids=(evidence[1].support_id,),
                ),
            ),
            **common,
        ),
        PhysicalTableFact(
            fact_id=canonical_sha256("output-fact"),
            value_column_index=2,
            value_support_ids=(evidence[6].support_id,),
            headers=(
                PhysicalTableHeader(
                    row_index=0,
                    column_indexes=(2,),
                    support_ids=(evidence[2].support_id,),
                ),
            ),
            **common,
        ),
    )


def _pack(
    plan: QueryPlan,
    evidence: tuple[EvidenceItem, ...],
    *,
    input_status: str = "SUPPORTED",
    include_output_binding: bool = True,
) -> GenerationEvidencePack:
    facts = _facts(evidence)
    entries = tuple(
        GenerationEvidenceEntry(
            support_id=item.support_id,
            evidence_item=item,
            source_group_id=None,
            linked_atom_ids=tuple(atom.atom_id for atom in plan.atoms),
            admission_status=EvidenceAdmissionStatus.ADMITTED,
            hard_reject_reasons=(),
            soft_signals=(EvidenceAdmissionReason.ACTIVE_CITABLE,),
            rerank_rank=index,
            source_order=index,
        )
        for index, item in enumerate(evidence, 1)
    )
    bindings: list[AtomFactBinding] = []
    for atom in plan.atoms:
        relation = "输出" if atom.relation == "输出" else "输入"
        fact = facts[1] if relation == "输出" else facts[0]
        bindings.append(
            AtomFactBinding(
                atom_id=atom.atom_id,
                fact_id=fact.fact_id,
                relation_status=(
                    input_status if relation == "输入" else "SUPPORTED"
                ),
                requested_target="需求快验",
                requested_relation=relation,
            )
        )
        if include_output_binding and atom.relation not in {"输入", "输出"}:
            bindings.append(
                AtomFactBinding(
                    atom_id=atom.atom_id,
                    fact_id=facts[1].fact_id,
                    requested_target="需求快验",
                    requested_relation=atom.relation,
                )
            )
    per_atom = tuple(
        (atom.atom_id, tuple(item.support_id for item in evidence))
        for atom in plan.atoms
    )
    return GenerationEvidencePack(
        original_query=plan.original_query,
        resolved_root_query=plan.resolved_root_query,
        entries=entries,
        rejected_entries=(),
        per_atom_candidate_support_ids=per_atom,
        complete_group_ids=(),
        partial_group_ids=(),
        missing_atom_ids=(),
        physical_table_facts=facts,
        atom_fact_bindings=tuple(bindings),
    )


def _fixture_plan(
    query: str,
    *,
    relations: tuple[str, ...] = ("输入",),
    resolved: bool = True,
    input_status: str = "SUPPORTED",
) -> tuple[QueryPlan, GenerationEvidencePack, tuple[EvidenceItem, ...]]:
    evidence, _group = _input_table()
    plan = _plan(query, evidence, relations=relations, resolved=resolved)
    return plan, _pack(plan, evidence, input_status=input_status), evidence


def _list_fixture(
    query: str = "开发团队的职责有哪些？",
    *,
    shared_member_node: bool = False,
) -> tuple[QueryPlan, GenerationEvidencePack, tuple[EvidenceItem, ...]]:
    """构造带 canonical 列表成员的开放生成计划。"""
    evidence = _evidence(
        "职责如下：",
        "1. 负责设计与开发。",
        "2. 负责现场调试与问题修复。",
    )
    if shared_member_node:
        first_span = evidence[1].source_spans[0]
        second_span = evidence[2].source_spans[0]
        evidence = (
            evidence[0],
            evidence[1],
            evidence[2].model_copy(
                update={
                    "source_spans": (
                        second_span.model_copy(
                            update={"node_id": first_span.node_id}
                        ),
                    )
                }
            ),
        )
    atom = QueryAtom(
        atom_id="A1",
        target="开发团队",
        relation="职责",
        answer_shape=AtomAnswerShape.DUTIES,
        original_fragment=query,
    )
    plan = make_query_plan(
        standalone_query=query,
        original_query=query,
        intent="FACT",
        effort="DIRECT",
        atoms=(atom,),
        reason_code="TEST",
        planner_called=False,
    )
    group = trusted_list_group(evidence).model_copy(
        update={"heading_path": ("开发团队",)}
    )
    entries = tuple(
        GenerationEvidenceEntry(
            support_id=item.support_id,
            evidence_item=item,
            source_group_id=group.group_id,
            linked_atom_ids=("A1",),
            admission_status=EvidenceAdmissionStatus.ADMITTED,
            hard_reject_reasons=(),
            soft_signals=(EvidenceAdmissionReason.COMPLETE_GROUP,),
            rerank_rank=index,
            source_order=index,
        )
        for index, item in enumerate(evidence, 1)
    )
    pack = GenerationEvidencePack(
        original_query=query,
        resolved_root_query=query,
        entries=entries,
        rejected_entries=(),
        per_atom_candidate_support_ids=(
            ("A1", tuple(item.support_id for item in evidence)),
        ),
        complete_group_ids=(group.group_id,),
        partial_group_ids=(),
        missing_atom_ids=(),
        trusted_source_groups=(group,),
    )
    return plan, pack, evidence


def test_query_view_masks_only_authority_source_span() -> None:
    query = "《开发中心三种工作模式》中，需求快验的输入项是什么？"
    plan, _pack_value, _evidence_value = _fixture_plan(query)

    view = build_resolved_query_view(plan)

    assert view.business_query == "需求快验的输入项是什么?"
    assert "三种" not in view.business_query
    assert view.source_mentions[0].role is SourceMentionRole.AUTHORITY
    mention = view.source_mentions[0]
    assert query[mention.original_start : mention.original_end] == mention.text
    assert len(view.normalized_offsets) == len(query)


def test_reference_owner_keeps_referenced_document_in_business_query() -> None:
    query = "乙文档是否提到《甲规范》？"
    plan, _pack_value, evidence = _fixture_plan(query)
    plan = _plan(
        query,
        evidence,
        source_qualifier=None,
    )

    view = build_resolved_query_view(plan)

    assert "《甲规范》" in view.business_query
    assert tuple(item.role for item in view.source_mentions) == (
        SourceMentionRole.SOURCE_OWNER,
        SourceMentionRole.REFERENCED_OBJECT,
    )


def test_explicit_source_field_lookup_freezes_only_requested_fact() -> None:
    query = "《开发中心三种工作模式》中，需求快验的输入项是什么？"
    query_plan, pack, _evidence_value = _fixture_plan(query)

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    assert len(plan.selections) == 1
    assert plan.selections[0].field_label.startswith("输入")
    assert plan.obligations[0].operation is AnswerOperation.FIELD_LOOKUP
    assert plan.obligations[0].required_member_keys == (
        plan.selections[0].fact_id,
    )
    assert plan.physical_tasks[0].mode is AnswerTaskMode.DETERMINISTIC


def test_duplicate_schema_candidates_do_not_form_deterministic_binding() -> (
    None
):
    query_plan, pack, _ = _fixture_plan("需求快验的输入是什么？")
    original = pack.physical_table_facts[0]
    duplicate = original.model_copy(
        update={
            "fact_id": canonical_sha256("duplicate-input-fact"),
            "table_key": canonical_sha256("duplicate-input-table"),
        }
    )
    binding = next(
        item
        for item in pack.atom_fact_bindings
        if item.fact_id == original.fact_id
    )
    ambiguous_pack = replace(
        pack,
        physical_table_facts=(
            original,
            duplicate,
            *pack.physical_table_facts[1:],
        ),
        atom_fact_bindings=(
            *pack.atom_fact_bindings,
            binding.model_copy(update={"fact_id": duplicate.fact_id}),
        ),
    )

    plan = compile_answer_plan(
        query_plan,
        ambiguous_pack,
        snapshot_id="irev-test",
    )

    assert not plan.selections
    assert plan.obligations[0].operation is AnswerOperation.OPEN_TEXT


def test_explicit_column_variant_keeps_same_selection_identity() -> None:
    first_plan, first_pack, _ = _fixture_plan(
        "《开发中心三种工作模式》中，需求快验的输入项是什么？"
    )
    second_plan, second_pack, _ = _fixture_plan(
        "《开发中心三种工作模式》中，需求快验的“输入”列有哪些内容？",
        relations=("输入", "内容"),
    )

    first = compile_answer_plan(first_plan, first_pack, snapshot_id="irev-test")
    second = compile_answer_plan(
        second_plan, second_pack, snapshot_id="irev-test"
    )

    assert len(second.selections) == 1
    assert first.selections[0].selection_digest == (
        second.selections[0].selection_digest
    )
    assert second.obligations[0].atom_ids == ("A1", "A2")


def test_candidate_reorder_and_alias_change_keep_selection_identity() -> None:
    query_plan, pack, _ = _fixture_plan("需求快验的输入是什么？")
    before = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")
    aliases = {
        item.support_id: f"S{len(pack.entries) - index}"
        for index, item in enumerate(pack.entries)
    }

    def mapped(ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(aliases[item] for item in ids)

    facts = tuple(
        fact.model_copy(
            update={
                "row_label_support_ids": mapped(fact.row_label_support_ids),
                "value_support_ids": mapped(fact.value_support_ids),
                "headers": tuple(
                    header.model_copy(
                        update={"support_ids": mapped(header.support_ids)}
                    )
                    for header in fact.headers
                ),
            }
        )
        for fact in pack.physical_table_facts
    )
    reordered = replace(
        pack,
        entries=tuple(
            replace(
                entry,
                support_id=aliases[entry.support_id],
                evidence_item=entry.evidence_item.model_copy(
                    update={"evidence_id": aliases[entry.support_id]}
                ),
            )
            for entry in reversed(pack.entries)
        ),
        per_atom_candidate_support_ids=tuple(
            (atom_id, mapped(support_ids))
            for atom_id, support_ids in pack.per_atom_candidate_support_ids
        ),
        physical_table_facts=facts,
    )

    after = compile_answer_plan(
        query_plan,
        reordered,
        snapshot_id="irev-test",
    )

    assert before.selections[0].selection_digest == (
        after.selections[0].selection_digest
    )
    assert before.obligations[0].required_member_keys == (
        after.obligations[0].required_member_keys
    )


def test_strict_temporal_modality_is_not_erased_or_auto_satisfied() -> None:
    query_plan, pack, _ = _fixture_plan(
        "需求快验之前必须准备哪些材料？",
        relations=("输入", "必须准备"),
        input_status="UNDETERMINED",
    )

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    assert len(plan.selections) == 1
    qualifiers = plan.obligations[0].qualifiers
    assert tuple(item.kind for item in qualifiers) == (
        AnswerQualifierKind.BEFORE,
        AnswerQualifierKind.MUST,
    )
    assert not any(item.supported for item in qualifiers)


def test_qualifier_atom_reuses_base_fact_without_generation_task() -> None:
    query_plan, pack, _ = _fixture_plan(
        "需求快验之前必须准备哪些材料？",
        relations=("输入", "必须准备"),
    )
    pack = replace(
        pack,
        atom_fact_bindings=tuple(
            item.model_copy(
                update={
                    "relation_status": "UNDETERMINED",
                    "requested_relation": "必须准备",
                }
            )
            if item.atom_id == "A2"
            else item
            for item in pack.atom_fact_bindings
        ),
    )

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    assert len(plan.obligations) == 1
    assert plan.obligations[0].atom_ids == ("A1", "A2")
    assert plan.physical_tasks[0].mode is AnswerTaskMode.DETERMINISTIC
    assert not any(
        item.mode is AnswerTaskMode.GROUNDED_GENERATION
        for item in plan.physical_tasks
    )


def test_colloquial_question_does_not_invent_strict_qualifier() -> None:
    query_plan, pack, _ = _fixture_plan("做快验前到底得备齐啥？")

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    assert len(plan.selections) == 1
    assert not plan.obligations[0].qualifiers


def test_two_fields_keep_two_obligations_but_share_one_physical_task() -> None:
    query_plan, pack, _ = _fixture_plan(
        "需求快验的输入和输出分别是什么？",
        relations=("输入", "输出"),
    )

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    assert len(plan.selections) == 2
    assert len(plan.obligations) == 2
    assert len(plan.physical_tasks) == 1
    task = plan.physical_tasks[0]
    row_support_id = pack.physical_table_facts[0].row_label_support_ids[0]
    assert task.dependency_support_ids.count(row_support_id) == 1


def test_unresolved_explicit_source_cannot_enter_deterministic_path() -> None:
    query_plan, pack, _ = _fixture_plan(
        "《不存在的文档》中，需求快验的输入是什么？",
        resolved=False,
    )

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    assert not plan.selections
    assert plan.obligations[0].operation is AnswerOperation.OPEN_TEXT
    assert not plan.obligations[0].source_resolved
    assert plan.physical_tasks[0].mode is AnswerTaskMode.GROUNDED_GENERATION


def test_deterministic_execution_and_coverage_ignore_sibling_column() -> None:
    query_plan, pack, _ = _fixture_plan(
        "《开发中心三种工作模式》中，需求快验的输入项是什么？"
    )
    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    execution = execute_deterministic_tasks(plan, pack)
    coverage = reduce_plan_coverage(plan, execution.artifacts)

    assert coverage.complete
    assert coverage.obligations[0].status == "FULL"
    assert len(coverage.obligations[0].covered_member_keys) == 1
    assert execution.artifacts[0].claim is not None
    assert len(execution.artifacts[0].claim.supports) == 4
    answer = render_deterministic_answer(plan, execution, coverage)
    assert answer is not None
    assert "需求功能点描述" in answer
    assert "快验结论" not in answer


def test_qualifier_gap_keeps_safe_fact_but_plan_is_partial() -> None:
    query_plan, pack, _ = _fixture_plan(
        "需求快验之前必须准备哪些材料？",
        relations=("输入", "必须准备"),
        input_status="UNDETERMINED",
    )
    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    execution = execute_deterministic_tasks(plan, pack)
    coverage = reduce_plan_coverage(plan, execution.artifacts)
    answer = render_deterministic_answer(plan, execution, coverage)

    assert coverage.obligations[0].status == "PARTIAL"
    assert coverage.obligations[0].missing_qualifier_ids == ("Q1", "Q2")
    assert answer is not None
    assert "不把它们作为结论" in answer


def test_missing_second_field_stays_missing_after_first_is_published() -> None:
    query_plan, pack, _ = _fixture_plan(
        "需求快验的输入和输出分别是什么？",
        relations=("输入", "输出"),
    )
    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")
    execution = execute_deterministic_tasks(plan, pack)

    coverage = reduce_plan_coverage(plan, execution.artifacts[:1])

    assert tuple(item.status for item in coverage.obligations) == (
        "FULL",
        "MISSING",
    )
    assert not coverage.complete


def test_structured_list_freezes_members_before_generation() -> None:
    query_plan, pack, evidence = _list_fixture()

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    obligation = plan.obligations[0]
    assert obligation.operation is AnswerOperation.OPEN_TEXT
    assert len(obligation.required_member_keys) == 2
    assert tuple(
        dependency.support_ids for dependency in obligation.member_dependencies
    ) == ((evidence[1].support_id,), (evidence[2].support_id,))
    assert plan.physical_tasks[0].mode is AnswerTaskMode.GROUNDED_GENERATION


def test_structured_list_full_quote_does_not_hide_missing_member() -> None:
    query_plan, pack, evidence = _list_fixture()
    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")
    partial = ValidatedNaturalClaim(
        claim_id="C1",
        atom_ids=("A1",),
        claim=AnswerClaim(
            text=evidence[1].citation_text,
            supports=tuple(
                ClaimSupport(
                    support_id=item.support_id,
                    quote=item.citation_text,
                )
                for item in evidence[1:]
            ),
        ),
    )

    partial_coverage = reduce_plan_coverage(
        plan,
        _generation_plan_artifacts(
            plan,
            (partial,),
            evidence,
            skip_claim_ids=frozenset(),
        ),
    )

    assert partial_coverage.obligations[0].status == "PARTIAL"
    assert len(partial_coverage.obligations[0].missing_member_keys) == 1

    limited = ValidatedPlanArtifact(
        artifact_id="R1",
        plan_id=plan.plan_id,
        obligation_ids=(plan.obligations[0].obligation_id,),
        selection_digests=(),
        covered_member_keys=(),
        satisfied_qualifier_ids=(),
        source_closed=False,
        origin="GROUNDED_GENERATION",
        resource_limited=True,
    )
    limited_coverage = reduce_plan_coverage(
        plan,
        (
            *_generation_plan_artifacts(
                plan,
                (partial,),
                evidence,
                skip_claim_ids=frozenset(),
            ),
            limited,
        ),
    )
    assert limited_coverage.obligations[0].status == "RESOURCE_LIMITED"

    full_claims = tuple(
        ValidatedNaturalClaim(
            claim_id=f"C{index}",
            atom_ids=("A1",),
            claim=AnswerClaim(
                text=item.citation_text,
                supports=(
                    ClaimSupport(
                        support_id=item.support_id,
                        quote=item.citation_text,
                    ),
                ),
            ),
        )
        for index, item in enumerate(evidence[1:], 1)
    )
    full_coverage = reduce_plan_coverage(
        plan,
        _generation_plan_artifacts(
            plan,
            full_claims,
            evidence,
            skip_claim_ids=frozenset(),
        ),
    )
    assert full_coverage.complete


def test_structured_ordinal_requires_only_requested_member() -> None:
    query_plan, pack, evidence = _list_fixture("开发团队职责的第二项是什么？")

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    obligation = plan.obligations[0]
    assert len(obligation.required_member_keys) == 1
    assert obligation.member_dependencies[0].support_ids == (
        evidence[2].support_id,
    )


def test_split_chunks_of_same_source_node_are_one_member() -> None:
    query_plan, pack, evidence = _list_fixture(shared_member_node=True)

    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")

    obligation = plan.obligations[0]
    assert len(obligation.required_member_keys) == 1
    assert set(obligation.member_dependencies[0].support_ids) == {
        evidence[1].support_id,
        evidence[2].support_id,
    }


def test_coverage_rejects_changed_plan_or_selection_identity() -> None:
    query_plan, pack, _ = _fixture_plan("需求快验的输入是什么？")
    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")
    artifact = execute_deterministic_tasks(plan, pack).artifacts[0]

    with pytest.raises(
        AnswerPlanContractError, match="ANSWER_PLAN_IDENTITY_CHANGED"
    ):
        reduce_plan_coverage(
            plan,
            (replace(artifact, plan_id=canonical_sha256("changed")),),
        )
    with pytest.raises(
        AnswerPlanContractError, match="ANSWER_SELECTION_IDENTITY_CHANGED"
    ):
        reduce_plan_coverage(
            plan,
            (
                replace(
                    artifact,
                    selection_digests=(canonical_sha256("changed"),),
                ),
            ),
        )
    with pytest.raises(
        AnswerPlanContractError, match="ANSWER_MEMBER_IDENTITY_CHANGED"
    ):
        reduce_plan_coverage(
            plan,
            (
                replace(
                    artifact,
                    covered_member_keys=(canonical_sha256("changed"),),
                ),
            ),
        )


def test_runtime_fact_mutation_is_internal_contract_error() -> None:
    query_plan, pack, _ = _fixture_plan("需求快验的输入是什么？")
    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")
    changed_fact = pack.physical_table_facts[0].model_copy(
        update={"value_column_index": 7}
    )
    changed_pack = replace(
        pack,
        physical_table_facts=(changed_fact, *pack.physical_table_facts[1:]),
    )

    with pytest.raises(
        AnswerExecutionError, match="ANSWER_SELECTION_RUNTIME_MISMATCH"
    ):
        execute_deterministic_tasks(plan, changed_pack)


def test_open_generation_artifact_can_close_only_its_own_obligation() -> None:
    query_plan, pack, _ = _fixture_plan(
        "请概述需求快验的背景。",
        relations=("背景",),
        input_status="UNDETERMINED",
    )
    pack = replace(pack, atom_fact_bindings=())
    plan = compile_answer_plan(query_plan, pack, snapshot_id="irev-test")
    artifact = ValidatedPlanArtifact(
        artifact_id="G1",
        plan_id=plan.plan_id,
        obligation_ids=(plan.obligations[0].obligation_id,),
        selection_digests=(),
        covered_member_keys=(),
        satisfied_qualifier_ids=(),
        source_closed=True,
        origin="GROUNDED_GENERATION",
        claim=AnswerClaim(
            text="已核验概述。",
            supports=(
                ClaimSupport(
                    support_id=pack.evidence[0].support_id,
                    quote=pack.evidence[0].citation_text,
                ),
            ),
        ),
    )

    coverage = reduce_plan_coverage(plan, (artifact,))

    assert coverage.complete


def test_service_deterministic_path_calls_no_generation_or_review() -> None:
    query_plan, pack, _ = _fixture_plan(
        "《开发中心三种工作模式》中，需求快验的输入项是什么？"
    )
    generator = Mock()
    support_ids = tuple(item.support_id for item in pack.evidence)

    outcome = GroundedAnsweringService(generator).answer(
        query_plan.standalone_query,
        pack.evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=query_plan,
        atom_support_matrix=_matrix(
            query_plan,
            ((AtomStatus.SUPPORTED, support_ids),),
        ),
        generation_evidence_pack=pack,
        snapshot_id="irev-test",
    )

    assert outcome.answer is not None
    assert outcome.reason_code == "DETERMINISTIC_ANSWER_PLAN"
    assert outcome.relation_review_calls == 0
    assert outcome.relation_review_skip_reason == "DETERMINISTIC_EXECUTION"
    assert outcome.answer_plan_id is not None
    assert generator.generate.call_count == 0


def test_mixed_plan_keeps_deterministic_fact_when_generation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_plan, pack, _ = _fixture_plan(
        "需求快验的输入是什么，并概述背景。",
        relations=("输入", "背景"),
    )
    pack = replace(
        pack,
        atom_fact_bindings=tuple(
            item for item in pack.atom_fact_bindings if item.atom_id == "A1"
        ),
    )
    generator = Mock()
    generator.generate.side_effect = ProviderInputTooLarge(
        "synthetic generation budget failure",
        stage="generation.prepare",
        code="GENERATION_INPUT_BUDGET_EXCEEDED",
    )
    monkeypatch.setattr(
        "rag_app.application.answering.grounded._target_coverage_records",
        lambda *_args, **_kwargs: pytest.fail("冻结计划覆盖不得再次读取原问题"),
    )
    support_ids = tuple(item.support_id for item in pack.evidence)

    outcome = GroundedAnsweringService(generator).answer(
        query_plan.standalone_query,
        pack.evidence,
        ConfidenceDecision(status=ConfidenceStatus.ANSWERABLE, score=1.0),
        query_plan=query_plan,
        atom_support_matrix=_matrix(
            query_plan,
            (
                (AtomStatus.SUPPORTED, support_ids),
                (AtomStatus.SUPPORTED, support_ids),
            ),
        ),
        generation_evidence_pack=pack,
        snapshot_id="irev-test",
    )

    assert outcome.answer is not None
    assert "需求功能点描述" in outcome.answer
    assert outcome.mode == "extractive"
    assert outcome.reason_code == "LIMITED_ANSWER"
    assert outcome.atom_coverage == (("A1", "SUPPORTED"), ("A2", "MISSING"))
    assert tuple(
        dict(item)["status"] for item in outcome.answer_plan_coverage
    ) == ("FULL", "RESOURCE_LIMITED")
    request = generator.generate.call_args.args[0]
    assert request.execution_atom_ids == ("A2",)
