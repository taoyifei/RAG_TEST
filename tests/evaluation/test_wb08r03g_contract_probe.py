"""保留 P0 真实形状，并验证 V8 修复后的来源结构合同。"""

from __future__ import annotations

from rag_app.application.answering.grounded import (
    _source_groups,
    _validate_natural_support_structure,
)
from rag_app.core.models import EvidenceItem
from tests.application.answering.test_grounded_claim_v5_quotes import (
    _table_cell,
)
from tests.application.answering.test_natural_grounded_answer import _evidence


def _contiguous_same_row_fragments() -> tuple[EvidenceItem, EvidenceItem]:
    """构造同节点、同表行且首尾连续的两个可引用片段。"""
    first_text = "甲组完成事项一，"
    second_text = "并完成事项二。"
    by_text = {
        item.citation_text: _table_cell(item, 1, 1)
        for item in _evidence(first_text, second_text)
    }
    first, second = by_text[first_text], by_text[second_text]
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


def test_contiguous_same_node_cell_passes_without_group_certificate() -> None:
    """连续的真实节点片段无需虚构组证书即可通过结构门。"""
    first, second = _contiguous_same_row_fragments()
    first_span = first.source_spans[0]
    second_span = second.source_spans[0]

    assert first_span.node_id == second_span.node_id
    assert first_span.source_end_char == second_span.source_start_char
    assert first_span.structural_path == second_span.structural_path
    assert _source_groups(first) == _source_groups(second)
    assert len(_source_groups(first)) == 1

    _validate_natural_support_structure((first, second))
