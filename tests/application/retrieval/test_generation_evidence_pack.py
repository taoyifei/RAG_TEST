"""生成证据准入只处理安全边界，语义证明留给生成后 Claim。"""

from __future__ import annotations

from dataclasses import replace
from typing import TypedDict, Unpack

from rag_app.application.retrieval.evidence import _evidence_item
from rag_app.application.retrieval.evidence_groups import GroupCandidate
from rag_app.application.retrieval.generation_evidence import (
    EvidenceAdmissionReason,
    EvidenceAdmissionStatus,
    GenerationEvidencePack,
    build_generation_evidence_pack,
)
from rag_app.core.models import (
    ChunkRole,
    EvidenceGroup,
    EvidenceGroupKind,
    EvidenceItem,
    GroupSourceMap,
    KnowledgeBaseScope,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    AtomConstraintKind,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
    QueryAtom,
    QueryConstraint,
    QueryPlan,
)
from tests.application.retrieval.helpers import make_ranked_chunk


def _plan(*atoms: QueryAtom) -> QueryPlan:
    return QueryPlan(
        plan_id=f"sha256:{'a' * 64}",
        standalone_query="设备检修时限是多少？",
        original_query="设备检修时限是多少？",
        resolved_root_query="设备检修时限是多少？",
        context_digest=f"sha256:{'b' * 64}",
        intent="fact",
        effort="DIRECT",
        atoms=atoms,
        planner_reason_code="TEST",
    )


def _request(text: str = "设备检修时限是多少？") -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=f"prj_{'a' * 32}",
            knowledge_base_id=f"kb_{'b' * 32}",
        ),
        text=text,
    )


def _item(number: int, text: str) -> tuple[RankedChunk, EvidenceItem]:
    candidate = make_ranked_chunk(number, text)
    span = candidate.hydrated.chunk.source_spans[0]
    evidence = _evidence_item(candidate, span, text, "S1")
    return candidate, evidence


class _PackOptions(TypedDict, total=False):
    root: tuple[EvidenceItem, ...]
    atom: tuple[EvidenceItem, ...]
    links: tuple[AtomCandidateLink, ...]
    groups: tuple[GroupCandidate, ...]
    request: SearchRequest | None


def _pack(
    plan: QueryPlan,
    candidates: tuple[RankedChunk, ...],
    **options: Unpack[_PackOptions],
) -> GenerationEvidencePack:
    return build_generation_evidence_pack(
        query_plan=plan,
        root_evidence=options.get("root", ()),
        atom_evidence=options.get("atom", ()),
        ranked_candidates=candidates,
        groups=options.get("groups", ()),
        links=options.get("links", ()),
        request=options.get("request") or _request(),
        active_revision_id=f"irev_{'c' * 32}",
        excluded_document_ids=(),
        policy=RetrievalPolicy(),
    )


def test_direct_support_mismatch_remains_generation_candidate() -> None:
    candidate, evidence = _item(1, "检修记录应在三天内归档。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "answer_support": {
                        "status": "SUPPORTED",
                        "query_target": "检修记录",
                        "requested_relation_or_attribute": "归档期限",
                    }
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备检修资料",
        relation="保存时限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    link = AtomCandidateLink(
        atom_id="A1",
        chunk_id=candidate.hydrated.chunk.chunk_id,
        channels=("lexical",),
        best_rank=1,
        score=1.0,
    )
    pack = _pack(_plan(atom), (candidate,), atom=(evidence,), links=(link,))
    assert len(pack.entries) == 1
    assert pack.per_atom_candidate_support_ids == (("A1", ("S1",)),)
    assert EvidenceAdmissionReason.TARGET_SOFT_MISMATCH in (
        pack.entries[0].soft_signals
    )


def test_root_candidate_without_atom_provenance_is_available() -> None:
    candidate, evidence = _item(1, "检修记录应在三天内归档。")
    atom = QueryAtom(
        atom_id="A1",
        target="检修记录",
        relation="期限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert pack.evidence[0].citation_text == evidence.citation_text
    assert pack.per_atom_candidate_support_ids == (("A1", ("S1",)),)
    assert EvidenceAdmissionReason.ROOT_RETRIEVAL in (
        pack.entries[0].soft_signals
    )
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.MISSING),)
    )
    assert pack.pre_generation_availability(matrix) == {
        "A1": "EVIDENCE_AVAILABLE"
    }


def test_explicit_source_mismatch_is_hard_rejected() -> None:
    candidate, evidence = _item(1, "检修记录应在三天内归档。")
    atom = QueryAtom(
        atom_id="A1",
        target="检修记录",
        relation="期限",
        answer_shape=AtomAnswerShape.DURATION,
        source_qualifier="指定制度.docx",
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert not pack.entries
    assert pack.rejected_entries[0].admission_status is (
        EvidenceAdmissionStatus.REJECTED_HARD
    )
    assert EvidenceAdmissionReason.EXPLICIT_SOURCE_MISMATCH in (
        pack.rejected_entries[0].hard_reject_reasons
    )
    assert pack.hard_rejected_sources == (
        {
            "document_version_id": evidence.document_version_id,
            "chunk_id": evidence.chunk_id,
            "node_ids": (evidence.source_spans[0].node_id,),
            "reasons": ("EXPLICIT_SOURCE_MISMATCH",),
        },
    )


def test_non_citable_and_inactive_version_are_hard_rejected() -> None:
    candidate, evidence = _item(1, "检修记录应在三天内归档。")
    atom = QueryAtom(
        atom_id="A1",
        target="检修记录",
        relation="期限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    invalid = evidence.model_copy(
        update={
            "document_version_id": f"dver_{'f' * 32}",
            "source_spans": (
                evidence.source_spans[0].model_copy(
                    update={"is_citable": False}
                ),
            ),
        }
    )
    pack = _pack(_plan(atom), (candidate,), root=(invalid,))
    assert not pack.entries
    assert set(pack.rejected_entries[0].hard_reject_reasons) == {
        EvidenceAdmissionReason.NON_CITABLE,
        EvidenceAdmissionReason.INACTIVE_VERSION,
    }


def test_partial_group_evidence_is_admitted_for_limited_answer() -> None:
    candidate, evidence = _item(1, "第一项：检查设备。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "evidence_group_id": "egrp_partial",
                    "evidence_group_type": "LIST_GROUP",
                    "group_complete": False,
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备检查",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert len(pack.entries) == 1
    assert pack.entries[0].admission_status is (
        EvidenceAdmissionStatus.ADMITTED_STRUCTURED_PARTIAL
    )
    assert pack.partial_group_ids == ("egrp_partial",)


def test_direct_single_value_literal_contradiction_is_hard_rejected() -> None:
    candidate, evidence = _item(1, "检修记录应在5天内归档。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "answer_support": {
                        "status": "SUPPORTED",
                        "query_target": "检修记录",
                        "requested_relation_or_attribute": "归档期限",
                    }
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="检修记录",
        relation="归档期限",
        answer_shape=AtomAnswerShape.DURATION,
        constraints=(
            QueryConstraint(kind=AtomConstraintKind.DURATION, value="3天"),
        ),
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert not pack.entries
    assert EvidenceAdmissionReason.HARD_LITERAL_CONTRADICTION in (
        pack.rejected_entries[0].hard_reject_reasons
    )


def _complete_group(
    members: tuple[RankedChunk, ...],
) -> GroupCandidate:
    first = members[0].hydrated.chunk
    source_maps = tuple(
        GroupSourceMap(
            chunk_id=member.hydrated.chunk.chunk_id,
            citation_text=member.hydrated.chunk.citation_text,
            source_spans=member.hydrated.chunk.source_spans,
        )
        for member in members
    )
    return GroupCandidate(
        group=EvidenceGroup(
            group_id=f"egrp_{'a' * 32}",
            kind=EvidenceGroupKind.LIST_GROUP,
            document_id=first.version.document_id,
            document_version_id=first.version.document_version_id,
            index_revision_id=first.index_revision_id,
            section_id=first.section_id,
            display_name="fixture.docx",
            heading_path=("检查步骤",),
            member_chunk_ids=tuple(item.chunk_id for item in source_maps),
            member_source_maps=source_maps,
            member_ranks=tuple(range(1, len(members) + 1)),
            group_text_for_model="\n".join(
                member.hydrated.chunk.citation_text for member in members
            ),
            complete=True,
            token_cost=sum(
                member.hydrated.chunk.token_count for member in members
            ),
        ),
        members=members,
        rerank_text="\n".join(
            member.hydrated.chunk.citation_text for member in members
        ),
    )


def test_complete_group_fills_real_member_spans() -> None:
    first, evidence = _item(1, "检查步骤包括：")
    second, _second_evidence = _item(2, "第一步：检查设备。")
    group = _complete_group((first, second))
    atom = QueryAtom(
        atom_id="A1",
        target="设备检查",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    pack = _pack(
        _plan(atom),
        (first,),
        root=(evidence,),
        groups=(group,),
    )
    assert len(pack.entries) == 2
    assert pack.complete_group_ids == (group.group_id,)
    assert {item.chunk_id for item in pack.evidence} == {
        first.hydrated.chunk.chunk_id,
        second.hydrated.chunk.chunk_id,
    }
    assert pack.per_atom_candidate_support_ids == (("A1", ("S1", "S2")),)
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.PARTIAL),)
    )
    assert pack.pre_generation_availability(matrix) == {
        "A1": "STRUCTURED_COMPLETE"
    }
    assert pack.structural_sibling_observation((group,)) == {
        "structural_sibling_pollution_count": 0,
        "structural_sibling_observation_status": "COMPLETE",
        "structural_sibling_unknown_group_count": 0,
        "structural_sibling_unknown_structural_count": 0,
        "structural_sibling_unknown_table_coordinate_count": 0,
        "structural_sibling_multi_row_table_count": 0,
        "structural_sibling_observation_scope": (
            "ADMITTED_STRUCTURAL_GROUP_AND_TABLE_ROW"
        ),
    }


def test_unknown_group_provenance_is_only_partially_observed() -> None:
    candidate, evidence = _item(1, "第一项：检查设备。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "evidence_group_id": "egrp_unknown",
                    "evidence_group_type": "LIST_GROUP",
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备检查",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    observation = pack.structural_sibling_observation(())
    assert observation["structural_sibling_pollution_count"] == 0
    assert observation["structural_sibling_observation_status"] == "PARTIAL"
    assert observation["structural_sibling_unknown_group_count"] == 1


def test_ungrouped_table_row_coordinates_are_observable() -> None:
    candidate = make_ranked_chunk(1, "机型", role=ChunkRole.TABLE)
    original_span = candidate.hydrated.chunk.source_spans[0]
    path = ("body", "tbl:2", "tr:2", "tc:0")
    anchor = original_span.source_anchor.model_copy(
        update={
            "structural_path": path,
            "table_index": 2,
            "row_index": 2,
            "cell_index": 0,
        }
    )
    span = original_span.model_copy(
        update={"structural_path": path, "source_anchor": anchor}
    )
    chunk = candidate.hydrated.chunk.model_copy(
        update={
            "source_spans": (span,),
            "metadata": freeze_json_object(
                {
                    "atoms": [
                        {
                            "metadata": {
                                "table_node_id": f"node_{'a' * 32}",
                                "row_index": 2,
                            }
                        }
                    ]
                }
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )
    evidence = _evidence_item(candidate, span, "机型", "S1")
    atom = QueryAtom(
        atom_id="A1",
        target="机型",
        relation="名称",
        answer_shape=AtomAnswerShape.FACT,
    )
    pack = _pack(_plan(atom), (candidate,), root=(evidence,))
    assert len(pack.entries) == 1
    assert pack.entries[0].table_node_id == f"node_{'a' * 32}"
    assert pack.entries[0].table_row_index == 2
    observation = pack.structural_sibling_observation(())
    assert observation["structural_sibling_pollution_count"] == 0
    assert observation["structural_sibling_observation_status"] == "COMPLETE"
    assert observation["structural_sibling_unknown_structural_count"] == 0
    other_row = replace(pack.entries[0], support_id="S2", table_row_index=3)
    multi_row = replace(pack, entries=(*pack.entries, other_row))
    multi_observation = multi_row.structural_sibling_observation(())
    assert (
        multi_observation["structural_sibling_observation_status"] == "COMPLETE"
    )
    assert multi_observation["structural_sibling_multi_row_table_count"] == 1
    matrix = AtomSupportMatrix(
        atoms=(AtomSupport(atom_id="A1", status=AtomStatus.PARTIAL),)
    )
    assert pack.pre_generation_availability(matrix) == {
        "A1": "STRUCTURED_PARTIAL"
    }


def test_sibling_group_identity_conflict_is_hard_rejected() -> None:
    first, _first_evidence = _item(1, "检查步骤包括：")
    second, _second_evidence = _item(2, "第一步：检查设备。")
    sibling, evidence = _item(3, "别的流程：报告异常。")
    group = _complete_group((first, second))
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "evidence_group_id": group.group_id,
                    "evidence_group_type": "LIST_GROUP",
                    "group_complete": True,
                }
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备检查",
        relation="步骤",
        answer_shape=AtomAnswerShape.PROCEDURE,
    )
    pack = _pack(
        _plan(atom),
        (sibling,),
        root=(evidence,),
        groups=(group,),
    )
    assert not pack.entries
    assert EvidenceAdmissionReason.STRUCTURAL_SIBLING_CONFLICT in (
        pack.rejected_entries[0].hard_reject_reasons
    )


def test_table_cross_row_and_scope_mismatch_are_hard_rejected() -> None:
    candidate, evidence = _item(1, "表格数值：3天。")
    span = evidence.source_spans[0]
    evidence = evidence.model_copy(
        update={
            "table_context": True,
            "source_spans": (
                span.model_copy(
                    update={"structural_path": ("body", "tbl:1", "tr:1")}
                ),
                span.model_copy(
                    update={"structural_path": ("body", "tbl:1", "tr:2")}
                ),
            ),
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="表格数值",
        relation="时限",
        answer_shape=AtomAnswerShape.DURATION,
    )
    wrong_scope = SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=f"prj_{'f' * 32}",
            knowledge_base_id=f"kb_{'b' * 32}",
        ),
        text="表格数值的时限是多少？",
    )
    pack = _pack(
        _plan(atom),
        (candidate,),
        root=(evidence,),
        request=wrong_scope,
    )
    assert set(pack.rejected_entries[0].hard_reject_reasons) == {
        EvidenceAdmissionReason.TABLE_ROW_CONFLICT,
        EvidenceAdmissionReason.SCOPE_MISMATCH,
    }


def test_catalog_entry_cannot_supply_template_body() -> None:
    candidate, evidence = _item(1, "模板目录列有设备登记表。")
    evidence = evidence.model_copy(
        update={
            "metadata": freeze_json_object(
                {"evidence_group_type": "CATALOG_ENTRY"}
            )
        }
    )
    atom = QueryAtom(
        atom_id="A1",
        target="设备登记表",
        relation="正文内容",
        answer_shape=AtomAnswerShape.FACT,
    )
    pack = _pack(
        _plan(atom),
        (candidate,),
        root=(evidence,),
        request=_request("设备登记表的正文内容是什么？"),
    )
    assert not pack.entries
    assert EvidenceAdmissionReason.TEMPLATE_BODY_UNAVAILABLE in (
        pack.rejected_entries[0].hard_reject_reasons
    )
