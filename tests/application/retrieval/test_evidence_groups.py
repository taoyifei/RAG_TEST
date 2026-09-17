"""通用 EvidenceGroup 的结构闭合、来源映射和原子预算。"""

from __future__ import annotations

from rag_app.adapters.providers.deterministic import (
    LexicalOverlapRerankerAdapter,
)
from rag_app.application.retrieval.evidence_groups import (
    GroupCandidate,
    build_catalog_evidence_group,
    build_evidence_groups,
    pack_evidence_groups,
    pack_evidence_groups_with_diagnostics,
)
from rag_app.application.retrieval.reranking import CircuitAwareReranker
from rag_app.core.models import (
    ChunkRole,
    EvidenceGroupKind,
    RankedChunk,
    RetrievalPolicy,
    SourceSpan,
    SourceSpanKind,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.core.policies import EgressPolicy
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
    assert pack_evidence_groups(
        (incomplete,), token_budget=100, max_groups=2, max_chunks=8
    ) == ()


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
        )
    )
    assert catalog.group.kind is EvidenceGroupKind.CATALOG_ENTRY
    assert catalog.complete
    assert catalog.members == ()
    assert catalog.group.member_source_maps == ()
    assert catalog.group.category_path == ("表单", "模板")
    assert "正文" not in catalog.rerank_text


def test_group_reranker_orders_complete_groups_and_preserves_chunks() -> None:
    unrelated = make_ranked_chunk(
        40, "通用说明。", neighbor_group_id="unrelated"
    )
    relevant = make_ranked_chunk(
        41, "办理时限为三日。", neighbor_group_id="relevant"
    )
    groups = _groups(unrelated, relevant)
    result = CircuitAwareReranker(
        LexicalOverlapRerankerAdapter()
    ).rerank_groups(
        "办理时限",
        groups,
        EgressPolicy(),
        RetrievalPolicy(),
        enabled=True,
        result_limit=1,
    )

    assert result.reason_code == "RERANK_EXECUTED"
    assert len(result.groups) == 1
    assert result.groups[0].group.member_chunk_ids == (
        relevant.hydrated.chunk.chunk_id,
    )
    assert result.groups[0].members[0].hydrated.chunk == relevant.hydrated.chunk
