"""统一 Word parser 的 DOC/DOCX 回归。"""

from __future__ import annotations

# ruff: noqa: E501
import hashlib
from pathlib import Path

import pytest

from rag_app.adapters.chunkers import DocxStructuralChunker
from rag_app.adapters.parsers import word_document
from rag_app.adapters.parsers.doc_conversion import (
    DocConversion,
    DocConversionUnavailableError,
    SandboxedLibreOfficeConverter,
)
from rag_app.adapters.parsers.word_document import WordDocumentV1Parser
from rag_app.core.errors import InvalidDocument
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    DocumentRef,
    NodeKind,
    ParseContext,
    ParseSource,
    validate_document_ir,
)
from rag_app.core.policies import ParsingPolicy
from tests.adapters.parsers.docx.fixtures import build_package
from tests.ole_doc_fixture import build_ole_word_container

_DOC_MEDIA_TYPE = "application/msword"
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_CONTENT_TYPES_WITH_IMAGE = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
_RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
  <Relationship Id="rIdExternal" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/synthetic" TargetMode="External"/>
</Relationships>
"""
_CONVERTED_BLOCKS = """
<w:p><w:r><w:t>公开合成检修说明</w:t></w:r></w:p>
<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:tr>
  <w:tc><w:p><w:r><w:t>参数</w:t></w:r></w:p></w:tc>
  <w:tc><w:p><w:r><w:t>17 kPa</w:t></w:r></w:p></w:tc>
</w:tr></w:tbl>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="9525"/><wp:docPr id="1" name="合成图一"/><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImage"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="9525"/><wp:docPr id="2" name="合成图二"/><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rIdImage"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:p><w:hyperlink r:id="rIdExternal"><w:r><w:t>保留显示文字</w:t></w:r></w:hyperlink></w:p>
"""


def test_parser_descriptor_tracks_default_converter_recipe() -> None:
    assert (
        SandboxedLibreOfficeConverter.recipe
        in WordDocumentV1Parser.descriptor.version
    )


class _StaticConverter:
    """返回通用合成 DOCX 的注入式 converter。"""

    def __init__(self, content: bytes) -> None:
        self._content = content

    def convert(self, *_args: object, **_kwargs: object) -> DocConversion:
        return DocConversion(
            content=self._content,
            converter="synthetic-converter",
            converter_version="1.2.3",
            recipe="synthetic-bounded-v1",
        )


def _context() -> ParseContext:
    return ParseContext(
        document=DocumentRef(
            project_id=f"prj_{'1' * 32}",
            knowledge_base_id=f"kb_{'2' * 32}",
            document_id=f"doc_{'3' * 32}",
            display_name="质量制度.doc",
        )
    )


def _doc_source(
    *,
    content: bytes | None = None,
    media_type: str = _DOC_MEDIA_TYPE,
    extension: str = ".doc",
) -> ParseSource:
    return ParseSource(
        media_type=media_type,
        display_name="质量制度.doc",
        content=content or build_ole_word_container(),
        extension=extension,
    )


def test_doc_parser_preserves_source_identity_and_reports_flattening() -> None:
    content = build_ole_word_container()
    parser = WordDocumentV1Parser(
        doc_converter=_UnavailableConverter(),
        doc_extractor=lambda _content, _policy: (
            "  第一条制度  \n\n第二条制度\n"
        ),
    )

    result = parser.parse(
        _doc_source(content=content),
        ParsingPolicy(),
        _context(),
    )
    repeated = parser.parse(
        _doc_source(content=content),
        ParsingPolicy(),
        _context(),
    )
    digest = hashlib.sha256(content).hexdigest()

    assert [node.text for node in result.document_ir.nodes] == [
        "第一条制度",
        "第二条制度",
    ]
    assert result.document_ir.source.content_sha256 == digest
    assert result.document_ir.source.extension == ".doc"
    assert result.document_ir.source.media_type == _DOC_MEDIA_TYPE
    assert result.document_ir.version.content_sha256 == digest
    assert result.artifacts[0].content == content
    assert result.report.warnings == (
        "DOC_CONVERTER_UNAVAILABLE_FALLBACK",
        "LEGACY_DOC_FLATTENED_TEXT",
    )
    first_node = result.document_ir.nodes[0]
    assert first_node.anchor.source_start_char == 0
    assert first_node.anchor.source_end_char == len(first_node.text)
    assert result.document_ir.nodes == repeated.document_ir.nodes


@pytest.mark.parametrize(
    ("source", "stage"),
    [
        (
            _doc_source(content=b"not-an-ole-document"),
            "word-document-v1.input",
        ),
        (
            _doc_source(media_type=_DOCX_MEDIA_TYPE),
            "word-document-v1.input",
        ),
        (
            _doc_source(extension=".txt"),
            "word-document-v1.input",
        ),
    ],
)
def test_doc_parser_rejects_format_contract_mismatch(
    source: ParseSource,
    stage: str,
) -> None:
    parser = WordDocumentV1Parser(
        doc_converter=_UnavailableConverter(),
        doc_extractor=lambda _content, _policy: "不会执行",
    )

    with pytest.raises(InvalidDocument) as captured:
        parser.parse(source, ParsingPolicy(), _context())

    assert captured.value.stage == stage


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"{\\rtf1\\ansi renamed}", "RTF"),
        (b"<!doctype html><p>renamed</p>", "HTML"),
        (build_package("<w:p/>"), "OOXML"),
        (
            build_ole_word_container(stream_name="Workbook"),
            "不是受支持的 Word DOC",
        ),
    ],
)
def test_doc_parser_rejects_renamed_or_foreign_formats(
    content: bytes,
    message: str,
) -> None:
    parser = WordDocumentV1Parser(doc_converter=_UnavailableConverter())

    with pytest.raises(InvalidDocument) as captured:
        parser.parse(_doc_source(content=content), ParsingPolicy(), _context())

    assert captured.value.stage == "word-document-v1.input"
    assert message in captured.value.safe_message


def test_doc_parser_accepts_non_sector_aligned_word_cfb() -> None:
    derived = build_package(
        "<w:p><w:r><w:t>公开合成尾段兼容</w:t></w:r></w:p>"
    )
    original = build_ole_word_container() + b"public-synthetic-tail"
    parser = WordDocumentV1Parser(doc_converter=_StaticConverter(derived))

    result = parser.parse(
        _doc_source(content=original),
        ParsingPolicy(),
        _context(),
    )

    assert result.document_ir.source.content_sha256 == hashlib.sha256(
        original
    ).hexdigest()
    assert any(
        node.text == "公开合成尾段兼容"
        for node in result.document_ir.nodes
    )


def test_converted_doc_maps_structure_media_and_artifacts_to_original() -> None:
    derived = build_package(
        _CONVERTED_BLOCKS,
        document_relationships=_RELATIONSHIPS,
        extra_entries={"word/media/image1.png": b"synthetic-image"},
        content_types=_CONTENT_TYPES_WITH_IMAGE,
    )
    original = build_ole_word_container()
    parser = WordDocumentV1Parser(
        doc_converter=_StaticConverter(derived),
    )

    result = parser.parse(
        _doc_source(content=original),
        ParsingPolicy(images="extract"),
        _context(),
    )
    repeated = parser.parse(
        _doc_source(content=original),
        ParsingPolicy(images="extract"),
        _context(),
    )
    validate_document_ir(result.document_ir)
    metadata = dict(result.document_ir.metadata)
    images = [
        node for node in result.document_ir.nodes if node.kind is NodeKind.IMAGE
    ]

    assert (
        result.document_ir.version.content_sha256
        == hashlib.sha256(original).hexdigest()
    )
    assert result.document_ir.source.extension == ".doc"
    assert result.document_ir.source.media_type == _DOC_MEDIA_TYPE
    assert result.document_ir.nodes == repeated.document_ir.nodes
    assert any(node.kind is NodeKind.TABLE for node in result.document_ir.nodes)
    assert any(node.text == "17 kPa" for node in result.document_ir.nodes)
    assert len(images) == 2
    assert images[0].node_id != images[1].node_id
    assert images[0].anchor != images[1].anchor
    assert images[0].image_attributes is not None
    assert images[1].image_attributes is not None
    assert (
        images[0].image_attributes.blob_ref
        == images[1].image_attributes.blob_ref
    )
    assert all(
        node.anchor.part_uri.startswith("/converted-docx/")
        for node in result.document_ir.nodes
    )
    assert {artifact.role for artifact in result.artifacts} == {
        "source_document",
        "derived_document",
        "embedded_media",
    }
    assert (
        len(
            [
                artifact
                for artifact in result.artifacts
                if artifact.role == "embedded_media"
            ]
        )
        == 1
    )
    assert metadata["mapping_quality"] == "converted_instance"
    assert metadata["media_inventory"] == "partial"
    assert metadata["relationship_status"] == "external_removed"
    derived_artifact = next(
        artifact
        for artifact in result.artifacts
        if artifact.role == "derived_document"
    )
    assert b"example.invalid" not in derived_artifact.content
    assert "DOC_EXTERNAL_RELATIONSHIPS_REMOVED" in result.report.warnings


def test_converted_doc_long_table_keeps_all_rows_and_cells() -> None:
    rows = "".join(
        "<w:tr>"
        f"<w:tc><w:p><w:r><w:t>序号 {index}</w:t></w:r></w:p></w:tc>"
        f"<w:tc><w:p><w:r><w:t>值 {index * 7}</w:t></w:r></w:p></w:tc>"
        "</w:tr>"
        for index in range(120)
    )
    derived = build_package(
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
        + rows
        + "</w:tbl>"
    )
    parsed = WordDocumentV1Parser(
        doc_converter=_StaticConverter(derived)
    ).parse(_doc_source(), ParsingPolicy(), _context())
    chunker = DocxStructuralChunker()

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

    assert chunked.report.table_row_count == 120
    assert chunked.report.represented_table_row_count == 120
    assert chunked.report.table_cell_count == 240
    assert chunked.report.represented_table_cell_count == 240
    assert chunked.report.missing_source_chars == 0
    assert chunked.report.source_span_coverage == 1.0


@pytest.mark.parametrize("text", ["", " \n\t", "正文\x00隐藏"])
def test_doc_parser_rejects_empty_or_invalid_extracted_text(text: str) -> None:
    parser = WordDocumentV1Parser(
        doc_converter=_UnavailableConverter(),
        doc_extractor=lambda _content, _policy: text,
    )

    with pytest.raises(InvalidDocument):
        parser.parse(_doc_source(), ParsingPolicy(), _context())


def test_docx_is_delegated_to_existing_ooxml_parser() -> None:
    parser = WordDocumentV1Parser(doc_converter=_UnavailableConverter())
    source = ParseSource(
        media_type=_DOCX_MEDIA_TYPE,
        display_name="公开合成.docx",
        content=build_package("<w:p><w:r><w:t>公开合成内容</w:t></w:r></w:p>"),
        extension=".docx",
    )

    result = parser.parse(source, ParsingPolicy(), _context())

    assert result.report.parser_id == "docx-ooxml-v4"
    assert result.document_ir.source.extension == ".docx"
    assert any(node.text == "公开合成内容" for node in result.document_ir.nodes)


def test_missing_antiword_fails_with_safe_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        word_document,
        "_ANTIWORD_EXECUTABLE",
        str(tmp_path / "missing-antiword"),
    )
    parser = WordDocumentV1Parser(doc_converter=_UnavailableConverter())

    with pytest.raises(InvalidDocument) as captured:
        parser.parse(_doc_source(), ParsingPolicy(), _context())

    assert captured.value.stage == "word-document-v1.runtime"
    assert str(tmp_path) not in captured.value.safe_message


def test_antiword_process_timeout_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "slow-antiword"
    executable.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(
        word_document,
        "_ANTIWORD_EXECUTABLE",
        str(executable),
    )
    parser = WordDocumentV1Parser(doc_converter=_UnavailableConverter())

    with pytest.raises(InvalidDocument) as captured:
        parser.parse(
            _doc_source(),
            ParsingPolicy(parse_timeout_seconds=0.01),
            _context(),
        )

    assert captured.value.stage == "word-document-v1.timeout"


class _UnavailableConverter:
    """强制覆盖本用例的显式 Antiword fallback 边界。"""

    def convert(self, *_args: object, **_kwargs: object) -> object:
        raise DocConversionUnavailableError("synthetic unavailable")
