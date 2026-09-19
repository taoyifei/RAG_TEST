"""阶段前缀属于事实正文，主体识别不能删除其后的数量关系。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from rag_app.core.models.query_plan import AtomAnswerShape, AtomStatus
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
    ("claim", "accepted"),
    [
        ("上线前2-3个月。", True),
        ("上线前3-4个月。", False),
        ("验收后2-3个月。", False),
    ],
)
def test_final_time_range_keeps_its_source_stage(
    claim: str, accepted: bool
) -> None:
    """完整服务接受原阶段同数值，拒绝借另一阶段或改变时限。"""
    source = "评测系统在上线前2-3个月安排兼容性检验。"
    evidence = _evidence(source)
    plan = _plan("评测系统", shape=AtomAnswerShape.DURATION)
    generator = Mock()
    generator.generate.return_value = _draft(
        (_claim("C1", claim, "A1", "S1", source),), plan
    )

    outcome = _answer_with_pack(
        generator, plan, evidence, ((AtomStatus.MISSING, ()),)
    )

    assert outcome.accepted_claim_count == int(accepted)
    if accepted:
        assert outcome.answer == f"{claim} [S1]"
    else:
        assert claim not in (outcome.answer or "")
        assert outcome.raw_failures == (("A1", "CLAIM_NUMBER_UNSUPPORTED"),)
    assert outcome.repair_calls == 0
    assert generator.generate.call_count == 1
