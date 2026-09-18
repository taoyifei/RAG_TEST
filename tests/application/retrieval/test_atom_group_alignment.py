"""相邻结构证据的逐原子归属边界。"""

from __future__ import annotations

from rag_app.application.retrieval.atom_group_alignment import (
    AlignmentQualification,
    align_atom_to_groups,
)
from rag_app.application.retrieval.evidence_groups import GroupCandidate
from rag_app.application.retrieval.service import _atom_scoped_candidates
from rag_app.core.models import (
    EvidenceGroup,
    EvidenceGroupKind,
    GroupSourceMap,
    RetrievalPolicy,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    QueryAtom,
)
from tests.application.retrieval.helpers import make_ranked_chunk


def _group(
    number: int,
    kind: EvidenceGroupKind,
    texts: tuple[str, ...],
    *,
    heading: tuple[str, ...] = ("通用条款",),
    coordinates: tuple[tuple[str, ...], ...] = (),
) -> GroupCandidate:
    members = tuple(
        make_ranked_chunk(number * 10 + index, text)
        for index, text in enumerate(texts, 1)
    )
    first = members[0].hydrated.chunk
    source_maps = tuple(
        GroupSourceMap(
            chunk_id=member.hydrated.chunk.chunk_id,
            citation_text=member.hydrated.chunk.citation_text,
            source_spans=member.hydrated.chunk.source_spans,
            structural_coordinates=(coordinates[index] if coordinates else ()),
        )
        for index, member in enumerate(members)
    )
    return GroupCandidate(
        group=EvidenceGroup(
            group_id=f"egrp_{number:032x}",
            kind=kind,
            document_id=first.version.document_id,
            document_version_id=first.version.document_version_id,
            index_revision_id=first.index_revision_id,
            section_id=first.section_id,
            display_name="合成资料.docx",
            heading_path=heading,
            member_chunk_ids=tuple(item.chunk_id for item in source_maps),
            member_source_maps=source_maps,
            member_ranks=tuple(range(1, len(members) + 1)),
            group_text_for_model="\n".join(texts),
            complete=True,
            token_cost=sum(
                member.hydrated.chunk.token_count for member in members
            ),
        ),
        members=members,
        rerank_text="\n".join(texts),
    )


def _atom(
    target: str,
    *,
    shape: AtomAnswerShape = AtomAnswerShape.FACT,
    relation: str = "规定",
) -> QueryAtom:
    return QueryAtom(
        atom_id="A1", target=target, relation=relation, answer_shape=shape
    )


def _selected(
    atom: QueryAtom,
    groups: tuple[GroupCandidate, ...],
    links: tuple[AtomCandidateLink, ...] = (),
) -> tuple[GroupCandidate, ...]:
    _candidates, selected, _alignments = _atom_scoped_candidates(
        atom,
        tuple(member for group in groups for member in group.members),
        groups,
        links,
        policy=RetrievalPolicy(),
        multi_atom=True,
    )
    return selected


def test_adjacent_categories_do_not_borrow_list_members() -> None:
    first = _group(
        1,
        EvidenceGroupKind.LIST_GROUP,
        ("甲类文具包括：", "钢笔。", "纸张。"),
    )
    second = _group(
        2,
        EvidenceGroupKind.LIST_GROUP,
        ("乙类用品包括：", "桌椅。", "灯具。"),
    )

    assert _selected(
        _atom("甲类文具", shape=AtomAnswerShape.ENUMERATION),
        (first, second),
    ) == (first,)


def test_same_section_duties_lists_stay_separate() -> None:
    first = _group(
        1,
        EvidenceGroupKind.LIST_GROUP,
        ("甲岗职责：", "核对申请。"),
        heading=("岗位职责",),
    )
    second = _group(
        2,
        EvidenceGroupKind.LIST_GROUP,
        ("乙岗职责：", "归档申请。"),
        heading=("岗位职责",),
    )

    assert _selected(
        _atom("甲岗", shape=AtomAnswerShape.DUTIES), (first, second)
    ) == (first,)


def test_table_header_does_not_open_adjacent_row() -> None:
    first = _group(
        1,
        EvidenceGroupKind.TABLE_ROW_GROUP,
        ("岗位", "甲部门", "五天"),
        coordinates=(("r0:c0",), ("r1:c0",), ("r1:c1",)),
    )
    second = _group(
        2,
        EvidenceGroupKind.TABLE_ROW_GROUP,
        ("岗位", "乙部门", "七天"),
        coordinates=(("r0:c0",), ("r2:c0",), ("r2:c1",)),
    )

    assert _selected(_atom("甲部门"), (first, second)) == (first,)


def test_adjacent_procedures_do_not_borrow_steps() -> None:
    first = _group(
        1,
        EvidenceGroupKind.PROCEDURE_GROUP,
        ("甲流程：", "步骤一提交。", "步骤二复核。"),
        heading=("办理流程",),
    )
    second = _group(
        2,
        EvidenceGroupKind.PROCEDURE_GROUP,
        ("乙流程：", "步骤一登记。", "步骤二归档。"),
        heading=("办理流程",),
    )

    assert _selected(
        _atom("甲流程", shape=AtomAnswerShape.PROCEDURE),
        (first, second),
    ) == (first,)


def test_root_hit_with_strong_anchor_remains_available_to_atom() -> None:
    group = _group(
        1, EvidenceGroupKind.SECTION_GROUP, ("甲设备保管期限为五天。",)
    )
    root_link = AtomCandidateLink(
        unit_id="ROOT",
        atom_id=None,
        chunk_id=group.group.member_chunk_ids[0],
        channels=("lexical",),
        best_rank=1,
        score=1.0,
    )

    assert _selected(_atom("甲设备"), (group,), (root_link,)) == (group,)


def test_no_anchor_never_opens_all_sibling_groups() -> None:
    groups = (
        _group(1, EvidenceGroupKind.LIST_GROUP, ("甲岗：", "核对。")),
        _group(2, EvidenceGroupKind.LIST_GROUP, ("乙岗：", "归档。")),
    )
    atom = _atom("丙岗", shape=AtomAnswerShape.DUTIES)

    assert _selected(atom, groups) == ()
    assert all(
        alignment.qualification is AlignmentQualification.REJECTED
        for alignment in align_atom_to_groups(
            atom, groups, (), RetrievalPolicy()
        )
    )


def test_complete_list_can_certify_structural_relation_from_its_lead_in() -> (
    None
):
    group = _group(
        1,
        EvidenceGroupKind.LIST_GROUP,
        ("甲类文具包括：", "钢笔。", "纸张。"),
    )
    atom = _atom(
        "甲类文具",
        shape=AtomAnswerShape.ENUMERATION,
        relation="组成",
    )

    alignment = align_atom_to_groups(
        atom, (group,), (), RetrievalPolicy()
    )[0]

    assert alignment.qualification is AlignmentQualification.STRONG
    assert alignment.structural_relation_proven
    assert alignment.relation_compatible
    assert alignment.publishable


def test_list_without_target_relation_lead_in_has_no_certificate() -> None:
    group = _group(
        1,
        EvidenceGroupKind.LIST_GROUP,
        ("甲类文具：", "钢笔。", "纸张。"),
    )
    atom = _atom(
        "甲类文具",
        shape=AtomAnswerShape.ENUMERATION,
        relation="组成",
    )

    alignment = align_atom_to_groups(
        atom, (group,), (), RetrievalPolicy()
    )[0]

    assert not alignment.structural_relation_proven
    assert not alignment.publishable


def test_relation_marker_in_sibling_member_cannot_certify_target() -> None:
    group = _group(
        1,
        EvidenceGroupKind.LIST_GROUP,
        ("甲类文具：", "乙类用品包括桌椅。", "钢笔。"),
    )
    atom = _atom(
        "甲类文具",
        shape=AtomAnswerShape.ENUMERATION,
        relation="组成",
    )

    alignment = align_atom_to_groups(
        atom, (group,), (), RetrievalPolicy()
    )[0]

    assert not alignment.structural_relation_proven
    assert not alignment.publishable
