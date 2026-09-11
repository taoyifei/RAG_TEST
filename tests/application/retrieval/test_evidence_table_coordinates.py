"""用独立合成表格验证行列交叉定位和来源边界。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import (
    ChunkRole,
    EvidenceSelectionContext,
    KnowledgeBaseScope,
    QueryKind,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
    SourceSpan,
    SourceSpanKind,
)
from rag_app.core.models.common import freeze_json_object
from tests.application.retrieval.helpers import make_ranked_chunk

_ROWS = (
    ("机型", "流量", "上限温度"),
    ("蔚蓝泵", "27 L/min", "63 ℃"),
    ("白桦泵", "41 L/min", "82 ℃"),
)
_SCOPE = KnowledgeBaseScope(
    project_id=f"prj_{'1' * 32}",
    knowledge_base_id=f"kb_{'2' * 32}",
)


def _context(text: str) -> EvidenceSelectionContext:
    return EvidenceSelectionContext(
        analysis=QueryAnalyzer().analyze(
            SearchRequest(scope=_SCOPE, text=text)
        ),
        query_kind=QueryKind.SIMPLE_FACT,
        rerank_mode="lexical_overlap",
        selected_slot=None,
    )


def _table(  # noqa: PLR0913
    number: int = 1,
    *,
    rows: tuple[tuple[str, ...], ...] = _ROWS,
    first_row: int = 0,
    document_number: int = 2,
    table_number: int = 1,
    repeated: bool = False,
) -> RankedChunk:
    text = " | ".join(value for row in rows for value in row)
    candidate = make_ranked_chunk(
        number, text, role=ChunkRole.TABLE, document_number=document_number
    )
    prototype = candidate.hydrated.chunk.source_spans[0]
    spans: list[SourceSpan] = []
    cursor = 0
    for row_index, row in enumerate(rows, first_row):
        for column_index, value in enumerate(row):
            if cursor:
                spans.append(
                    SourceSpan(
                        span_type=SourceSpanKind.SEPARATOR,
                        chunk_start_char=cursor,
                        chunk_end_char=cursor + 3,
                        is_citable=False,
                    )
                )
                cursor += 3
            path = (
                "body",
                f"tbl:{table_number}",
                f"tr:{row_index}",
                f"tc:{column_index}",
                "p:0",
            )
            anchor = prototype.source_anchor.model_copy(
                update={
                    "structural_path": path,
                    "source_start_char": 5,
                    "source_end_char": 5 + len(value),
                }
            )
            node_number = table_number * 1000 + row_index * 10 + column_index
            spans.append(
                SourceSpan(
                    node_id=f"node_{node_number:032x}",
                    source_anchor=anchor,
                    structural_path=path,
                    span_type=(
                        SourceSpanKind.REPEATED_CONTEXT
                        if repeated
                        else SourceSpanKind.ORIGINAL_TEXT
                    ),
                    is_repeated=repeated,
                    chunk_start_char=cursor,
                    chunk_end_char=cursor + len(value),
                    source_start_char=5,
                    source_end_char=5 + len(value),
                )
            )
            cursor += len(value)
    chunk = candidate.hydrated.chunk.model_copy(
        update={"source_spans": tuple(spans)}
    )
    return candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )


@pytest.mark.parametrize(
    ("query", "expected", "row", "column"),
    [
        ("蔚蓝泵的流量是多少", "27 L/min", 1, 1),
        ("白桦泵的流量是多少", "41 L/min", 2, 1),
        ("蔚蓝泵的上限温度是多少", "63 ℃", 1, 2),
        ("白桦泵的上限温度是多少", "82 ℃", 2, 2),
    ],
)
def test_selects_intersection_instead_of_row_or_column_label(
    query: str, expected: str, row: int, column: int
) -> None:
    evidence = EvidenceAssembler().assemble(
        (_table(),), RetrievalPolicy(), context=_context(query)
    )

    assert [item.citation_text for item in evidence] == [expected]
    span = evidence[0].source_spans[0]
    assert span.node_id == f"node_{1000 + row * 10 + column:032x}"
    assert (span.source_start_char, span.source_end_char) == (
        5,
        5 + len(expected),
    )
    assert (span.chunk_start_char, span.chunk_end_char) == (0, len(expected))


def test_links_header_and_data_chunks_within_the_same_table() -> None:
    evidence = EvidenceAssembler().assemble(
        (
            _table(1, rows=_ROWS[:1], repeated=True),
            _table(2, rows=_ROWS[1:], first_row=1),
        ),
        RetrievalPolicy(),
        context=_context("白桦泵的上限温度是多少"),
    )

    assert [item.citation_text for item in evidence] == ["82 ℃"]


@pytest.mark.parametrize(
    "query",
    [
        "蔚蓝泵是多少",
        "上限温度是多少",
        "紫杉泵的流量是多少",
        "白桦泵和蔚蓝泵的流量是多少",
        "白桦泵的流量和上限温度是多少",
    ],
)
def test_incomplete_or_ambiguous_coordinates_do_not_choose_a_value(
    query: str,
) -> None:
    evidence = EvidenceAssembler().assemble(
        (_table(),), RetrievalPolicy(), context=_context(query)
    )

    assert not {item.citation_text for item in evidence} & {
        "27 L/min",
        "41 L/min",
        "63 ℃",
        "82 ℃",
    }


@pytest.mark.parametrize(
    "boundary", ["document", "version", "table", "group", "section"]
)
def test_does_not_join_coordinates_across_identity_boundaries(
    boundary: str,
) -> None:
    header = _table(1, rows=_ROWS[:1])
    data = _table(
        2,
        rows=_ROWS[1:],
        first_row=1,
        document_number=3 if boundary == "document" else 2,
        table_number=2 if boundary == "table" else 1,
    )
    chunk = data.hydrated.chunk
    if boundary == "version":
        chunk = chunk.model_copy(
            update={
                "version": chunk.version.model_copy(
                    update={"document_version_id": f"dver_{'9' * 32}"}
                )
            }
        )
    elif boundary == "group":
        chunk = chunk.model_copy(update={"neighbor_group_id": "group-other"})
    elif boundary == "section":
        chunk = chunk.model_copy(update={"section_id": "section-other"})
    data = data.model_copy(
        update={"hydrated": data.hydrated.model_copy(update={"chunk": chunk})}
    )
    evidence = EvidenceAssembler().assemble(
        (header, data),
        RetrievalPolicy(),
        context=_context("白桦泵的上限温度是多少"),
    )

    assert "82 ℃" not in {item.citation_text for item in evidence}


@pytest.mark.parametrize(
    "coordinates",
    [
        ["r1:c0:rs1:cs2", "r1:c2:rs1:cs1"],
        ["r1:c1:rs1:cs1", "r1:c2:rs1:cs1"],
        ["r1:c0:rs2:cs1", "r1:c1:rs1:cs1"],
    ],
)
def test_merged_or_omitted_grid_does_not_invent_an_intersection(
    coordinates: list[str],
) -> None:
    candidate = _table()
    chunk = candidate.hydrated.chunk.model_copy(
        update={
            "metadata": freeze_json_object(
                {
                    "atoms": [
                        {
                            "metadata": {
                                "cell_coordinates": coordinates,
                            }
                        }
                    ]
                }
            ),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )

    evidence = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(),
        context=_context("白桦泵的上限温度是多少"),
    )

    assert "82 ℃" not in {item.citation_text for item in evidence}


def test_duplicate_row_labels_do_not_guess_between_different_values() -> None:
    candidate = _table(
        rows=(
            _ROWS[0],
            _ROWS[1],
            ("蔚蓝泵", "55 L/min", "95 ℃"),
        )
    )

    evidence = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(),
        context=_context("蔚蓝泵的上限温度是多少"),
    )

    assert not {"63 ℃", "95 ℃"} & {item.citation_text for item in evidence}


def test_verified_merged_row_uses_original_logical_coordinate() -> None:
    candidate = _table(
        rows=(
            ("型号", "单位", "日期"),
            ("ZX-17", "27 mm", "2026-01-01"),
            ("ZX-17", "28 mm", "2026-02-01"),
        )
    )
    chunk = candidate.hydrated.chunk
    spans = list(chunk.source_spans)
    label_indexes = [
        index
        for index, span in enumerate(spans)
        if chunk.citation_text[span.chunk_start_char : span.chunk_end_char]
        == "ZX-17"
    ]
    original = spans[label_indexes[0]]
    spans[label_indexes[1]] = spans[label_indexes[1]].model_copy(
        update={
            "node_id": original.node_id,
            "source_anchor": original.source_anchor,
            "structural_path": original.structural_path,
            "span_type": SourceSpanKind.REPEATED_CONTEXT,
            "is_repeated": True,
            "source_start_char": original.source_start_char,
            "source_end_char": original.source_end_char,
        }
    )

    def _node(row: int, column: int) -> str:
        if row == 2 and column == 0:
            row = 1
        return f"node_{1000 + row * 10 + column:032x}"

    atoms = []
    for row in range(3):
        coordinates = [
            f"r{row}:c{column}:rs{2 if row == 1 and column == 0 else 1}:cs1"
            for column in range(3)
        ]
        atoms.append(
            {
                "metadata": {
                    "row_index": row,
                    "cell_coordinates": coordinates,
                    "cell_source_node_ids": {
                        str(column): [_node(row, column)] for column in range(3)
                    },
                }
            }
        )
    chunk = chunk.model_copy(
        update={
            "source_spans": tuple(spans),
            "metadata": freeze_json_object({"atoms": atoms}),
        }
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(update={"chunk": chunk})
        }
    )

    evidence = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(),
        context=_context("ZX-17 的单位数值是多少"),
    )

    assert [item.citation_text for item in evidence] == ["27 mm"]


def test_table_answer_respects_token_budget_and_existing_caps() -> None:
    first = _table()
    second = _table(2, document_number=3)
    context = _context("白桦泵的流量是多少")

    assert not EvidenceAssembler().assemble(
        (first,),
        RetrievalPolicy(evidence_token_budget=1),
        context=context,
    )
    evidence = EvidenceAssembler().assemble(
        (first, first, second),
        RetrievalPolicy(max_evidence_items=1, max_evidence_items_per_chunk=1),
        context=context,
    )

    assert [item.citation_text for item in evidence] == ["41 L/min"]
    assert evidence[0].chunk_id == first.hydrated.chunk.chunk_id


def test_explicit_numeric_query_uses_coordinates_over_unit_preferences() -> (
    None
):
    context = _context("白桦泵的流量是多少").model_copy(
        update={"query_kind": QueryKind.TABLE_NUMERIC}
    )

    evidence = EvidenceAssembler().assemble(
        (_table(),),
        RetrievalPolicy(),
        context=context,
    )

    assert [item.citation_text for item in evidence] == ["41 L/min"]


def test_identifier_table_measurement_without_question_mark_uses_column() -> (
    None
):
    candidate = _table(
        rows=(
            ("型号", "单位", "日期"),
            ("ZX-17", "27 mm", "2026-01-01"),
        )
    )

    evidence = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(),
        context=_context("ZX-17 表格单位数值"),
    )

    assert [item.citation_text for item in evidence] == ["27 mm"]


def test_responsible_party_uses_semantic_header_mapping() -> None:
    candidate = _table(
        rows=(
            ("交付件名称", "主责岗位", "交付说明"),
            ("星环登记表", "资料协调员", "记录设备进场状态"),
        )
    )

    evidence = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(),
        context=_context("星环登记表谁负责"),
    )

    assert [item.citation_text for item in evidence] == ["资料协调员"]
    support = dict(evidence[0].metadata)["answer_support"]
    assert support["answer_type"] == "RESPONSIBLE_PARTY"
    assert support["support_reason"] == "SOURCE_RELATION_AND_VALUE"


def test_definition_selects_cell_without_rule_record_answer() -> None:
    candidate = _table(
        rows=(
            ("交付件名称", "责任角色", "交付件说明"),
            ("星环登记表", "资料协调员", "记录设备进场状态"),
        )
    )

    evidence = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(
            max_evidence_items=8,
            max_evidence_items_per_chunk=8,
            per_document_cap=8,
            per_section_cap=8,
        ),
        context=_context("星环登记表是什么"),
    )

    assert [item.citation_text for item in evidence] == ["记录设备进场状态"]
    assert all(
        dict(item.metadata)["answer_support"]["support_reason"]
        == "SOURCE_RELATION_AND_VALUE"
        for item in evidence
    )


def test_definition_preserves_source_mapping_after_cell_whitespace_trim() -> (
    None
):
    candidate = _table(
        rows=(
            ("术语", " 定义 "),
            (" OPC ", " 对象过程控制。 "),
        )
    )

    evidence = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(
            max_evidence_items=8,
            max_evidence_items_per_chunk=8,
            per_document_cap=8,
            per_section_cap=8,
        ),
        context=_context("OPC 是啥？"),
    )

    assert [item.citation_text.strip() for item in evidence] == [
        "对象过程控制。"
    ]
    assert all(
        dict(item.metadata)["answer_support"]["support_reason"]
        != "TABLE_ROW_RECORD"
        for item in evidence
    )
    for item in evidence:
        span = item.source_spans[0]
        assert span.chunk_start_char == 0
        assert span.chunk_end_char == len(item.citation_text)
        assert span.source_end_char - span.source_start_char == len(
            item.citation_text
        )
