from __future__ import annotations

# ruff: noqa: E501
import pytest

from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import NodeKind, ParseSource
from tests.adapters.parsers.docx.fixtures import build_package, context, policy


def test_grid_span_and_vertical_merge_preserve_logical_cells() -> None:
    table = """
<w:tbl>
  <w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
  <w:tr>
    <w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>合并</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>C1</w:t></w:r></w:p></w:tc>
  </w:tr>
  <w:tr>
    <w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr><w:p/></w:tc>
    <w:tc><w:p><w:r><w:t>C2</w:t></w:r></w:p></w:tc>
  </w:tr>
</w:tbl>
"""
    result = DocxOoxmlV4Parser().parse(
        ParseSource(
            media_type="application/octet-stream",
            display_name="table.docx",
            content=build_package(table),
        ),
        policy(),
        context(),
    )

    tables = [node for node in result.document_ir.nodes if node.kind is NodeKind.TABLE]
    rows = [node for node in result.document_ir.nodes if node.kind is NodeKind.TABLE_ROW]
    cells = [node for node in result.document_ir.nodes if node.kind is NodeKind.TABLE_CELL]

    assert len(tables) == 1
    assert len(rows) == 2
    assert [(cell.cell_grid.row_index, cell.cell_grid.column_index) for cell in cells] == [
        (0, 0),
        (0, 2),
        (1, 0),
        (1, 2),
    ]
    assert cells[0].cell_grid.column_span == 2
    assert dict(cells[2].metadata)["vmerge_anchor_node_id"] == cells[0].node_id
    assert all(node.kind is not NodeKind.TABLE_REPRESENTATION for node in cells)


def test_omitted_grid_and_repeated_header_remain_explicit_metadata() -> None:
    table = """
<w:tbl><w:tr><w:trPr><w:gridBefore w:val="1"/><w:gridAfter w:val="2"/><w:tblHeader/></w:trPr><w:tc><w:p><w:r><w:t>唯一物理单元格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
"""
    result = DocxOoxmlV4Parser().parse(
        ParseSource(
            media_type="application/octet-stream",
            display_name="omitted.docx",
            content=build_package(table),
        ),
        policy(),
        context(),
    )
    row = next(
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.TABLE_ROW
    )
    cell = next(
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.TABLE_CELL
    )

    assert dict(row.metadata) == {
        "grid_after": 2,
        "grid_before": 1,
        "repeated_header": True,
    }
    assert cell.cell_grid is not None
    assert cell.cell_grid.column_index == 1
    assert "DOCX_TABLE_GRID_MISSING" in result.report.warnings


def test_nested_table_preserves_physical_child_order() -> None:
    table = """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc>
  <w:p><w:r><w:t>之前</w:t></w:r></w:p>
  <w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>内表</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  <w:p><w:r><w:t>之后</w:t></w:r></w:p>
</w:tc></w:tr></w:tbl>
"""
    result = DocxOoxmlV4Parser().parse(
        ParseSource(
            media_type="application/octet-stream",
            display_name="nested.docx",
            content=build_package(table),
        ),
        policy(),
        context(),
    )
    cell = next(
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.TABLE_CELL
        and node.cell_grid is not None
        and node.cell_grid.row_index == 0
        and node.parent_node_id is not None
    )
    by_id = {node.node_id: node for node in result.document_ir.nodes}

    assert [by_id[item].kind for item in cell.child_ids] == [
        NodeKind.PARAGRAPH,
        NodeKind.TABLE,
        NodeKind.PARAGRAPH,
    ]
    assert [by_id[item].text for item in cell.child_ids] == ["之前", "", "之后"]


def test_inconsistent_grid_is_strict_failure_but_best_effort_issue() -> None:
    table = """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p/></w:tc></w:tr></w:tbl>
"""
    source = ParseSource(
        media_type="application/octet-stream",
        display_name="bad-grid.docx",
        content=build_package(table),
    )

    with pytest.raises(InvalidDocument, match="span"):
        DocxOoxmlV4Parser().parse(source, policy(), context())

    result = DocxOoxmlV4Parser().parse(
        source,
        policy(mode="best_effort"),
        context(),
    )
    assert "DOCX_TABLE_GRID_INCONSISTENT" in result.report.warnings
