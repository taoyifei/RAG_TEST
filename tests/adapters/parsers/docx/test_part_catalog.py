from __future__ import annotations

# ruff: noqa: E501
import pytest

from rag_app.adapters.parsers.docx.package import DocxPackage
from rag_app.core.errors import InvalidDocument
from tests.adapters.parsers.docx.fixtures import build_package, policy


def test_catalog_normalizes_internal_and_external_relationships() -> None:
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rIdLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/private" TargetMode="External"/>
</Relationships>
"""

    with DocxPackage(
        build_package("", document_relationships=relationships),
        policy(),
    ) as package:
        assert package.catalog.main_part_uri == "/word/document.xml"
        assert package.catalog.part("/word/styles.xml") is not None
        link = package.relationship("/word/document.xml", "rIdLink")

    assert link is not None
    assert link.target_mode == "External"
    assert link.target_part_uri is None
    assert link.external_scheme == "https"


def test_relationship_to_missing_internal_part_is_fatal() -> None:
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdMissing" Type="urn:test" Target="missing.xml"/>
</Relationships>
"""

    with pytest.raises(InvalidDocument, match="缺失 Part"):
        DocxPackage(
            build_package("", document_relationships=relationships),
            policy(),
        )
