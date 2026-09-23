"""Q1 受控对照入口的来源边界测试。"""

from __future__ import annotations

from typing import cast
from unittest.mock import Mock

import pytest

from evaluation.wanshitong.q1_control_compare import (
    _answering_pack,
    _selected_evidence,
    _selected_units,
    _simple_read,
)
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
)
from rag_app.core.models import EvidenceReadUnit
from rag_app.core.models.query_plan import AtomStatus
from rag_app.core.ports import GenerationRequest
from tests.application.answering.test_natural_grounded_answer import (
    _evidence,
    _matrix,
    _plan,
)


def _units() -> tuple[EvidenceReadUnit, ...]:
    return (
        EvidenceReadUnit(
            unit_id="E1",
            kind="paragraph",
            text="收到申请后2个工作日内审核完成。",
            support_ids=("S1",),
            source_complete=True,
        ),
        EvidenceReadUnit(
            unit_id="E2",
            kind="paragraph",
            text="另一个流程的时限为5个自然日。",
            support_ids=("S2",),
            source_complete=True,
        ),
    )


def test_manual_selection_cannot_add_or_duplicate_unsent_units() -> None:
    units = _units()

    assert _selected_units(units, ("E2", "E1")) == units
    with pytest.raises(ValueError, match="COMPARE_SELECTION_NOT_SENT"):
        _selected_units(units, ("E3",))
    with pytest.raises(ValueError, match="COMPARE_SELECTION_DUPLICATE"):
        _selected_units(units, ("E1", "E1"))


def test_comparison_pack_keeps_only_selected_source_dependencies() -> None:
    evidence = _evidence(
        "收到申请后2个工作日内审核完成。",
        "另一个流程的时限为5个自然日。",
    )
    plan = _plan("申请审核")
    matrix = _matrix(plan, ((AtomStatus.SUPPORTED, ("S1", "S2")),))
    request = GenerationRequest(
        query="收到申请后多久审核完成？",
        evidence=evidence,
        citation_protocol="support-id-v3-quoted-natural-claims",
        query_plan=plan,
        atom_support_matrix=matrix,
        per_atom_candidate_support_ids=(("A1", ("S1", "S2")),),
        evidence_read_units=_units(),
    )

    selected = _selected_units(_units(), ("E1",))
    pack = _answering_pack(
        request, selected, _selected_evidence(request, selected)
    )

    assert tuple(item.support_id for item in pack.evidence) == ("S1",)
    assert pack.per_atom_candidate_support_ids == (("A1", ("S1",)),)
    assert not pack.physical_table_facts


def test_simple_read_rejects_model_reference_outside_same_packet() -> None:
    evidence = _evidence("收到申请后2个工作日内审核完成。")
    unit = _units()[0]
    fake_adapter = Mock()
    fake_adapter.complete.return_value = Mock(
        content='{"answer":"2个工作日","refs":["E2"]}',
        model="synthetic-model",
        usage=Mock(model_dump=Mock(return_value={})),
    )

    with pytest.raises(ValueError, match="COMPARE_SIMPLE_REF_OUTSIDE_SENT"):
        _simple_read(
            cast(OpenAICompatibleChatAdapter, fake_adapter),
            "收到申请后多久审核完成？",
            (unit,),
            evidence,
        )
