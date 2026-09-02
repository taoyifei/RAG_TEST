from __future__ import annotations

from rag_app.adapters.legacy import V4DocumentIrToLegacyElementsAdapter
from rag_app.contracts import ElementKind
from tests.adapters.parsers.docx.fixtures import build_package, parse_package


def test_v4_table_is_flattened_once_with_explicit_loss_report() -> None:
    table = """
<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>单元格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
"""
    result = parse_package(build_package(table))

    elements, report = V4DocumentIrToLegacyElementsAdapter().convert(
        result.document_ir
    )

    assert [(element.kind, element.text) for element in elements] == [
        (ElementKind.TABLE, "单元格")
    ]
    assert report.converted_count == 1
    assert {issue.code for issue in report.issues} == {
        "IR_NODE_NOT_EXPRESSIBLE_IN_LEGACY",
        "V4_COMPLEX_TABLE_FLATTENED",
    }
