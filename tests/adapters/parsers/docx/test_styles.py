from __future__ import annotations

# ruff: noqa: E501
from rag_app.core.models import NodeKind
from tests.adapters.parsers.docx.fixtures import build_package, parse_package


def test_inherited_outline_and_custom_style_create_headings() -> None:
    styles = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Base"><w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Child"><w:name w:val="继承标题"/><w:basedOn w:val="Base"/><w:next w:val="PolicyHeading"/><w:link w:val="CharStyle"/><w:qFormat/><w:unhideWhenUsed/></w:style>
  <w:style w:type="paragraph" w:styleId="PolicyHeading"><w:name w:val="业务标题"/></w:style>
</w:styles>
"""
    blocks = (
        '<w:p><w:pPr><w:pStyle w:val="Child"/></w:pPr>'
        '<w:r><w:t>三级标题</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="PolicyHeading"/></w:pPr>'
        '<w:r><w:t>策略标题</w:t></w:r></w:p>'
    )

    result = parse_package(
        build_package(blocks, styles=styles),
        custom_heading_styles=(("业务标题", 4),),
    )
    headings = [
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.HEADING
    ]

    assert [node.text for node in headings] == ["三级标题", "策略标题"]
    assert [dict(node.metadata)["heading_level"] for node in headings] == [3, 4]
    first_metadata = dict(headings[0].metadata)
    assert first_metadata["next_style_id"] == "PolicyHeading"
    assert first_metadata["linked_style_id"] == "CharStyle"
    assert first_metadata["style_quick_format"] is True
    assert first_metadata["style_unhide_when_used"] is True


def test_style_cycle_is_reported_without_infinite_recursion() -> None:
    styles = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="A"><w:basedOn w:val="B"/></w:style>
  <w:style w:type="paragraph" w:styleId="B"><w:basedOn w:val="A"/></w:style>
</w:styles>
"""
    result = parse_package(
        build_package(
            '<w:p><w:pPr><w:pStyle w:val="A"/></w:pPr><w:r><w:t>正文</w:t></w:r></w:p>',
            styles=styles,
        )
    )

    assert "DOCX_STYLE_INHERITANCE_CYCLE" in result.report.warnings
