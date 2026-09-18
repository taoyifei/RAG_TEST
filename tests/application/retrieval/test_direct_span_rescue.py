"""逐原子直接证据补救只对真实引用和硬门生效。"""

from __future__ import annotations

from rag_app.application.retrieval.atom_group_alignment import (
    AlignmentQualification,
    AtomGroupAlignment,
    EvidenceSupportMode,
    qualify_atom_evidence,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomCandidateLink,
    AtomConstraintKind,
    QueryAtom,
    QueryConstraint,
)
from tests.application.answering.test_natural_grounded_answer import _evidence


def _atom(
    *,
    shape: AtomAnswerShape = AtomAnswerShape.DURATION,
    constraints: tuple[QueryConstraint, ...] = (),
) -> QueryAtom:
    return QueryAtom(
        atom_id="A1",
        target="甲设备",
        relation="维护期限",
        answer_shape=shape,
        constraints=constraints,
    )


def _linked(
    *,
    root: bool,
    relation_supported: bool = True,
) -> tuple[object, AtomCandidateLink]:
    item = _evidence("甲设备维护期限为五天。")[0]
    item = item.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "answer_support": {
                        "status": "SUPPORTED"
                        if relation_supported
                        else "UNSUPPORTED"
                    }
                }
            )
        }
    )
    link = AtomCandidateLink(
        atom_id=None if root else "A1",
        unit_id="ROOT" if root else "A1",
        chunk_id=item.chunk_id,
        channels=("exact",),
        best_rank=1,
        score=1.0,
    )
    return item, link


def test_scalar_direct_root_rescue_requires_trusted_resolved_relation() -> None:
    item, link = _linked(root=True)
    qualified = qualify_atom_evidence(
        _atom(),
        item,
        (link,),
        alignment=None,
        resolved_root_query="甲设备 维护期限",
        context_resolution_confidence="MEDIUM",
    )
    unsafe = qualify_atom_evidence(
        _atom(),
        item,
        (link,),
        alignment=None,
        resolved_root_query="甲设备 维护期限",
        context_resolution_confidence="LOW",
    )

    assert qualified.publishable
    assert qualified.support_mode is EvidenceSupportMode.DIRECT_ROOT_SPAN
    assert unsafe.retrieval_relevant
    assert not unsafe.publishable
    assert "ROOT_RESCUE_UNTRUSTED" in unsafe.reason_codes


def test_direct_atom_rescue_keeps_candidate_on_failure() -> None:
    item, link = _linked(root=False, relation_supported=False)
    failed = qualify_atom_evidence(
        _atom(),
        item,
        (link,),
        alignment=None,
        resolved_root_query="甲设备 维护期限",
    )
    supported_item, supported_link = _linked(root=False)
    passed = qualify_atom_evidence(
        _atom(),
        supported_item,
        (supported_link,),
        alignment=None,
        resolved_root_query="甲设备 维护期限",
    )

    assert failed.retrieval_relevant
    assert not failed.publishable
    assert "ATOM_RELATION_UNSUPPORTED" in failed.reason_codes
    assert passed.publishable
    assert passed.support_mode is EvidenceSupportMode.DIRECT_ATOM_SPAN


def test_single_atom_direct_evidence_has_no_link_requirement() -> None:
    item, _link = _linked(root=False)
    qualified = qualify_atom_evidence(
        _atom(),
        item,
        (),
        alignment=None,
        resolved_root_query="甲设备 维护期限",
        single_atom_direct=True,
    )
    unrelated = qualify_atom_evidence(
        _atom(),
        item,
        (),
        alignment=None,
        resolved_root_query="甲设备 维护期限",
    )

    assert qualified.publishable
    assert qualified.support_mode is EvidenceSupportMode.DIRECT_ATOM_SPAN
    assert not unrelated.publishable


def test_hard_constraint_and_structure_restrict_direct_span() -> None:
    item, link = _linked(root=False)
    constrained = qualify_atom_evidence(
        _atom(
            constraints=(
                QueryConstraint(kind=AtomConstraintKind.NUMBER, value="六天"),
            )
        ),
        item,
        (link,),
        alignment=None,
        resolved_root_query="甲设备 维护期限",
    )
    enumeration = qualify_atom_evidence(
        _atom(shape=AtomAnswerShape.ENUMERATION),
        item,
        (link,),
        alignment=None,
        resolved_root_query="甲设备 维护期限",
    )

    assert constrained.retrieval_relevant
    assert not constrained.publishable
    assert "ATOM_CONSTRAINT_UNVERIFIED" in constrained.reason_codes
    assert not enumeration.publishable
    assert "ATOM_STRUCTURE_UNSAFE" in enumeration.reason_codes


def test_group_alignment_relation_is_a_publication_gate() -> None:
    item, link = _linked(root=False)
    group_id = "egrp_" + "1" * 32
    item = item.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    **dict(item.metadata),
                    "evidence_group_id": group_id,
                    "group_complete": True,
                }
            )
        }
    )
    alignment = AtomGroupAlignment(
        atom_id="A1",
        group_id=group_id,
        document_version_id=item.document_version_id,
        provenance_hit=True,
        target_anchor_score=1.0,
        relation_compatible=False,
        constraint_checks=(),
        qualification=AlignmentQualification.STRONG,
        publishable=False,
        reason_codes=("ATOM_RELATION_UNVERIFIED",),
    )

    qualified = qualify_atom_evidence(
        _atom(),
        item,
        (link,),
        alignment=alignment,
        resolved_root_query="甲设备 维护期限",
    )

    assert qualified.retrieval_relevant
    assert not qualified.publishable
    assert not qualified.relation_supported
    assert "ATOM_ALIGNMENT_NOT_PUBLISHABLE" in qualified.reason_codes


def test_group_alignment_hard_constraint_is_a_publication_gate() -> None:
    item, link = _linked(root=False)
    group_id = "egrp_" + "2" * 32
    item = item.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    **dict(item.metadata),
                    "evidence_group_id": group_id,
                    "group_complete": True,
                }
            )
        }
    )
    alignment = AtomGroupAlignment(
        atom_id="A1",
        group_id=group_id,
        document_version_id=item.document_version_id,
        provenance_hit=True,
        target_anchor_score=1.0,
        relation_compatible=True,
        constraint_checks=(("NUMBER", False),),
        qualification=AlignmentQualification.STRONG,
        publishable=False,
        reason_codes=("ATOM_CONSTRAINT_UNVERIFIED",),
    )

    qualified = qualify_atom_evidence(
        _atom(),
        item,
        (link,),
        alignment=alignment,
        resolved_root_query="甲设备 维护期限",
    )

    assert qualified.retrieval_relevant
    assert not qualified.publishable
    assert not qualified.constraints_supported
    assert "ATOM_CONSTRAINT_UNVERIFIED" in qualified.reason_codes


def test_complete_group_can_publish_member_with_structural_certificate() -> (
    None
):
    item, link = _linked(root=False, relation_supported=False)
    group_id = "egrp_" + "4" * 32
    item = item.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    **dict(item.metadata),
                    "evidence_group_id": group_id,
                    "group_complete": True,
                }
            )
        }
    )
    alignment = AtomGroupAlignment(
        atom_id="A1",
        group_id=group_id,
        document_version_id=item.document_version_id,
        provenance_hit=True,
        target_anchor_score=1.0,
        relation_compatible=True,
        constraint_checks=(),
        qualification=AlignmentQualification.STRONG,
        publishable=True,
        reason_codes=(),
        structural_relation_proven=True,
    )
    atom = _atom(shape=AtomAnswerShape.ENUMERATION)

    qualified = qualify_atom_evidence(
        atom,
        item,
        (link,),
        alignment=alignment,
        resolved_root_query="甲设备 维护期限",
    )

    assert qualified.relation_supported
    assert qualified.publishable
    assert qualified.support_mode is EvidenceSupportMode.ALIGNED_COMPLETE_GROUP
