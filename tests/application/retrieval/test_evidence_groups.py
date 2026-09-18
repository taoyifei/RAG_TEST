"""通用 EvidenceGroup 的结构闭合、来源映射和原子预算。"""

from __future__ import annotations

from rag_app.application.retrieval.evidence_groups import (
    GroupCandidate,
    build_catalog_evidence_group,
    build_evidence_groups,
    pack_evidence_groups,
    pack_evidence_groups_with_diagnostics,
    rank_evidence_groups,
)
from rag_app.core.models import (
    ChunkRole,
    EvidenceGroupKind,
    RankedChunk,
    SourceSpan,
    SourceSpanKind,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.ports.evidence_source import CatalogDocument
from tests.application.retrieval.helpers import make_ranked_chunk


def _groups(*candidates: RankedChunk) -> tuple[GroupCandidate, ...]:
    return build_evidence_groups(
        candidates,
        max_groups=12,
        max_member_chunks=8,
        rerank_text_char_limit=2400,
    )


def _with_chunk(candidate: RankedChunk, **updates: object) -> RankedChunk:
    chunk = candidate.hydrated.chunk.model_copy(update=updates)
    return candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )


def _table_row(
    number: int, row: int, cells: tuple[str, str], *, header: bool
) -> RankedChunk:
    text = " | ".join(cells)
    candidate = make_ranked_chunk(
        number, text, role=ChunkRole.TABLE, neighbor_group_id="table-1"
    )
    anchor = candidate.hydrated.chunk.source_spans[0].source_anchor
    assert anchor is not None
    spans: list[SourceSpan] = []
    cursor = 0
    for column, value in enumerate(cells):
        if column:
            spans.append(
                SourceSpan(
                    span_type=SourceSpanKind.SEPARATOR,
                    chunk_start_char=cursor,
                    chunk_end_char=cursor + 3,
                    is_citable=False,
                )
            )
            cursor += 3
        path = ("body", "tbl:1", f"tr:{row}", f"tc:{column}", "p:0")
        source = anchor.model_copy(
            update={
                "structural_path": path,
                "row_index": row,
                "cell_index": column,
                "source_start_char": 0,
                "source_end_char": len(value),
            }
        )
        spans.append(
            SourceSpan(
                node_id=f"node_{number * 10 + column:032x}",
                source_anchor=source,
                structural_path=path,
                chunk_start_char=cursor,
                chunk_end_char=cursor + len(value),
                source_start_char=0,
                source_end_char=len(value),
            )
        )
        cursor += len(value)
    return _with_chunk(
        candidate,
        heading_path=("事项表",),
        source_spans=tuple(spans),
        metadata=freeze_json_object(
            {
                "document_title": "事项清单",
                "department_name": "综合处",
                "category_path": ["办事", "规范"],
                "atoms": [
                    {
                        "metadata": {
                            "row_index": row,
                            "header_strategy": (
                                "tblHeader" if header else "none"
                            ),
                        }
                    }
                ],
            }
        ),
    )


def test_table_row_group_keeps_header_row_label_cells_and_coordinates() -> None:
    header = _table_row(1, 0, ("事项", "时限"), header=True)
    row = _table_row(2, 1, ("申请", "五日"), header=False)
    groups = _groups(row, header)
    assert len(groups) == 1
    group = groups[0]
    assert group.group.kind is EvidenceGroupKind.TABLE_ROW_GROUP
    assert group.complete
    assert group.group.member_chunk_ids == (
        header.hydrated.chunk.chunk_id,
        row.hydrated.chunk.chunk_id,
    )
    assert "r0:c1" in group.group.structural_coordinates
    assert "r1:c0" in group.group.structural_coordinates
    assert "五日" in group.rerank_text
    assert "综合处" in group.rerank_text
    assert group.group.member_source_maps[1].citation_text == "申请 | 五日"
    assert len(group.group.member_source_maps[1].source_spans) == 3


def test_split_table_cell_uses_source_offset_before_retrieval_rank() -> None:
    """同一单元格的后半块排名更高时，完整行仍按原文顺序闭合。"""
    header = _table_row(91, 0, ("任务类型", "输入"), header=True)
    first = make_ranked_chunk(
        92,
        "需求快验",
        role=ChunkRole.TABLE,
        neighbor_group_id="table-1",
        next_chunk_id=f"chunk_{93:032x}",
    )
    second = make_ranked_chunk(
        93,
        "输入及启动",
        role=ChunkRole.TABLE,
        neighbor_group_id="table-1",
        previous_chunk_id=f"chunk_{92:032x}",
    )
    path = ("body", "tbl:1", "tr:1", "tc:0", "p:0")
    for index, candidate in enumerate((first, second)):
        span = candidate.hydrated.chunk.source_spans[0]
        offset = index * len(first.hydrated.chunk.citation_text)
        anchor = span.source_anchor
        assert anchor is not None
        adjusted = span.model_copy(
            update={
                "source_anchor": anchor.model_copy(
                    update={"ordinal": 29, "structural_path": path}
                ),
                "structural_path": path,
                "source_start_char": offset,
                "source_end_char": offset
                + len(candidate.hydrated.chunk.citation_text),
            }
        )
        updated = _with_chunk(
            candidate,
            heading_path=("事项表",),
            source_spans=(adjusted,),
        )
        if index == 0:
            first = updated.model_copy(update={"fusion_rank": 9})
        else:
            second = updated.model_copy(update={"fusion_rank": 1})

    group = _groups(second, first, header)[0]

    assert group.complete
    assert group.group.member_chunk_ids == (
        header.hydrated.chunk.chunk_id,
        first.hydrated.chunk.chunk_id,
        second.hydrated.chunk.chunk_id,
    )


def test_group_budget_excludes_repeated_chunk_prefix() -> None:
    """检索用 Chunk 前缀不应占用实际未渲染的组预算。"""
    header = _with_chunk(
        _table_row(71, 0, ("事项", "条件"), header=True),
        token_count=5000,
    )
    row = _with_chunk(
        _table_row(72, 1, ("申请", "会议通过"), header=False),
        token_count=5000,
    )
    group = _groups(header, row)[0]
    assert group.complete
    assert group.token_cost < 1000
    assert pack_evidence_groups(
        (group,), token_budget=1000, max_groups=1, max_chunks=2
    ) == (group,)


def test_unmarked_short_first_row_can_supply_column_headers() -> None:
    header = _table_row(3, 0, ("事项", "时限"), header=False)
    row = _table_row(4, 1, ("申请", "五日"), header=False)

    group = _groups(row, header)[0]

    assert group.complete
    assert group.group.member_chunk_ids == (
        header.hydrated.chunk.chunk_id,
        row.hydrated.chunk.chunk_id,
    )


def test_unmarked_data_row_does_not_masquerade_as_header() -> None:
    first = _table_row(
        5,
        0,
        ("申请事项需要先提交完整的证明文件和申请表", "五日"),
        header=False,
    )
    row = _table_row(6, 1, ("复核", "三日"), header=False)

    group = _groups(row, first)[0]

    assert not group.complete
    assert "MISSING_TABLE_HEADER" in group.group.incomplete_reasons


def test_contiguous_list_requires_intro_and_both_end_boundaries() -> None:
    intro = make_ranked_chunk(10, "办理条件如下：", neighbor_group_id="intro")
    first = make_ranked_chunk(
        11,
        "第一项",
        role=ChunkRole.LIST,
        neighbor_group_id="items",
        next_chunk_id=f"chunk_{12:032x}",
    )
    last = make_ranked_chunk(
        12,
        "第二项",
        role=ChunkRole.LIST,
        neighbor_group_id="items",
        previous_chunk_id=f"chunk_{11:032x}",
    )
    groups = _groups(last, intro, first)
    listed = next(
        item
        for item in groups
        if item.group.kind is EvidenceGroupKind.LIST_GROUP
    )
    assert listed.complete
    assert listed.group.member_chunk_ids == tuple(
        item.hydrated.chunk.chunk_id for item in (intro, first, last)
    )
    missing = _groups(intro, first)
    incomplete = next(
        item
        for item in missing
        if item.group.kind is EvidenceGroupKind.LIST_GROUP
    )
    assert not incomplete.complete
    assert "MISSING_NEXT_NEIGHBOR" in incomplete.group.incomplete_reasons
    assert (
        pack_evidence_groups(
            (incomplete,), token_budget=100, max_groups=2, max_chunks=8
        )
        == ()
    )


def test_procedure_preserves_order_and_is_not_split_by_budget() -> None:
    intro = make_ranked_chunk(20, "办理流程如下：", neighbor_group_id="intro")
    first = _with_chunk(
        make_ranked_chunk(
            21,
            "申请人提交材料，三日内受理。",
            role=ChunkRole.LIST,
            neighbor_group_id="steps",
            next_chunk_id=f"chunk_{22:032x}",
        ),
        heading_path=("办理流程",),
    )
    second = _with_chunk(
        make_ranked_chunk(
            22,
            "经办人核验；材料不齐时通知补正。",
            role=ChunkRole.LIST,
            neighbor_group_id="steps",
            previous_chunk_id=f"chunk_{21:032x}",
        ),
        heading_path=("办理流程",),
    )
    procedure = next(
        item
        for item in _groups(second, first, intro)
        if item.group.kind is EvidenceGroupKind.PROCEDURE_GROUP
    )
    assert procedure.complete
    assert procedure.group.member_chunk_ids == tuple(
        item.hydrated.chunk.chunk_id for item in (intro, first, second)
    )
    packed = pack_evidence_groups(
        (procedure,),
        token_budget=procedure.token_cost,
        max_groups=1,
        max_chunks=3,
    )
    assert packed == (procedure,)
    too_small = pack_evidence_groups_with_diagnostics(
        (procedure,),
        token_budget=procedure.token_cost - 1,
        max_groups=1,
        max_chunks=3,
    )
    assert too_small.selected == ()
    assert too_small.rejected == ((procedure.group_id, "GROUP_EXCEEDS_BUDGET"),)
    assert not too_small.incomplete[0].complete
    assert too_small.incomplete[0].group.incomplete_reasons == (
        "GROUP_EXCEEDS_BUDGET",
    )


def test_group_packing_reports_duplicate_document_and_section_caps() -> None:
    """同一来源不得重复装包，文档和章节组数上限分别可诊断。"""
    first, second = _groups(
        make_ranked_chunk(51, "第一段。"),
        make_ranked_chunk(52, "第二段。"),
    )
    budget = first.token_cost + second.token_cost
    common = {
        "token_budget": budget,
        "max_groups": 2,
        "max_chunks": 2,
    }

    duplicate = pack_evidence_groups_with_diagnostics((first, first), **common)
    document_cap = pack_evidence_groups_with_diagnostics(
        (first, second), per_document_cap=1, **common
    )
    section_cap = pack_evidence_groups_with_diagnostics(
        (first, second), per_document_cap=2, per_section_cap=1, **common
    )

    assert duplicate.rejected == ((first.group_id, "GROUP_DUPLICATE"),)
    assert document_cap.rejected == ((second.group_id, "GROUP_DOCUMENT_CAP"),)
    assert section_cap.rejected == ((second.group_id, "GROUP_SECTION_CAP"),)


def test_table_rows_may_share_one_header_without_duplicate_rejection() -> None:
    """多行表格共同引用同一表头时，仍可按行原子装包。"""
    header = _table_row(61, 0, ("事项", "时限"), header=True)
    first = _table_row(62, 1, ("申请", "五日"), header=False)
    second = _table_row(63, 2, ("复核", "三日"), header=False)
    groups = _groups(header, first, second)
    assert len(groups) == 2

    packed = pack_evidence_groups_with_diagnostics(
        groups,
        token_budget=sum(group.token_cost for group in groups),
        max_groups=2,
        max_chunks=3,
    )

    assert len(packed.selected) == 2
    assert not packed.rejected


def test_catalog_group_only_metadata_and_standalone_paragraph() -> None:
    paragraph = make_ranked_chunk(
        30,
        "原文事实。",
        previous_chunk_id=f"chunk_{29:032x}",
        next_chunk_id=f"chunk_{31:032x}",
    )
    paragraph_group = _groups(paragraph)[0]
    assert paragraph_group.group.kind is EvidenceGroupKind.PARAGRAPH_GROUP
    assert paragraph_group.complete
    catalog = build_catalog_evidence_group(
        CatalogDocument(
            document_id=paragraph.hydrated.chunk.version.document_id,
            document_version_id=(
                paragraph.hydrated.chunk.version.document_version_id
            ),
            chunk_id=paragraph.hydrated.chunk.chunk_id,
            title="流程模板",
            metadata=freeze_json_object({"category_path": ["表单", "模板"]}),
        ),
        index_revision_id=paragraph.hydrated.chunk.index_revision_id,
    )
    assert catalog.group.kind is EvidenceGroupKind.CATALOG_ENTRY
    assert catalog.complete
    assert catalog.members == ()
    assert catalog.group.member_source_maps == ()
    assert catalog.group.category_path == ("表单", "模板")
    assert "正文" not in catalog.rerank_text


def test_group_order_reuses_member_rerank_rank_without_provider() -> None:
    unrelated = make_ranked_chunk(
        40, "通用说明。", neighbor_group_id="unrelated"
    ).model_copy(update={"rerank_rank": 2, "rerank_score": 0.1})
    relevant = make_ranked_chunk(
        41, "办理时限为三日。", neighbor_group_id="relevant"
    ).model_copy(update={"rerank_rank": 1, "rerank_score": 0.9})

    ranked = rank_evidence_groups(_groups(unrelated, relevant))

    assert ranked[0].group.member_chunk_ids == (
        relevant.hydrated.chunk.chunk_id,
    )
    assert ranked[0].members[0].hydrated.chunk == relevant.hydrated.chunk


def test_partial_section_falls_back_to_independent_paragraphs() -> None:
    first = make_ranked_chunk(
        50,
        "本节第一段。",
        neighbor_group_id="long-section",
        previous_chunk_id=f"chunk_{49:032x}",
        next_chunk_id=f"chunk_{51:032x}",
    )
    second = make_ranked_chunk(
        51,
        "本节第二段。",
        neighbor_group_id="long-section",
        previous_chunk_id=f"chunk_{50:032x}",
        next_chunk_id=f"chunk_{52:032x}",
    )

    groups = _groups(second, first)

    assert len(groups) == 2
    assert all(group.complete for group in groups)
    assert all(
        group.group.kind is EvidenceGroupKind.PARAGRAPH_GROUP
        for group in groups
    )
    assert tuple(group.group.member_chunk_ids for group in groups) == (
        (first.hydrated.chunk.chunk_id,),
        (second.hydrated.chunk.chunk_id,),
    )


def test_complete_group_orders_before_partial_structure() -> None:
    incomplete = _groups(
        make_ranked_chunk(
            60,
            "列表首项。",
            role=ChunkRole.LIST,
            neighbor_group_id="open-list",
            next_chunk_id=f"chunk_{61:032x}",
        ).model_copy(update={"rerank_rank": 1})
    )[0]
    paragraph = _groups(make_ranked_chunk(62, "办理时限为三日。"))[0]

    ranked = rank_evidence_groups((incomplete, paragraph))

    assert not incomplete.complete
    assert ranked == (paragraph, incomplete)


def test_group_identity_changes_with_index_revision() -> None:
    candidate = make_ranked_chunk(70, "同一来源。")
    alternate_chunk = candidate.hydrated.chunk.model_copy(
        update={"index_revision_id": f"irev_{'f' * 32}"}
    )
    alternate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={"chunk": alternate_chunk}
            )
        }
    )

    assert _groups(candidate)[0].group_id != _groups(alternate)[0].group_id
