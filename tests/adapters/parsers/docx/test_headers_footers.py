from __future__ import annotations

# ruff: noqa: E501
from rag_app.core.models import NodeKind, StoryKind
from tests.adapters.parsers.docx.fixtures import build_package, parse_package


def test_section_bindings_and_header_footer_stories_are_preserved() -> None:
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdH" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
  <Relationship Id="rIdF" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>
"""
    blocks = """
<w:p><w:r><w:t>正文</w:t></w:r></w:p>
<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/><w:footerReference w:type="default" r:id="rIdF"/></w:sectPr>
"""
    extra = {
        "word/header1.xml": '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页眉</w:t></w:r></w:p></w:hdr>',
        "word/footer1.xml": '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页脚</w:t></w:r></w:p></w:ftr>',
    }

    result = parse_package(
        build_package(
            blocks,
            document_relationships=relationships,
            extra_entries=extra,
        ),
        headers_footers="parse",
    )
    section = next(
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.SECTION
    )
    metadata = dict(section.metadata)

    assert metadata["effective_header_bindings"] == {
        "default": "/word/header1.xml"
    }
    assert metadata["effective_footer_bindings"] == {
        "default": "/word/footer1.xml"
    }
    assert {node.text for node in result.document_ir.nodes if node.text} >= {
        "正文",
        "页眉",
        "页脚",
    }
    assert {node.anchor.story_kind for node in result.document_ir.nodes} >= {
        StoryKind.HEADER,
        StoryKind.FOOTER,
    }


def test_later_section_records_inherited_story_source_without_copying_part() -> None:
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdH" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
</Relationships>
"""
    blocks = (
        '<w:p><w:pPr><w:sectPr><w:headerReference w:type="default" '
        'r:id="rIdH"/></w:sectPr></w:pPr><w:r><w:t>第一节</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>第二节</w:t></w:r></w:p><w:sectPr/>'
    )
    extra = {
        "word/header1.xml": '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>继承页眉</w:t></w:r></w:p></w:hdr>'
    }

    result = parse_package(
        build_package(
            blocks,
            document_relationships=relationships,
            extra_entries=extra,
        ),
        headers_footers="parse",
    )
    sections = [
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.SECTION
    ]
    second_metadata = dict(sections[1].metadata)
    header_roots = [
        node for node in result.document_ir.nodes
        if node.anchor.story_kind is StoryKind.HEADER
        and node.parent_node_id is None
    ]

    assert second_metadata["header_inherited_from"] == {
        "default": sections[0].node_id
    }
    assert len(header_roots) == 1
