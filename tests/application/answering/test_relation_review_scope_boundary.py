"""新自然路径由批量语义状态决定事实是否取得发布许可。"""

from __future__ import annotations

import pytest

from rag_app.core.models.query_plan import AtomStatus
from tests.application.answering.relation_review_fixtures import (
    fixed_review_generator,
)
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _answer_with_pack,
)
from tests.application.answering.test_natural_grounded_answer import (
    _claim,
    _draft,
    _evidence,
    _plan,
)


@pytest.mark.parametrize(
    "status,accepted", [("supported", True), ("contradicted", False)]
)
def test_semantic_status_controls_publication(
    status: str, accepted: bool
) -> None:
    source = "对于员工跨年领到证书的情况，请在证书领取年份进行报销。"
    evidence = _evidence(source)
    plan = _plan("费用跨年还能报吗？")
    draft = _draft(
        (
            _claim(
                "C1",
                "员工跨年领到证书，在证书领取年份进行报销。",
                "A1",
                "S1",
                source,
            ),
        ),
        plan,
    )
    generator = fixed_review_generator(
        draft,
        statuses=(status,),
    )

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert (outcome.answer is not None) is accepted
    assert outcome.accepted_claim_count == int(accepted)
    assert outcome.published_claim_count == int(accepted)
    assert outcome.atom_coverage == (
        ("A1", "SUPPORTED" if accepted else "MISSING"),
    )
    assert len(outcome.prepared_packets) == 2
    assert all(
        packet.evidence_level == "TRANSPORT_SENT"
        for packet in outcome.prepared_packets
    )


def test_semantic_relation_label_uses_verbatim_source_anchor() -> None:
    """语义标签可以概括，但事实锚点仍必须逐字来自实际发送来源。"""
    source = "对于员工跨年领到证书的情况，请在证书领取年份进行报销。"
    evidence = _evidence(source)
    plan = _plan("费用跨年还能报吗？")
    draft = _draft(
        (
            _claim(
                "C1",
                "员工跨年领到证书，在证书领取年份进行报销。",
                "A1",
                "S1",
                source,
            ),
        ),
        plan,
    )
    generator = fixed_review_generator(
        draft,
    )

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.accepted_claim_count == 1
    assert outcome.published_claim_count == 1
    assert outcome.answer is not None
    assert "进行报销" in outcome.answer
