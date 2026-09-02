from __future__ import annotations

# ruff: noqa: E501
from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.core.models import NodeKind, ParseSource
from tests.adapters.parsers.docx.fixtures import build_package, context, policy

_NUMBERING = """<?xml version="1.0" encoding="UTF-8"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:suff w:val="space"/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%1.%2)"/><w:suff w:val="tab"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="7"><w:abstractNumId w:val="1"/></w:num>
  <w:num w:numId="8"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/></w:lvlOverride></w:num>
</w:numbering>
"""


def _paragraph(text: str, num_id: int, level: int) -> str:
    return (
        "<w:p><w:pPr><w:numPr>"
        f'<w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/>'
        "</w:numPr></w:pPr>"
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def test_multilevel_and_start_override_labels_are_derived() -> None:
    content = build_package(
        _paragraph("甲", 7, 0)
        + _paragraph("乙", 7, 1)
        + _paragraph("丙", 7, 0)
        + _paragraph("丁", 8, 0),
        numbering=_NUMBERING,
    )
    result = DocxOoxmlV4Parser().parse(
        ParseSource(
            media_type="application/octet-stream",
            display_name="numbering.docx",
            content=content,
        ),
        policy(),
        context(),
    )

    items = [
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.LIST_ITEM
    ]
    assert [item.list_attributes.marker for item in items] == [
        "1. ",
        "1.a)\t",
        "2. ",
        "5. ",
    ]
    assert [dict(item.metadata)["num_id"] for item in items] == [7, 7, 7, 8]


def test_interleaved_num_ids_keep_independent_counters() -> None:
    content = build_package(
        _paragraph("A1", 7, 0)
        + _paragraph("B1", 8, 0)
        + _paragraph("A2", 7, 0)
        + _paragraph("B2", 8, 0),
        numbering=_NUMBERING,
    )

    result = DocxOoxmlV4Parser().parse(
        ParseSource(
            media_type="application/octet-stream",
            display_name="interleaved.docx",
            content=content,
        ),
        policy(),
        context(),
    )
    markers = [
        node.list_attributes.marker
        for node in result.document_ir.nodes
        if node.kind is NodeKind.LIST_ITEM
        and node.list_attributes is not None
    ]

    assert markers == ["1. ", "5. ", "2. ", "6. "]


def test_style_numbering_is_effective_but_manual_prefix_is_plain_text() -> None:
    styles = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="ListStyle"><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr></w:pPr></w:style>
</w:styles>
"""
    blocks = (
        '<w:p><w:pPr><w:pStyle w:val="ListStyle"/></w:pPr>'
        '<w:r><w:t>样式列表</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>1. 手工段落</w:t></w:r></w:p>'
    )

    result = DocxOoxmlV4Parser().parse(
        ParseSource(
            media_type="application/octet-stream",
            display_name="style-list.docx",
            content=build_package(
                blocks,
                styles=styles,
                numbering=_NUMBERING,
            ),
        ),
        policy(),
        context(),
    )

    assert result.document_ir.nodes[0].kind is NodeKind.LIST_ITEM
    assert result.document_ir.nodes[1].kind is NodeKind.PARAGRAPH
