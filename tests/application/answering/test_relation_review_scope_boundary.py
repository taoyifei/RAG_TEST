"""严格 JSON 的 supported 仍不能新增来源没有的主体或关系。"""

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
    "subject,relation", [("员工", "费用报销"), ("所有人员", "报销")]
)
def test_model_supported_cannot_add_unquoted_scope(
    subject: str, relation: str
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
        subject=subject,
        relation=relation,
        conditions=("跨年领到证书", "证书领取年份"),
    )

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.answer is None
    assert outcome.accepted_claim_count == 0
    assert outcome.published_claim_count == 0
    assert outcome.atom_coverage == (("A1", "MISSING"),)
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
        subject="员工",
        relation="跨年费用处理职责",
        relation_anchor="报销",
        conditions=("跨年领到证书", "证书领取年份"),
    )

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.accepted_claim_count == 1
    assert outcome.published_claim_count == 1
    assert outcome.answer is not None
    assert "进行报销" in outcome.answer
