"""真实 Atom 装配链保留来源区间身份及各 Atom 独立认证。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from rag_app.application.retrieval import service as service_module
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.atom_group_alignment import (
    AtomEvidenceQualification,
    EvidenceProvenance,
)
from rag_app.application.retrieval.expansion import RuleBasedNormalizer
from rag_app.application.retrieval.planner import QueryPlanner
from rag_app.application.retrieval.service import RetrievalService
from rag_app.core.models import EvidenceItem, RetrievalPolicy
from rag_app.core.models.common import freeze_json_object
from rag_app.core.models.generation_packet import stable_support_key
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    AtomStatus,
    AtomSupport,
    AtomSupportMatrix,
    QueryAtom,
)
from tests.application.retrieval.test_generation_evidence_pack import (
    _item,
    _plan,
    _request,
)


def _ground(items: tuple[EvidenceItem, ...]) -> tuple:
    """固定上游合法支持，只观察真实 service 的身份去重和证书回映。"""
    service = object.__new__(RetrievalService)
    service._policy = RetrievalPolicy()
    service._analyzer = QueryAnalyzer()
    service._planner = QueryPlanner()
    service._expander = RuleBasedNormalizer()
    service._evidence = Mock()
    service._evidence.assemble_sets.side_effect = [
        SimpleNamespace(
            answer_support_set=(item,),
            model_evidence_candidates=(),
            ambiguous=False,
        )
        for item in items
    ]
    atoms = tuple(
        QueryAtom(
            atom_id=f"A{index}",
            target="甲组",
            relation=relation,
            answer_shape=AtomAnswerShape.FACT,
        )
        for index, relation in enumerate(("登记", "复核"), 1)
    )
    qualification = AtomEvidenceQualification(
        atom_id="A1",
        chunk_id=items[0].chunk_id,
        group_id=None,
        provenance=EvidenceProvenance.ATOM,
        retrieval_relevant=True,
        target_owned=True,
        relation_supported=True,
        constraints_supported=True,
        source_scope_supported=True,
        structure_safe=True,
        publishable=True,
        support_mode=None,
        reason_codes=(),
    )
    with (
        patch.object(
            service_module, "_atom_scoped_candidates", return_value=((), (), ())
        ),
        patch.object(
            service_module, "qualify_atom_evidence", return_value=qualification
        ),
    ):
        return service._ground_atoms(
            request=_request(),
            query_plan=_plan(*atoms),
            candidates=(),
            groups=(),
            links=(),
            selected_slot=None,
            snapshot=Mock(),
            rerank_mode="rerank",
        )


def test_ground_atoms_distinguishes_identical_quote_source_offsets() -> None:
    _, first = _item(1, "甲组负责登记并复核。")
    span = first.source_spans[0]
    later = first.model_copy(
        update={
            "source_spans": (
                span.model_copy(
                    update={
                        "source_start_char": 100,
                        "source_end_char": 100 + len(first.citation_text),
                    }
                ),
            )
        }
    )
    matrix, evidence, _, membership = _ground((first, later))
    assert len(evidence) == 2
    assert (
        matrix.atoms[0].supporting_support_keys
        != matrix.atoms[1].supporting_support_keys
    )
    assert stable_support_key(membership[0][1][0]) != stable_support_key(
        membership[1][1][0]
    )


def test_ground_atoms_preserves_each_atom_certificate_on_shared_source() -> (
    None
):
    _, original = _item(1, "甲组负责登记并复核。")
    items = tuple(
        original.model_copy(
            update={
                "metadata": freeze_json_object(
                    {
                        "answer_support": {
                            "status": "SUPPORTED",
                            "query_target": "甲组",
                            "requested_relation_or_attribute": relation,
                        }
                    }
                )
            }
        )
        for relation in ("登记", "复核")
    )
    matrix, evidence, _, membership = _ground(items)
    assert len(evidence) == 1
    assert (
        matrix.atoms[0].supporting_support_keys
        == matrix.atoms[1].supporting_support_keys
    )
    assert [
        dict(values[0].metadata)["answer_support"][
            "requested_relation_or_attribute"
        ]
        for _, values in membership
    ] == ["登记", "复核"]


@pytest.mark.parametrize("legacy", (False, True))
def test_final_pack_other_source_offset_does_not_become_direct(
    legacy: bool,
) -> None:
    _, first = _item(1, "甲组负责登记并复核。")
    second = first.model_copy(
        update={
            "evidence_id": "S2",
            "source_spans": (
                first.source_spans[0].model_copy(
                    update={
                        "source_start_char": 100,
                        "source_end_char": 100 + len(first.citation_text),
                    }
                ),
            ),
        }
    )
    matrix = AtomSupportMatrix(
        atoms=(
            AtomSupport(
                atom_id="A1",
                status=AtomStatus.SUPPORTED,
                supporting_support_ids=("S1",),
                supporting_support_keys=()
                if legacy
                else (stable_support_key(first),),
            ),
        )
    )
    final = (
        second.model_copy(update={"evidence_id": "S1"}),
        first.model_copy(update={"evidence_id": "S2"}),
    )
    direct = service_module._supported_generation_evidence(
        matrix, (first, second), final
    )
    assert direct == (final[1],)
