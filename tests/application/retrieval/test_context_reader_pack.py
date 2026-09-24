"""来源回读组直接形成生成证据与阅读单元的边界回归。"""

from __future__ import annotations

from rag_app.application.retrieval.context_reader import (
    ContextReadGroup,
    ContextReadPiece,
)
from rag_app.application.retrieval.context_reader_pack import (
    build_context_reader_evidence_pack,
)
from rag_app.application.retrieval.generation_evidence import (
    EvidenceAdmissionReason,
    EvidenceAdmissionStatus,
    GenerationEvidencePack,
    project_evidence_read_units,
)
from rag_app.core.models import KnowledgeBaseScope, RankedChunk, SearchRequest
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    QueryAtom,
    QueryPlan,
)
from tests.application.retrieval.helpers import make_ranked_chunk

_REVISION_ID = f"irev_{'c' * 32}"


def _plan() -> QueryPlan:
    query = "甲流程的办理步骤是什么？"
    return QueryPlan(
        plan_id=f"sha256:{'a' * 64}",
        standalone_query=query,
        original_query=query,
        resolved_root_query=query,
        context_digest=f"sha256:{'b' * 64}",
        intent="FACT",
        effort="DIRECT",
        atoms=(
            QueryAtom(
                atom_id="A1",
                target="甲流程",
                relation="办理步骤",
                answer_shape=AtomAnswerShape.PROCEDURE,
            ),
        ),
        planner_reason_code="TEST",
    )


def _request() -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=f"prj_{'a' * 32}",
            knowledge_base_id=f"kb_{'b' * 32}",
        ),
        text="甲流程的办理步骤是什么？",
    )


def _group(
    group_id: str,
    *candidates: RankedChunk,
    complete: bool = True,
) -> ContextReadGroup:
    pieces = tuple(
        ContextReadPiece(
            candidate=candidate,
            span=candidate.hydrated.chunk.source_spans[0],
        )
        for candidate in candidates
    )
    required = tuple(
        piece.span.node_id for piece in pieces if piece.span.node_id is not None
    )
    return ContextReadGroup(
        group_id=group_id,
        kind="list",
        seed_chunk_ids=(candidates[0].hydrated.chunk.chunk_id,),
        pieces=pieces,
        required_node_ids=required,
        missing_node_ids=() if complete else (required[-1],),
        source_complete=complete,
        reason_codes=() if complete else ("SOURCE_NODE_INCOMPLETE",),
    )


def _pack(
    *groups: ContextReadGroup,
    excluded_document_ids: tuple[str, ...] = (),
) -> GenerationEvidencePack:
    return build_context_reader_evidence_pack(
        query_plan=_plan(),
        groups=groups,
        request=_request(),
        active_revision_id=_REVISION_ID,
        excluded_document_ids=excluded_document_ids,
    )


def test_complete_group_keeps_all_pieces_in_pack_and_read_units() -> None:
    first = make_ranked_chunk(1, "甲流程先登记。")
    second = make_ranked_chunk(2, "随后由专人复核。")
    third = make_ranked_chunk(3, "复核后归档。")
    group = _group("complete-list", first, second, third)

    pack = _pack(group)
    units = project_evidence_read_units(
        pack.evidence, pack.physical_table_facts
    )

    assert tuple(item.citation_text for item in pack.evidence) == (
        "甲流程先登记。",
        "随后由专人复核。",
        "复核后归档。",
    )
    assert tuple(entry.support_id for entry in pack.entries) == (
        "S1",
        "S2",
        "S3",
    )
    assert all(
        entry.source_group_id == group.group_id
        and entry.admission_status is EvidenceAdmissionStatus.ADMITTED
        for entry in pack.entries
    )
    assert pack.complete_group_ids == (group.group_id,)
    assert pack.partial_group_ids == ()
    assert tuple(unit.text for unit in units) == tuple(
        item.citation_text for item in pack.evidence
    )
    assert tuple(
        support_id for unit in units for support_id in unit.support_ids
    ) == ("S1", "S2", "S3")
    assert all(unit.source_complete for unit in units)
    assert pack.priority_source_units == (
        (
            "A1",
            tuple(stable_support_key(item) for item in pack.evidence),
        ),
    )


def test_partial_group_keeps_incomplete_state_through_read_unit() -> None:
    group = _group(
        "partial-list",
        make_ranked_chunk(4, "甲流程已登记。"),
        make_ranked_chunk(5, "后续材料尚缺一段。"),
        complete=False,
    )

    pack = _pack(group)
    units = project_evidence_read_units(pack.evidence)

    assert pack.complete_group_ids == ()
    assert pack.partial_group_ids == (group.group_id,)
    assert pack.reading_unit_reason_codes == ("SOURCE_NODE_INCOMPLETE",)
    assert len(pack.entries) == len(group.pieces)
    assert all(
        entry.admission_status
        is EvidenceAdmissionStatus.ADMITTED_STRUCTURED_PARTIAL
        for entry in pack.entries
    )
    assert all(not unit.source_complete for unit in units)


def test_one_out_of_scope_piece_hard_rejects_entire_group() -> None:
    visible = make_ranked_chunk(6, "甲流程由经办人登记。")
    excluded = make_ranked_chunk(7, "甲流程由复核人确认。", document_number=3)
    group = _group("mixed-scope", visible, excluded)

    pack = _pack(
        group,
        excluded_document_ids=(excluded.hydrated.chunk.version.document_id,),
    )

    assert pack.entries == ()
    assert pack.evidence == ()
    assert pack.complete_group_ids == ()
    assert pack.partial_group_ids == ()
    assert len(pack.rejected_entries) == 1
    assert pack.rejected_entries[0].hard_reject_reasons == (
        EvidenceAdmissionReason.SCOPE_MISMATCH,
    )
    assert pack.reading_unit_reason_codes == ("CONTEXT_GROUP_REJECTED_HARD",)


def test_support_key_separates_same_source_in_different_reader_groups() -> None:
    candidate = make_ranked_chunk(8, "甲流程须留存原件。")
    first = _pack(_group("reader-group-a", candidate))
    repeated = _pack(_group("reader-group-a", candidate))
    second = _pack(_group("reader-group-b", candidate))

    assert first.evidence[0].citation_text == second.evidence[0].citation_text
    assert first.evidence[0].source_spans == second.evidence[0].source_spans
    assert stable_support_key(first.evidence[0]) == stable_support_key(
        repeated.evidence[0]
    )
    assert stable_support_key(first.evidence[0]) != stable_support_key(
        second.evidence[0]
    )
