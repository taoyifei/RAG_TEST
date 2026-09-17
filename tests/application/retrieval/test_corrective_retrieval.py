"""逐 Atom 闭库纠错只回读已锁定结构组的有界邻居。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from rag_app.application.retrieval.evidence_groups import GroupCandidate
from rag_app.application.retrieval.service import RetrievalService
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    EvidenceGroup,
    EvidenceGroupKind,
    GroupSourceMap,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
    QueryAtom,
    QueryPlan,
    make_query_plan,
)
from tests.application.retrieval.helpers import make_ranked_chunk


def _service(source: Mock) -> RetrievalService:
    service = object.__new__(RetrievalService)
    service._source = source
    service._policy = RetrievalPolicy()
    return service


def _plan(count: int) -> QueryPlan:
    return make_query_plan(
        standalone_query="甲设备的保管期限分别是多少？",
        intent="COMPOUND",
        effort="DEEP",
        atoms=tuple(
            QueryAtom(
                atom_id=f"A{index}",
                target=f"甲设备{index}",
                relation="保管期限",
                answer_shape=AtomAnswerShape.DURATION,
            )
            for index in range(1, count + 1)
        ),
        reason_code="SYNTHETIC_TEST",
        planner_called=False,
    )


def _matrix(count: int) -> AtomSupportMatrix:
    return AtomSupportMatrix(
        atoms=tuple(
            AtomSupport(atom_id=f"A{index}", status=AtomStatus.PARTIAL)
            for index in range(1, count + 1)
        )
    )


def _link(index: int, chunk_id: str) -> AtomCandidateLink:
    return AtomCandidateLink(
        atom_id=f"A{index}",
        chunk_id=chunk_id,
        channels=("lexical",),
        best_rank=1,
        score=1.0,
    )


def _group(index: int, members: tuple[RankedChunk, ...]) -> GroupCandidate:
    first = members[0].hydrated.chunk
    maps = tuple(
        GroupSourceMap(
            chunk_id=member.hydrated.chunk.chunk_id,
            citation_text=member.hydrated.chunk.citation_text,
            source_spans=member.hydrated.chunk.source_spans,
        )
        for member in members
    )
    return GroupCandidate(
        group=EvidenceGroup(
            group_id=f"egrp_{index:032x}",
            kind=EvidenceGroupKind.SECTION_GROUP,
            document_id=first.version.document_id,
            document_version_id=first.version.document_version_id,
            index_revision_id=first.index_revision_id,
            section_id=first.section_id,
            display_name="合成资料.docx",
            member_chunk_ids=tuple(item.chunk_id for item in maps),
            member_source_maps=maps,
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
        rerank_text="",
    )


def test_correction_reads_at_most_twelve_chunks_and_four_groups() -> None:
    anchors: list[RankedChunk] = []
    groups: list[GroupCandidate] = []
    neighbor_by_id: dict[str, RankedChunk] = {}
    links: list[AtomCandidateLink] = []
    for atom_index in range(1, 5):
        members: list[RankedChunk] = []
        for offset in range(3):
            number = atom_index * 100 + offset
            neighbor = make_ranked_chunk(
                number + 10,
                f"甲设备{atom_index}补充条款{offset}。",
                neighbor_group_id=f"group-{atom_index}",
            )
            member = make_ranked_chunk(
                number,
                f"甲设备{atom_index}保管期限待补充。",
                neighbor_group_id=f"group-{atom_index}",
                next_chunk_id=neighbor.hydrated.chunk.chunk_id,
            )
            members.append(member)
            neighbor_by_id[neighbor.hydrated.chunk.chunk_id] = neighbor
        anchors.extend(members)
        groups.append(_group(atom_index, tuple(members)))
        links.append(_link(atom_index, members[0].hydrated.chunk.chunk_id))
    source = Mock()
    source.hydrate_chunks.side_effect = lambda _snapshot, ids: tuple(
        neighbor_by_id[chunk_id].hydrated for chunk_id in ids
    )

    outcome = _service(source)._corrective_retrieval(
        snapshot=object(),  # type: ignore[arg-type]
        candidates=tuple(anchors),
        groups=tuple(groups),
        links=tuple(links),
        matrix=_matrix(4),
        query_plan=_plan(4),
    )

    assert outcome.added_chunk_count == 12
    assert outcome.added_group_count <= 4
    assert len(outcome.candidates) == 24
    assert len(outcome.atom_traces) == 4
    source.section_chunk_ids.assert_not_called()


def test_correction_rejects_cross_document_hydration() -> None:
    wrong = make_ranked_chunk(2, "乙审核材料。", document_number=3)
    anchor = make_ranked_chunk(
        1,
        "甲设备1保管期限待补充。",
        document_number=2,
        next_chunk_id=wrong.hydrated.chunk.chunk_id,
    )
    source = Mock()
    source.hydrate_chunks.return_value = (wrong.hydrated,)

    outcome = _service(source)._corrective_retrieval(
        snapshot=object(),  # type: ignore[arg-type]
        candidates=(anchor,),
        groups=(_group(1, (anchor,)),),
        links=(_link(1, anchor.hydrated.chunk.chunk_id),),
        matrix=_matrix(1),
        query_plan=_plan(1),
    )

    assert outcome.added_chunk_count == 0
    assert outcome.candidates == (anchor,)
    assert outcome.atom_traces[0].reason_code == "NEIGHBORS_OUTSIDE_GROUP"
    source.section_chunk_ids.assert_not_called()


def test_correction_detects_wrong_chunk_identity_from_source() -> None:
    requested = make_ranked_chunk(2, "甲设备1补充条款。")
    returned = make_ranked_chunk(3, "另一条款。")
    anchor = make_ranked_chunk(
        1,
        "甲设备1保管期限待补充。",
        next_chunk_id=requested.hydrated.chunk.chunk_id,
    )
    source = Mock()
    source.hydrate_chunks.return_value = (returned.hydrated,)

    with pytest.raises(IndexCorrupt):
        _service(source)._corrective_retrieval(
            snapshot=object(),  # type: ignore[arg-type]
            candidates=(anchor,),
            groups=(_group(1, (anchor,)),),
            links=(_link(1, anchor.hydrated.chunk.chunk_id),),
            matrix=_matrix(1),
            query_plan=_plan(1),
        )


def test_correction_processes_each_missing_atom() -> None:
    unrelated = make_ranked_chunk(1, "无关条款。")
    neighbor = make_ranked_chunk(3, "甲设备2保管期限五天。")
    anchor = make_ranked_chunk(
        2,
        "甲设备2保管期限待补充。",
        next_chunk_id=neighbor.hydrated.chunk.chunk_id,
    )
    source = Mock()
    source.hydrate_chunks.return_value = (neighbor.hydrated,)

    outcome = _service(source)._corrective_retrieval(
        snapshot=object(),  # type: ignore[arg-type]
        candidates=(unrelated, anchor),
        groups=(_group(1, (unrelated,)), _group(2, (anchor,))),
        links=(
            _link(1, unrelated.hydrated.chunk.chunk_id),
            _link(2, anchor.hydrated.chunk.chunk_id),
        ),
        matrix=_matrix(2),
        query_plan=_plan(2),
    )

    assert outcome.atom_traces[0].reason_code == "NO_QUALIFIED_ANCHOR"
    assert outcome.atom_traces[1].added_chunk_count == 1
    assert outcome.added_chunk_count == 1
