"""DOC 派生 OOXML 的通用安全清洗回归。"""

from __future__ import annotations

# ruff: noqa: E501
import io
import zipfile

import pytest

from rag_app.adapters.parsers.derived_docx import sanitize_derived_docx
from rag_app.core.errors import InvalidDocument
from rag_app.core.policies import ParsingPolicy
from tests.adapters.parsers.docx.fixtures import build_package

_RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdExternal" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/resource" TargetMode="External"/>
</Relationships>
"""


def test_sanitizer_removes_external_relationship_and_reference() -> None:
    source = build_package(
        '<w:p><w:hyperlink r:id="rIdExternal"><w:r><w:t>公开文字</w:t></w:r></w:hyperlink></w:p>',
        document_relationships=_RELATIONSHIPS,
    )

    cleaned, removed = sanitize_derived_docx(source, ParsingPolicy())

    assert removed == 1
    assert b"example.invalid" not in cleaned
    assert b"rIdExternal" not in cleaned
    with zipfile.ZipFile(io.BytesIO(cleaned)) as archive:
        assert archive.read("word/document.xml").count("公开文字".encode()) == 1


@pytest.mark.parametrize(
    ("entry_name", "payload"),
    [
        ("word/embeddings/object1.bin", b"synthetic"),
        ("word/activeX/activeX1.bin", b"synthetic"),
        ("word/vbaProject.bin", b"synthetic"),
    ],
)
def test_sanitizer_rejects_embedded_executable_parts(
    entry_name: str,
    payload: bytes,
) -> None:
    source = build_package(
        "<w:p><w:r><w:t>公开文字</w:t></w:r></w:p>",
        extra_entries={entry_name: payload},
    )

    with pytest.raises(InvalidDocument) as captured:
        sanitize_derived_docx(source, ParsingPolicy())

    assert captured.value.stage == "word-document-v2.derived_ooxml"


def test_sanitizer_rejects_xml_dtd() -> None:
    source = build_package(
        "<w:p/>",
        extra_entries={
            "word/custom.xml": (
                '<!DOCTYPE x [<!ENTITY e "blocked">]><x>&e;</x>'
            )
        },
    )

    with pytest.raises(InvalidDocument, match="DTD"):
        sanitize_derived_docx(source, ParsingPolicy())


def test_sanitizer_rejects_ole_content_type_outside_standard_path() -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/custom/object.bin" ContentType="application/vnd.openxmlformats-officedocument.oleObject"/>
</Types>
"""
    source = build_package(
        "<w:p/>",
        content_types=content_types,
        extra_entries={"custom/object.bin": b"synthetic"},
    )

    with pytest.raises(InvalidDocument, match="OLE"):
        sanitize_derived_docx(source, ParsingPolicy())
