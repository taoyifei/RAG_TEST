from __future__ import annotations

# ruff: noqa: E501
from rag_app.core.models import NodeKind, StoryKind
from tests.adapters.parsers.docx.fixtures import build_package, parse_package


def test_notes_comments_and_reference_relationships_are_explicit() -> None:
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdFootnotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
  <Relationship Id="rIdComments" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>
"""
    blocks = """
<w:p><w:commentRangeStart w:id="4"/><w:r><w:t>正文</w:t><w:footnoteReference w:id="2"/></w:r><w:commentRangeEnd w:id="4"/><w:r><w:commentReference w:id="4"/></w:r></w:p>
"""
    extra = {
        "word/footnotes.xml": '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="2"><w:p><w:r><w:t>脚注正文</w:t></w:r></w:p></w:footnote></w:footnotes>',
        "word/comments.xml": '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:id="4" w:author="审核人"><w:p><w:r><w:t>批注意见</w:t></w:r></w:p></w:comment></w:comments>',
    }

    result = parse_package(
        build_package(
            blocks,
            document_relationships=relationships,
            extra_entries=extra,
        ),
        footnotes_endnotes="parse",
        comments="include",
    )

    assert any(
        node.kind is NodeKind.NOTE
        and node.anchor.story_kind is StoryKind.FOOTNOTE
        for node in result.document_ir.nodes
    )
    assert any(
        node.kind is NodeKind.COMMENT
        and dict(node.metadata)["author"] == "审核人"
        for node in result.document_ir.nodes
    )
    assert {item.relationship_type for item in result.document_ir.relationships} == {
        "document-comment",
        "document-footnote",
    }
    body = next(node for node in result.document_ir.nodes if node.text == "正文")
    assert dict(body.metadata)["comment_range_starts"] == ["4"]
    assert dict(body.metadata)["comment_range_ends"] == ["4"]
