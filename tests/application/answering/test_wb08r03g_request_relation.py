"""来源分区恢复仍须证明所问关系，完整物理列表不等于回答完整。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from rag_app.core.models.query_plan import AtomAnswerShape, AtomStatus
from rag_app.core.models.retrieval import ClaimSupport, NaturalClaim
from tests.application.answering.source_contract_fixtures import (
    trusted_list_group,
)
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _answer_with_pack,
    _pack,
)
from tests.application.answering.test_natural_grounded_answer import (
    _draft,
    _evidence,
    _plan,
)


@pytest.mark.parametrize(
    "shape", [AtomAnswerShape.FACT, AtomAnswerShape.ENUMERATION]
)
def test_source_partition_cannot_publish_an_unrelated_complete_list(
    shape: AtomAnswerShape,
) -> None:
    """已发送、逐字、真实完整的别题列表也不能升级所问 Atom。"""
    evidence = _evidence(
        "校准活动包括核对仪表、保存读数。",
        "会务活动包括布置场地、登记来宾。",
    )
    plan = _plan("园区交通线路", shape=shape)
    pack = replace(
        _pack(plan, evidence),
        trusted_source_groups=tuple(
            trusted_list_group((item,), group_id="egrp_" + str(index) * 32)
            for index, item in enumerate(evidence, 1)
        ),
    )
    merged = NaturalClaim(
        atom_id="A1",
        text="\n".join(item.citation_text for item in evidence),
        supports=tuple(
            ClaimSupport(support_id=item.support_id, quote=item.citation_text)
            for item in evidence
        ),
    )
    generator = Mock()
    generator.generate.return_value = _draft((merged,), plan)

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),), pack=pack
    )

    assert outcome.answer is None
    assert outcome.accepted_claim_count == 0
    assert outcome.published_claim_count == 0
    assert outcome.atom_coverage == (("A1", "MISSING"),)
    assert outcome.repair_calls == 0
    assert generator.generate.call_count == 1


def test_single_literal_claim_cannot_answer_an_unrelated_question() -> None:
    """直接生成也执行同一问题关系门，不能只防分区恢复入口。"""
    evidence = _evidence("校准活动包括核对仪表、保存读数。")
    plan = _plan("园区交通线路")
    generator = Mock()
    generator.generate.return_value = _draft(
        (
            NaturalClaim(
                atom_id="A1",
                text=evidence[0].citation_text,
                supports=(
                    ClaimSupport(
                        support_id="S1", quote=evidence[0].citation_text
                    ),
                ),
            ),
        ),
        plan,
    )

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.answer is None
    assert outcome.accepted_claim_count == 0
    assert outcome.repair_calls == 0


def test_source_partition_keeps_a_supported_formal_answer() -> None:
    """来源分区后，确实回答同一目标的完整原文仍可发布。"""
    evidence = _evidence(
        "校准活动包括核对仪表、保存读数。",
        "校准活动包括复核量程、归档结论。",
    )
    plan = _plan("校准活动", shape=AtomAnswerShape.ENUMERATION)
    pack = replace(
        _pack(plan, evidence),
        trusted_source_groups=tuple(
            trusted_list_group((item,), group_id="egrp_" + str(index) * 32)
            for index, item in enumerate(evidence, 1)
        ),
    )
    merged = NaturalClaim(
        atom_id="A1",
        text="\n".join(item.citation_text for item in evidence),
        supports=tuple(
            ClaimSupport(support_id=item.support_id, quote=item.citation_text)
            for item in evidence
        ),
    )
    generator = Mock()
    generator.generate.return_value = _draft((merged,), plan)

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),), pack=pack
    )

    assert outcome.answer is not None
    assert outcome.accepted_claim_count == 2
    assert outcome.atom_coverage == (("A1", "SUPPORTED"),)
    assert outcome.repair_calls == 0
