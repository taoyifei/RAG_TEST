from __future__ import annotations

# ruff: noqa: E501
from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.core.models import NodeKind, StoryKind
from tests.adapters.parsers.docx.fixtures import (
    build_package,
    context,
    policy,
    source,
)


def test_inline_vml_images_share_blob_and_textbox_has_own_story() -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>
"""
    blocks = """
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="19050"/><wp:docPr id="1" name="图一" descr="替代文本"/><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImage"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>
<w:r><w:pict><v:shape><v:imagedata r:id="rIdImage" title="旧图"/><v:textbox><w:txbxContent><w:p><w:r><w:t>文本框正文</w:t></w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>
"""
    parser = DocxOoxmlV4Parser()

    result = parser.parse(
        source(
            build_package(
                blocks,
                document_relationships=relationships,
                extra_entries={"word/media/image1.png": b"not-a-real-png"},
                content_types=content_types,
            )
        ),
        policy(images="extract"),
        context(),
    )
    images = [
        node for node in result.document_ir.nodes
        if node.kind is NodeKind.IMAGE
    ]

    assert len(images) == 2
    assert images[0].image_attributes is not None
    assert images[1].image_attributes is not None
    assert images[0].image_attributes.blob_ref == images[1].image_attributes.blob_ref
    media = tuple(
        artifact
        for artifact in result.artifacts
        if artifact.role == "embedded_media"
    )
    assert len(media) == 1
    assert media[0].artifact_id == images[0].image_attributes.blob_ref
    assert {artifact.role for artifact in result.artifacts} == {
        "source_document",
        "embedded_media",
    }
    assert any(
        node.anchor.story_kind is StoryKind.TEXT_BOX
        and node.text == "文本框正文"
        for node in result.document_ir.nodes
    )
