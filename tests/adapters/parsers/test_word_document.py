"""统一 Word parser 的 DOC/DOCX 回归。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rag_app.adapters.parsers import word_document
from rag_app.adapters.parsers.word_document import WordDocumentV1Parser
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import DocumentRef, ParseContext, ParseSource
from rag_app.core.policies import ParsingPolicy
from tests.adapters.parsers.docx.fixtures import build_package

_DOC_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_DOC_MEDIA_TYPE = "application/msword"
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
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
    content: bytes = _DOC_MAGIC + b"synthetic",
    media_type: str = _DOC_MEDIA_TYPE,
    extension: str = ".doc",
) -> ParseSource:
    return ParseSource(
        media_type=media_type,
        display_name="质量制度.doc",
        content=content,
        extension=extension,
    )


def test_doc_parser_preserves_source_identity_and_reports_flattening() -> None:
    content = _DOC_MAGIC + b"synthetic"
    parser = WordDocumentV1Parser(
        doc_extractor=lambda _content, _policy: "  第一条制度  \n\n第二条制度\n"
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
    assert result.report.warnings == ("LEGACY_DOC_FLATTENED_TEXT",)
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
        doc_extractor=lambda _content, _policy: "不会执行"
    )

    with pytest.raises(InvalidDocument) as captured:
        parser.parse(source, ParsingPolicy(), _context())

    assert captured.value.stage == stage


@pytest.mark.parametrize("text", ["", " \n\t", "正文\x00隐藏"])
def test_doc_parser_rejects_empty_or_invalid_extracted_text(text: str) -> None:
    parser = WordDocumentV1Parser(doc_extractor=lambda _content, _policy: text)

    with pytest.raises(InvalidDocument):
        parser.parse(_doc_source(), ParsingPolicy(), _context())


def test_docx_is_delegated_to_existing_ooxml_parser() -> None:
    parser = WordDocumentV1Parser()
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
    parser = WordDocumentV1Parser()

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
    parser = WordDocumentV1Parser()

    with pytest.raises(InvalidDocument) as captured:
        parser.parse(
            _doc_source(),
            ParsingPolicy(parse_timeout_seconds=0.01),
            _context(),
        )

    assert captured.value.stage == "word-document-v1.timeout"
