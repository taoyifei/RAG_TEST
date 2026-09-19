"""WB08R-03G-P0 已确认合同断点的真实函数反例。"""

from __future__ import annotations

import pytest

from rag_app.application.answering.grounded import (
    _source_groups,
    _validate_natural_support_structure,
)
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import EvidenceItem
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _table_cell,
)
from tests.application.answering.test_natural_grounded_answer import _evidence


def _contiguous_same_row_fragments() -> tuple[EvidenceItem, EvidenceItem]:
    """构造同节点、同表行且首尾连续的两个可引用片段。"""
    first_text = "甲组完成事项一，"
    second_text = "并完成事项二。"
    first, second = (
        _table_cell(item, 1, 1)
        for item in _evidence(first_text, second_text)
    )
    first_span = first.source_spans[0]
    second_span = second.source_spans[0]
    second = second.model_copy(
        update={
            "source_spans": (
                second_span.model_copy(
                    update={
                        "node_id": first_span.node_id,
                        "source_start_char": first_span.source_end_char,
                        "source_end_char": (
                            first_span.source_end_char + len(second_text)
                        ),
                    }
                ),
            )
        }
    )
    return first, second


def test_same_row_identity_is_lost_before_source_group_recovery() -> None:
    """真实分组认出同一行，但前置结构门仍以缺组证书拒绝。"""
    first, second = _contiguous_same_row_fragments()
    first_span = first.source_spans[0]
    second_span = second.source_spans[0]

    assert first_span.node_id == second_span.node_id
    assert first_span.source_end_char == second_span.source_start_char
    assert first_span.structural_path == second_span.structural_path
    assert _source_groups(first) == _source_groups(second)
    assert len(_source_groups(first)) == 1

    with pytest.raises(ValidationFailed) as error:
        _validate_natural_support_structure((first, second))

    assert error.value.code == "CLAIM_SOURCE_MISMATCH"
    assert dict(error.value.details)["validator"] == (
        "_validate_natural_support_structure"
    )
