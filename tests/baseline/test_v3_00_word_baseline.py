"""V3-00 Word 保真与默认 Product 链路保护基线。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from rag_app.adapters.chunkers import DocxStructuralChunker
from rag_app.adapters.parsers.word_document import WordDocumentV1Parser
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    DocumentRef,
    NodeKind,
    ParseContext,
    ParseSource,
    SourceSpanKind,
    StoryKind,
)
from rag_app.core.policies import (
    CommentsPolicy,
    ImagesPolicy,
    ParsingPolicy,
    StoryPolicy,
)
from scripts.v3_00_word_probe import classify_media_inventory, run_probe
from tests.adapters.parsers.docx.fixtures import build_package

_ROOT = Path(__file__).resolve().parents[2]
_EXPECTED = _ROOT / "tests/fixtures/v3_00_word/expected_baseline.json"
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_OFFICE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _relationships() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Relationships xmlns="{_PACKAGE_REL}">\n'
        f'  <Relationship Id="rIdImage" Type="{_OFFICE_REL}/image" '
        'Target="media/image1.png"/>\n'
        f'  <Relationship Id="rIdComments" Type="{_OFFICE_REL}/comments" '
        'Target="comments.xml"/>\n'
        f'  <Relationship Id="rIdHeader" Type="{_OFFICE_REL}/header" '
        'Target="header1.xml"/>\n'
        f'  <Relationship Id="rIdFooter" Type="{_OFFICE_REL}/footer" '
        'Target="footer1.xml"/>\n'
        f'  <Relationship Id="rIdNumbering" Type="{_OFFICE_REL}/numbering" '
        'Target="numbering.xml"/>\n'
        "</Relationships>\n"
    )


def _numbering() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<w:numbering xmlns:w="{_WORD_NS}">\n'
        '  <w:abstractNum w:abstractNumId="1">\n'
        '    <w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>\n'
        '    <w:lvl w:ilvl="1"><w:start w:val="1"/>'
        '<w:numFmt w:val="lowerLetter"/>'
        '<w:lvlText w:val="%1.%2)"/></w:lvl>\n'
        "  </w:abstractNum>\n"
        '  <w:num w:numId="7"><w:abstractNumId w:val="1"/></w:num>\n'
        "</w:numbering>\n"
    )


def _list_item(text: str, level: int) -> str:
    return (
        "<w:p><w:pPr><w:numPr>"
        f'<w:ilvl w:val="{level}"/><w:numId w:val="7"/>'
        "</w:numPr></w:pPr>"
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def build_v3_00_docx() -> bytes:
    """构造不含私有内容的 Word 结构组合夹具。"""
    table = (
        "\n<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/>"
        "</w:tblGrid>\n"
        "  <w:tr><w:trPr><w:tblHeader/></w:trPr>\n"
        "    <w:tc><w:p><w:r><w:t>列甲</w:t></w:r></w:p></w:tc>\n"
        "    <w:tc><w:p><w:r><w:t>列乙</w:t></w:r></w:p></w:tc>\n"
        "    <w:tc><w:p><w:r><w:t>列丙</w:t></w:r></w:p></w:tc></w:tr>\n"
        "  <w:tr>\n"
        '    <w:tc><w:tcPr><w:gridSpan w:val="2"/>'
        '<w:vMerge w:val="restart"/></w:tcPr>'
        "<w:p><w:r><w:t>合并值</w:t></w:r></w:p></w:tc>\n"
        "    <w:tc><w:p><w:r><w:t>丙一</w:t></w:r></w:p></w:tc></w:tr>\n"
        "  <w:tr>\n"
        '    <w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/>'
        "</w:tcPr><w:p/></w:tc>\n"
        "    <w:tc><w:p><w:r><w:t>丙二</w:t></w:r></w:p></w:tc></w:tr>\n"
        "</w:tbl>\n"
    )
    image = (
        "\n<w:r><w:drawing><wp:inline>"
        '<wp:extent cx="9525" cy="9525"/>'
        '<wp:docPr id="1" name="合成示意图" descr="合成图片说明"/>'
        "<a:graphic><a:graphicData><pic:pic><pic:blipFill>"
        '<a:blip r:embed="rIdImage"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic>"
        "</wp:inline></w:drawing></w:r>\n"
    )
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>合成一级标题</w:t></w:r></w:p>"
        + _list_item("一级列表", 0)
        + _list_item("二级列表", 1)
        + table
        + '<w:p><w:commentRangeStart w:id="4"/><w:r><w:t>正文与图：</w:t></w:r>'
        + image
        + image.replace('id="1"', 'id="2"')
        + "<w:r><w:br/><w:t>换行后正文</w:t>"
        '<w:br w:type="page"/><w:t>跨页后正文</w:t></w:r>'
        '<w:commentRangeEnd w:id="4"/>'
        '<w:r><w:commentReference w:id="4"/></w:r></w:p>'
        "<w:p><w:r><w:pict><v:shape><v:textbox><w:txbxContent>"
        "<w:p><w:r><w:t>文本框正文</w:t></w:r></w:p>"
        "</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"
        '<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>'
        '<w:footerReference w:type="default" r:id="rIdFooter"/></w:sectPr>'
    )
    comments = (
        f'<w:comments xmlns:w="{_WORD_NS}"><w:comment w:id="4" '
        'w:author="合成审核人"><w:p><w:r><w:t>合成批注</w:t>'
        "</w:r></w:p></w:comment></w:comments>"
    )
    header = (
        f'<w:hdr xmlns:w="{_WORD_NS}"><w:p><w:r><w:t>合成页眉</w:t>'
        "</w:r></w:p></w:hdr>"
    )
    footer = (
        f'<w:ftr xmlns:w="{_WORD_NS}"><w:p><w:r><w:t>合成页脚</w:t>'
        "</w:r></w:p></w:ftr>"
    )
    return build_package(
        body,
        numbering=_numbering(),
        document_relationships=_relationships(),
        extra_entries={
            "word/comments.xml": comments,
            "word/header1.xml": header,
            "word/footer1.xml": footer,
            "word/media/image1.png": _ONE_PIXEL_PNG,
        },
    )


def build_normalized_baseline() -> dict[str, Any]:
    """生成排除耗时的 IR、三视图、来源和结构快照。"""
    content = build_v3_00_docx()
    parser = WordDocumentV1Parser(clock=lambda: 0.0)
    chunker = DocxStructuralChunker()
    context = ParseContext(
        document=DocumentRef(
            project_id=f"prj_{'1' * 32}",
            knowledge_base_id=f"kb_{'2' * 32}",
            document_id=f"doc_{'3' * 32}",
            display_name="v3-00-synthetic.docx",
        )
    )
    parsed = parser.parse(
        ParseSource(
            media_type=_DOCX_MEDIA_TYPE,
            display_name=context.document.display_name,
            content=content,
            extension=".docx",
        ),
        ParsingPolicy(
            comments=CommentsPolicy.INCLUDE,
            headers_footers=StoryPolicy.PARSE,
            images=ImagesPolicy.EXTRACT,
        ),
        context,
    )
    chunked = chunker.chunk(
        parsed.document_ir,
        ChunkingContext(
            chunker_fingerprint=chunker.fingerprint,
            index_revision_id=deterministic_id(
                "irev",
                parsed.document_ir.version.document_version_id,
                chunker.fingerprint,
            ),
        ),
    )
    document_ir = parsed.document_ir.model_dump(mode="json")
    document_ir["parse_report"]["elapsed_seconds"] = 0.0
    chunking_report = chunked.report.model_dump(mode="json")
    chunking_report["elapsed_seconds"] = 0.0
    return {
        "schema_version": "v3-00-docx-protection-1",
        "document_ir": document_ir,
        "artifacts": [
            {
                "artifact_id": item.artifact_id,
                "content_sha256": item.content_sha256,
                "media_type": item.media_type,
                "role": item.role,
                "size_bytes": len(item.content),
            }
            for item in parsed.artifacts
        ],
        "chunks": [item.model_dump(mode="json") for item in chunked.chunks],
        "chunking_report": chunking_report,
    }


def test_native_docx_structure_and_views_match_frozen_snapshot() -> None:
    observed = build_normalized_baseline()
    expected = json.loads(_EXPECTED.read_text(encoding="utf-8"))

    assert observed == expected
    assert build_normalized_baseline() == observed
    nodes = observed["document_ir"]["nodes"]
    kinds = {node["kind"] for node in nodes}
    stories = {node["anchor"]["story_kind"] for node in nodes}
    assert kinds >= {
        NodeKind.HEADING.value,
        NodeKind.LIST_ITEM.value,
        NodeKind.TABLE.value,
        NodeKind.TABLE_CELL.value,
        NodeKind.IMAGE.value,
        NodeKind.COMMENT.value,
        NodeKind.BREAK.value,
    }
    assert stories >= {
        StoryKind.HEADER.value,
        StoryKind.FOOTER.value,
        StoryKind.TEXT_BOX.value,
    }
    images = [node for node in nodes if node["kind"] == NodeKind.IMAGE.value]
    assert len(images) == 2
    assert len({node["image_attributes"]["blob_ref"] for node in images}) == 1
    table_rows = [
        node for node in nodes if node["kind"] == NodeKind.TABLE_ROW.value
    ]
    assert any(dict(node["metadata"])["repeated_header"] for node in table_rows)
    assert any(
        span["span_type"] == SourceSpanKind.REPEATED_CONTEXT.value
        for chunk in observed["chunks"]
        for span in chunk["source_spans"]
    )
    assert all(
        chunk["citation_text"]
        and chunk["embedding_text"]
        and chunk["lexical_text"]
        for chunk in observed["chunks"]
    )


def test_default_product_probe_uses_word_parser_and_structural_chunker(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v3-00-synthetic.docx"
    source.write_bytes(build_v3_00_docx())

    first = run_probe(source, include_content=True)
    second = run_probe(source, include_content=True)

    assert first == second
    assert first["product_components"]["parser"]["name"] == "word-document-v1"
    assert (
        first["product_components"]["chunker"]["name"] == "docx-structural-v3"
    )
    assert first["media_scan"]["inventory_status"] == "confirmed"
    assert first["parse"]["image_display_instances"] == 2
    assert first["parse"]["unique_embedded_media"] == 1


def test_flattened_doc_pic_placeholder_keeps_media_inventory_unknown() -> None:
    parser = WordDocumentV1Parser(
        doc_extractor=lambda _content, _policy: "正文\n[pic]\n图下注释"
    )
    content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"synthetic"
    parsed = parser.parse(
        ParseSource(
            media_type="application/msword",
            display_name="synthetic.doc",
            content=content,
            extension=".doc",
        ),
        ParsingPolicy(),
        ParseContext(
            document=DocumentRef(
                project_id=f"prj_{'4' * 32}",
                knowledge_base_id=f"kb_{'5' * 32}",
                document_id=f"doc_{'6' * 32}",
                display_name="synthetic.doc",
            )
        ),
    )

    assert classify_media_inventory(parsed.document_ir) == (
        "unknown",
        "unknown_after_flattening",
    )
    assert all(
        node.image_attributes is None for node in parsed.document_ir.nodes
    )
    assert {item.role for item in parsed.artifacts} == {"source_document"}


def test_word_probe_rejects_symlink_before_resolving(tmp_path: Path) -> None:
    source = tmp_path / "v3-00-synthetic.docx"
    source.write_bytes(build_v3_00_docx())
    link = tmp_path / "linked.docx"
    link.symlink_to(source)

    with pytest.raises(ValueError, match="symlink"):
        run_probe(link)
