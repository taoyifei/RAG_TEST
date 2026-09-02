from __future__ import annotations

# ruff: noqa: E501
import io
import zipfile

import pytest

from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import ParseSource
from tests.adapters.parsers.docx.fixtures import build_package, policy


def _source(content: bytes, extension: str = ".docx") -> ParseSource:
    return ParseSource(
        media_type="application/octet-stream",
        display_name=f"sample{extension}",
        extension=extension,
        content=content,
    )


def test_valid_minimal_package_is_parsed() -> None:
    content = build_package(
        "<w:p><w:r><w:t>安全正文</w:t></w:r></w:p>"
    )

    result = DocxOoxmlV4Parser().parse(_source(content), policy())

    assert result.report.parser_id == "docx-ooxml-v4"
    assert result.document_ir.nodes[0].text == "安全正文"


@pytest.mark.parametrize("extension", [".docm", ".dotm", ".zip"])
def test_non_docx_extensions_are_rejected(extension: str) -> None:
    with pytest.raises(InvalidDocument):
        DocxOoxmlV4Parser().parse(
            _source(build_package(""), extension),
            policy(),
        )


def test_unsafe_archive_path_is_rejected_before_body_read() -> None:
    source = io.BytesIO(build_package(""))
    output = io.BytesIO()
    with (
        zipfile.ZipFile(source) as original,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        for item in original.infolist():
            archive.writestr(item.filename, original.read(item.filename))
        archive.writestr("../escape.xml", "<root/>")

    with pytest.raises(InvalidDocument, match="归档路径"):
        DocxOoxmlV4Parser().parse(_source(output.getvalue()), policy())


def test_injected_resource_limit_remains_fatal_in_best_effort() -> None:
    content = build_package(
        "<w:p><w:r><w:t>正文</w:t></w:r></w:p>"
    )

    with pytest.raises(InvalidDocument, match="文件大小"):
        DocxOoxmlV4Parser().parse(
            _source(content),
            policy(mode="best_effort", max_file_bytes=1),
        )


def test_duplicate_zip_entry_is_rejected() -> None:
    output = io.BytesIO(build_package(""))
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(output, "a", zipfile.ZIP_DEFLATED) as archive,
    ):
        archive.writestr("word/document.xml", "<duplicate/>")

    with pytest.raises(InvalidDocument, match="重复条目"):
        DocxOoxmlV4Parser().parse(_source(output.getvalue()), policy())


def test_macro_content_type_is_always_fatal() -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>
</Types>
"""

    with pytest.raises(InvalidDocument, match="宏"):
        DocxOoxmlV4Parser().parse(
            _source(build_package("", content_types=content_types)),
            policy(mode="best_effort"),
        )


def test_xml_depth_and_entry_count_limits_are_injected_without_large_files() -> None:
    content = build_package(
        "<w:p><w:r><w:t>正文</w:t></w:r></w:p>"
    )

    with pytest.raises(InvalidDocument, match="条目数"):
        DocxOoxmlV4Parser().parse(
            _source(content),
            policy(max_entries=2),
        )
    with pytest.raises(InvalidDocument, match="XML 深度"):
        DocxOoxmlV4Parser().parse(
            _source(content),
            policy(max_xml_depth=2),
        )


def test_monotonic_timeout_is_fatal() -> None:
    ticks = iter((0.0, 0.0, 31.0, 31.0))

    with pytest.raises(InvalidDocument, match="耗时"):
        DocxOoxmlV4Parser(clock=lambda: next(ticks)).parse(
            _source(build_package("")),
            policy(parse_timeout_seconds=30.0),
        )


def test_external_relationship_reject_policy_never_downgrades() -> None:
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/private" TargetMode="External"/>
</Relationships>
"""

    with pytest.raises(InvalidDocument, match="外部关系"):
        DocxOoxmlV4Parser().parse(
            _source(
                build_package(
                    '<w:p><w:hyperlink r:id="rIdLink"><w:r><w:t>显示文字</w:t></w:r></w:hyperlink></w:p>',
                    document_relationships=relationships,
                )
            ),
            policy(
                mode="best_effort",
                external_relationships="reject",
            ),
        )
