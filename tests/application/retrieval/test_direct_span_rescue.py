"""逐原子直接证据补救只对真实引用和硬门生效。"""

from __future__ import annotations

from rag_app.application.retrieval.atom_group_alignment import (
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
