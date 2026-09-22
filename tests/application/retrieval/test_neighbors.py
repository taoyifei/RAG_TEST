from __future__ import annotations

from typing import cast

import pytest

from rag_app.application.retrieval.evidence_groups import build_evidence_groups
from rag_app.application.retrieval.neighbors import NeighborExpander
from rag_app.application.retrieval.service import RetrievalService
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ChunkRole,
    HydratedChunk,
    RankedChunk,
    RetrievalPolicy,
)
from rag_app.core.ports import EvidenceSourcePort
from tests.application.retrieval.helpers import make_ranked_chunk


class _NeighborSource:
    def __init__(self, chunks: tuple[HydratedChunk, ...]) -> None:
        self._chunks = {item.chunk.chunk_id: item for item in chunks}

    def hydrate_chunks(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        chunk_ids: tuple[str, ...],
    ) -> tuple[HydratedChunk, ...]:
        del snapshot
        return tuple(self._chunks[item] for item in chunk_ids)

    def section_chunk_ids(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        *,
        document_version_id: str,
        section_id: str,
        limit: int,
    ) -> tuple[str, ...]:
        del snapshot
        return tuple(
            item.chunk.chunk_id
            for item in self._chunks.values()
            if item.chunk.version.document_version_id == document_version_id
            and item.chunk.section_id == section_id
        )[:limit]


class _CountingNeighborSource(_NeighborSource):
    """记录结构闭合的批量 Hydration 次数。"""

    def __init__(self, chunks: tuple[HydratedChunk, ...]) -> None:
        super().__init__(chunks)
        self.hydrate_calls = 0

    def hydrate_chunks(
        self,
        snapshot: ActiveRevisionQuerySnapshot,
        chunk_ids: tuple[str, ...],
    ) -> tuple[HydratedChunk, ...]:
        self.hydrate_calls += 1
        return super().hydrate_chunks(snapshot, chunk_ids)


def _snapshot() -> ActiveRevisionQuerySnapshot:
    return cast(ActiveRevisionQuerySnapshot, object())


def _table_chain(
    base: int,
    *,
    document_number: int,
    display_name: str,
    length: int = 9,
) -> tuple[RankedChunk, ...]:
    """构造带真实双向链接和表格坐标的合成 chunk 链。"""
    chunk_ids = [f"chunk_{base + index:032x}" for index in range(length)]
    result = []
    for index, chunk_id in enumerate(chunk_ids):
        candidate = make_ranked_chunk(
            base + index,
            f"角色职责片段 {index}",
            role=ChunkRole.TABLE,
            document_number=document_number,
            section_id=f"section-{document_number}",
            neighbor_group_id=f"group-{document_number}",
            previous_chunk_id=chunk_ids[index - 1] if index else None,
            next_chunk_id=(
                chunk_ids[index + 1] if index + 1 < length else None
            ),
        )
        chunk = candidate.hydrated.chunk
        span = chunk.source_spans[0]
        assert span.source_anchor is not None
        path = (
            "body",
            f"tbl:{document_number}",
            f"tr:{index}",
            "tc:0",
            f"p:{index}",
        )
        anchor = span.source_anchor.model_copy(update={"structural_path": path})
        span = span.model_copy(
            update={"source_anchor": anchor, "structural_path": path}
        )
        chunk = chunk.model_copy(
            update={"chunk_id": chunk_id, "source_spans": (span,)}
        )
        result.append(
            candidate.model_copy(
                update={
                    "hydrated": candidate.hydrated.model_copy(
                        update={"chunk": chunk, "display_name": display_name}
                    )
                }
            )
        )
    return tuple(result)


@pytest.mark.parametrize(
    ("mode", "role", "reason"),
    (
        ("same_group", ChunkRole.TEXT, "SAME_GROUP_NEIGHBOR"),
        ("table", ChunkRole.TABLE, "TABLE_CONTINUITY"),
    ),
)
def test_neighbor_and_table_expansion_require_bidirectional_links(
    mode: str, role: ChunkRole, reason: str
) -> None:
    previous = make_ranked_chunk(
        1,
        "previous",
        role=role,
        next_chunk_id=f"chunk_{2:032x}",
    )
    origin = make_ranked_chunk(
        2,
        "origin",
        role=role,
        previous_chunk_id=previous.hydrated.chunk.chunk_id,
    )
    source = cast(EvidenceSourcePort, _NeighborSource((previous.hydrated,)))
    outcome = NeighborExpander(source).expand(
        _snapshot(), (origin,), mode, RetrievalPolicy()
    )

    assert [item.hydrated.chunk.chunk_id for item in outcome.candidates] == [
        origin.hydrated.chunk.chunk_id,
        previous.hydrated.chunk.chunk_id,
    ]
    assert outcome.candidates[1].expansion_reason == reason


def test_ranked_prose_closes_same_source_node_across_chunks() -> None:
    """正文长句切块后，生成证据还能读到逗号后的原文。"""
    first = make_ranked_chunk(
        1,
        "员工通过认证后，在规定时间内以部门为单位，",
        next_chunk_id=f"chunk_{2:032x}",
    ).model_copy(update={"rerank_rank": 1})
    second = make_ranked_chunk(
        2,
        "提交发票至管理部门报销。",
        previous_chunk_id=first.hydrated.chunk.chunk_id,
    )
    first_span = first.hydrated.chunk.source_spans[0]
    second_span = second.hydrated.chunk.source_spans[0]
    assert first_span.source_anchor is not None
    assert second_span.source_anchor is not None
    continuation = second_span.model_copy(
        update={
            "node_id": first_span.node_id,
            "source_start_char": len(first.hydrated.chunk.citation_text),
            "source_end_char": (
                len(first.hydrated.chunk.citation_text)
                + len(second.hydrated.chunk.citation_text)
            ),
            "source_anchor": second_span.source_anchor.model_copy(
                update={"ordinal": first_span.source_anchor.ordinal}
            ),
        }
    )
    second = second.model_copy(
        update={
            "hydrated": second.hydrated.model_copy(
                update={
                    "chunk": second.hydrated.chunk.model_copy(
                        update={"source_spans": (continuation,)}
                    )
                }
            )
        }
    )
    source = cast(EvidenceSourcePort, _NeighborSource((second.hydrated,)))

    outcome = NeighborExpander(source).close_source_nodes(
        _snapshot(), (first,), RetrievalPolicy()
    )

    assert len(outcome.candidates) == 2
    assert outcome.candidates[1].hydrated.chunk.chunk_id == (
        second.hydrated.chunk.chunk_id
    )
    assert outcome.candidates[1].expansion_reason == "SOURCE_NODE_CONTINUATION"


def test_ranked_table_cell_closes_only_same_row_node() -> None:
    """表格单元格被切块后可补原文后半段，邻行不借用。"""
    first = make_ranked_chunk(
        1,
        "甲团队负责完成系统联调，",
        role=ChunkRole.TABLE,
        next_chunk_id=f"chunk_{2:032x}",
    ).model_copy(update={"rerank_rank": 1})
    second = make_ranked_chunk(
        2,
        "解决接口兼容性问题。",
        role=ChunkRole.TABLE,
        previous_chunk_id=first.hydrated.chunk.chunk_id,
    )
    first_span = first.hydrated.chunk.source_spans[0]
    second_span = second.hydrated.chunk.source_spans[0]
    assert first_span.source_anchor is not None
    assert second_span.source_anchor is not None
    path = ("body", "tbl:1", "tr:1", "tc:1", "p:1")
    first = first.model_copy(
        update={
            "hydrated": first.hydrated.model_copy(
                update={
                    "chunk": first.hydrated.chunk.model_copy(
                        update={
                            "source_spans": (
                                first_span.model_copy(
                                    update={"structural_path": path}
                                ),
                            )
                        }
                    )
                }
            )
        }
    )
    continuation = second_span.model_copy(
        update={
            "node_id": first_span.node_id,
            "structural_path": path,
            "source_start_char": len(first.hydrated.chunk.citation_text),
            "source_end_char": (
                len(first.hydrated.chunk.citation_text)
                + len(second.hydrated.chunk.citation_text)
            ),
        }
    )
    second = second.model_copy(
        update={
            "hydrated": second.hydrated.model_copy(
                update={
                    "chunk": second.hydrated.chunk.model_copy(
                        update={"source_spans": (continuation,)}
                    )
                }
            )
        }
    )
    source = cast(EvidenceSourcePort, _NeighborSource((second.hydrated,)))
    outcome = NeighborExpander(source).close_source_nodes(
        _snapshot(), (first,), RetrievalPolicy()
    )
    assert len(outcome.candidates) == 2
    assert outcome.candidates[1].expansion_reason == "SOURCE_NODE_CONTINUATION"

    other_row = continuation.model_copy(
        update={
            "structural_path": ("body", "tbl:1", "tr:2", "tc:1", "p:1")
        }
    )
    wrong = second.model_copy(
        update={
            "hydrated": second.hydrated.model_copy(
                update={
                    "chunk": second.hydrated.chunk.model_copy(
                        update={"source_spans": (other_row,)}
                    )
                }
            )
        }
    )
    wrong_source = cast(EvidenceSourcePort, _NeighborSource((wrong.hydrated,)))
    refused = NeighborExpander(wrong_source).close_source_nodes(
        _snapshot(), (first,), RetrievalPolicy()
    )
    assert refused.candidates == (first,)


def test_section_expansion_is_bounded() -> None:
    origin = make_ranked_chunk(1, "origin")
    sibling = make_ranked_chunk(2, "sibling")
    source = cast(
        EvidenceSourcePort,
        _NeighborSource((origin.hydrated, sibling.hydrated)),
    )
    outcome = NeighborExpander(source).expand(
        _snapshot(),
        (origin,),
        "section",
        RetrievalPolicy(section_chunk_limit=2),
    )

    assert len(outcome.candidates) == 2
    assert outcome.candidates[1].expansion_reason == "SECTION_SIBLING"


def test_section_expansion_recovers_preceding_list_stages() -> None:
    """命中中段列表时，优先补同章节前一组的准备段落。"""
    section = "section-stages"
    chunks = (
        make_ranked_chunk(
            10,
            "无关早期段落。",
            role=ChunkRole.LIST,
            section_id=section,
            neighbor_group_id="group-earlier",
        ),
        make_ranked_chunk(
            11,
            "流程包含准备、申报和审核。",
            role=ChunkRole.LIST,
            section_id=section,
            neighbor_group_id="group-intro",
        ),
        make_ranked_chunk(
            12,
            "（一）准备阶段应编制材料。",
            role=ChunkRole.LIST,
            section_id=section,
            neighbor_group_id="group-prep",
        ),
        make_ranked_chunk(
            13,
            "夹在阶段之间的正文。",
            role=ChunkRole.TEXT,
            section_id=section,
            neighbor_group_id="group-text",
        ),
        make_ranked_chunk(
            14,
            "（二）申报阶段提交材料。",
            role=ChunkRole.LIST,
            section_id=section,
            neighbor_group_id="group-current",
        ),
    )
    source = cast(
        EvidenceSourcePort,
        _NeighborSource(tuple(item.hydrated for item in chunks)),
    )
    outcome = NeighborExpander(source).expand(
        _snapshot(),
        (chunks[-1],),
        "section",
        RetrievalPolicy(
            section_chunk_limit=2,
            section_search_limit=5,
            section_predecessor_max_gap=4,
        ),
    )

    assert [item.hydrated.chunk.chunk_id for item in outcome.candidates] == [
        chunks[-1].hydrated.chunk.chunk_id,
        chunks[2].hydrated.chunk.chunk_id,
        chunks[1].hydrated.chunk.chunk_id,
    ]
    assert [item.expansion_reason for item in outcome.candidates[1:]] == [
        "SECTION_PREDECESSOR",
        "SECTION_PREDECESSOR",
    ]


def test_list_chain_closes_from_middle_with_heading_intro() -> None:
    """检索只命中中段时，结构组仍保留完整顺序和真实章节导语。"""
    chunk_ids = tuple(f"chunk_{number:032x}" for number in range(100, 107))
    chain = tuple(
        make_ranked_chunk(
            100 + index,
            f"第 {index + 1} 项。",
            role=ChunkRole.LIST,
            neighbor_group_id="seven-steps",
            previous_chunk_id=chunk_ids[index - 1] if index else None,
            next_chunk_id=(
                chunk_ids[index + 1] if index + 1 < len(chunk_ids) else None
            ),
        )
        for index in range(len(chunk_ids))
    )
    chain = tuple(
        item.model_copy(
            update={
                "hydrated": item.hydrated.model_copy(
                    update={
                        "chunk": item.hydrated.chunk.model_copy(
                            update={"heading_path": ("办理流程",)}
                        )
                    }
                )
            }
        )
        for item in chain
    )
    source = cast(
        EvidenceSourcePort,
        _NeighborSource(tuple(item.hydrated for item in chain)),
    )
    service = object.__new__(RetrievalService)
    service._neighbors = NeighborExpander(source)
    service._policy = RetrievalPolicy(group_member_chunk_limit=8)

    closed = service._close_structural_context(_snapshot(), (chain[3],))
    groups = build_evidence_groups(
        closed.candidates,
        max_groups=8,
        max_member_chunks=8,
        rerank_text_char_limit=2400,
    )

    assert len(closed.candidates) == 7
    assert len(groups) == 1
    assert groups[0].complete
    assert groups[0].group.member_chunk_ids == chunk_ids


def test_structure_closure_batches_multiple_table_chains() -> None:
    """多个表格种子共享每层的一次 Hydration，仍保留各自来源。"""
    first = _table_chain(
        300,
        document_number=30,
        display_name="合成职责表一.docx",
        length=5,
    )
    second = _table_chain(
        400,
        document_number=40,
        display_name="合成职责表二.docx",
        length=5,
    )
    source = _CountingNeighborSource(
        tuple(item.hydrated for item in (*first, *second))
    )
    expander = NeighborExpander(cast(EvidenceSourcePort, source))

    outcome = expander.close_structure(
        _snapshot(),
        (first[2], second[2]),
        RetrievalPolicy(group_member_chunk_limit=8),
    )

    assert len(outcome.candidates) == 10
    assert source.hydrate_calls == 2
    assert not outcome.degraded_reason_codes


def test_section_mode_closes_top_table_row_before_section_siblings() -> None:
    """复杂问题首名命中表格时优先闭合当前行，而非退化为章节截断。"""
    chain = _table_chain(
        30,
        document_number=3,
        display_name="合成职责表.docx",
        length=5,
    )
    table_node_id = f"node_{'a' * 32}"
    with_rows: list[RankedChunk] = []
    for candidate in chain:
        chunk = candidate.hydrated.chunk.model_copy(
            update={
                "metadata": (
                    (
                        "atoms",
                        [
                            {
                                "role": "table",
                                "metadata": {
                                    "row_index": 1,
                                    "table_node_id": table_node_id,
                                },
                            }
                        ],
                    ),
                )
            }
        )
        with_rows.append(
            candidate.model_copy(
                update={
                    "hydrated": candidate.hydrated.model_copy(
                        update={"chunk": chunk}
                    )
                }
            )
        )
    source = cast(
        EvidenceSourcePort,
        _NeighborSource(tuple(item.hydrated for item in with_rows)),
    )

    outcome = NeighborExpander(source).expand(
        _snapshot(),
        (with_rows[-1],),
        "section",
        RetrievalPolicy(max_evidence_items=8, section_chunk_limit=1),
    )

    assert {item.hydrated.chunk.chunk_id for item in outcome.candidates} == {
        item.hydrated.chunk.chunk_id for item in with_rows
    }
    assert all(
        item.expansion_reason == "TABLE_CONTINUITY"
        for item in outcome.candidates[1:]
    )


def test_neighbor_link_damage_degrades_without_crossing_boundary() -> None:
    previous = make_ranked_chunk(1, "previous")
    origin = make_ranked_chunk(
        2,
        "origin",
        previous_chunk_id=previous.hydrated.chunk.chunk_id,
    )
    source = cast(EvidenceSourcePort, _NeighborSource((previous.hydrated,)))
    outcome = NeighborExpander(source).expand(
        _snapshot(), (origin,), "same_group", RetrievalPolicy()
    )

    assert outcome.candidates == (origin,)
    assert outcome.degraded_reason_codes == ("NEIGHBOR_INDEX_CORRUPT",)


def test_simple_fact_closes_detected_table_chain() -> None:
    """普通事实检索命中分段表格时，也闭合同组结构链。"""
    chain = _table_chain(
        50,
        document_number=5,
        display_name="合成分级时限表.docx",
        length=5,
    )
    source = cast(
        EvidenceSourcePort,
        _NeighborSource(tuple(item.hydrated for item in chain)),
    )

    outcome = NeighborExpander(source).expand(
        _snapshot(),
        (chain[2],),
        "same_group",
        RetrievalPolicy(max_evidence_items=8),
    )

    assert {item.hydrated.chunk.chunk_id for item in outcome.candidates} == {
        item.hydrated.chunk.chunk_id for item in chain
    }
    assert all(
        item.expansion_reason == "TABLE_CONTINUITY"
        for item in outcome.candidates[1:]
    )


def test_table_expansion_closes_same_row_before_adjacent_rows() -> None:
    """融合窗口较小时，长逻辑行的行名仍应先于相邻行进入候选。"""
    chain = _table_chain(
        200,
        document_number=6,
        display_name="合成长表格.docx",
        length=22,
    )
    row_indices = (
        0,
        *(1 for _ in range(7)),
        *(2 for _ in range(7)),
        *(3 for _ in range(7)),
    )
    table_node_id = f"node_{'f' * 32}"
    with_rows: list[RankedChunk] = []
    for candidate, row_index in zip(chain, row_indices, strict=True):
        chunk = candidate.hydrated.chunk.model_copy(
            update={
                "metadata": (
                    (
                        "atoms",
                        [
                            {
                                "role": "table",
                                "metadata": {
                                    "row_index": row_index,
                                    "table_node_id": table_node_id,
                                },
                            }
                        ],
                    ),
                )
            }
        )
        with_rows.append(
            candidate.model_copy(
                update={
                    "hydrated": candidate.hydrated.model_copy(
                        update={"chunk": chunk}
                    )
                }
            )
        )
    stored = tuple(item.hydrated for item in with_rows)
    source = cast(EvidenceSourcePort, _NeighborSource(stored))
    # 目标行是索引 8..14；模拟 reranker 命中行尾和部分正文，但丢掉行首。
    seeds = tuple(
        with_rows[index] for index in (14, 10, 13, 0, 3, 5, 16, 18, 20, 21)
    )

    outcome = NeighborExpander(source).expand(
        _snapshot(),
        seeds,
        "same_group",
        RetrievalPolicy(
            fusion_candidate_limit=12,
            max_evidence_items=16,
        ),
    )

    candidate_ids = {
        item.hydrated.chunk.chunk_id for item in outcome.candidates
    }
    target_row_ids = {item.hydrated.chunk.chunk_id for item in with_rows[8:15]}
    assert target_row_ids <= candidate_ids
    assert len(outcome.candidates) <= 16


def test_table_expansion_prioritizes_a_uniquely_qualified_source() -> None:
    noise_chains = tuple(
        _table_chain(
            100 * number,
            document_number=number,
            display_name=f"白鹭流程制度-{number}.docx",
        )
        for number in range(1, 6)
    )
    selected = _table_chain(
        900,
        document_number=9,
        display_name="蓝熊交付规范.docx",
        length=7,
    )
    seeds = (*[chain[0] for chain in noise_chains], selected[0])
    stored = tuple(
        item.hydrated for chain in (*noise_chains, selected) for item in chain
    )
    source = cast(EvidenceSourcePort, _NeighborSource(stored))
    expander = NeighborExpander(source)
    policy = RetrievalPolicy(fusion_candidate_limit=48, max_evidence_items=8)

    unqualified = expander.expand(_snapshot(), seeds, "table", policy)
    qualified = expander.expand(
        _snapshot(),
        seeds,
        "table",
        policy,
        source_qualifier="蓝熊规范",
    )

    selected_ids = {item.hydrated.chunk.chunk_id for item in selected}
    assert not selected_ids <= {
        item.hydrated.chunk.chunk_id for item in unqualified.candidates
    }
    assert selected_ids <= {
        item.hydrated.chunk.chunk_id for item in qualified.candidates
    }
    assert len(qualified.candidates) <= policy.fusion_candidate_limit

    other_match = _table_chain(
        700,
        document_number=7,
        display_name="蓝熊研发规范.docx",
    )
    ambiguous_chains = (other_match, *noise_chains[1:], selected)
    ambiguous_seeds = tuple(chain[0] for chain in ambiguous_chains)
    ambiguous_stored = tuple(
        item.hydrated for chain in ambiguous_chains for item in chain
    )
    ambiguous_source = cast(
        EvidenceSourcePort, _NeighborSource(ambiguous_stored)
    )
    ambiguous_expander = NeighborExpander(ambiguous_source)
    baseline = ambiguous_expander.expand(
        _snapshot(), ambiguous_seeds, "table", policy
    )
    unresolved = ambiguous_expander.expand(
        _snapshot(),
        ambiguous_seeds,
        "table",
        policy,
        source_qualifier="蓝熊规范",
    )
    assert unresolved.candidates == baseline.candidates
