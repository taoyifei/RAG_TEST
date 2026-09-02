from __future__ import annotations

import hashlib

import pytest

from rag_app.adapters.legacy.stores import InMemoryBlobStore
from rag_app.adapters.parsers.legacy_docx_ir import LegacyDocxIrParser
from rag_app.composition.registry import (
    ComponentRegistry,
    register_builtin_components,
)
from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import NodeKind, ParseSource
from rag_app.core.policies import (
    ExternalRelationshipsPolicy,
    ParsingMode,
    ParsingPolicy,
    UnknownIndexableContentPolicy,
)
from rag_app.core.ports.blob_store import BlobReadResult, BlobWriteRequest
from tests.adapters.parsers.docx_fixtures import (
    CONTENT_CONTROL,
    HEADING,
    IMAGE,
    LIST,
    PARAGRAPH,
    TABLE,
    build_docx,
)

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document"
)


def _policy(**updates: object) -> ParsingPolicy:
    values = {
        "metadata": (
            ("project_id", f"prj_{'1' * 32}"),
            ("knowledge_base_id", f"kb_{'2' * 32}"),
            ("document_id", f"doc_{'3' * 32}"),
        )
    }
    values.update(updates)
    return ParsingPolicy.model_validate(values)


def _source(content: bytes, name: str = "sample.docx") -> ParseSource:
    return ParseSource(
        media_type="application/octet-stream",
        display_name=name,
        extension=".wrong",
        content=content,
    )


def test_heading_paragraph_list_content_control_and_rename_are_stable() -> None:
    content = build_docx(HEADING + PARAGRAPH + LIST + CONTENT_CONTROL)
    parser = LegacyDocxIrParser()

    first = parser.parse(_source(content, "first-name.bin"), _policy())
    renamed = parser.parse(_source(content, "renamed.docx"), _policy())

    assert [node.kind for node in first.document_ir.nodes] == [
        NodeKind.HEADING,
        NodeKind.PARAGRAPH,
        NodeKind.LIST_ITEM,
        NodeKind.PARAGRAPH,
    ]
    assert first.document_ir.nodes[1].text == "第一步\t准备环境\n完成检查"
    assert first.document_ir.nodes[2].list_attributes is not None
    assert first.document_ir.nodes[2].list_attributes.level == 0
    assert [node.node_id for node in first.document_ir.nodes] == [
        node.node_id for node in renamed.document_ir.nodes
    ]
    assert first.document_ir.version == renamed.document_ir.version
    assert renamed.document_ir.source.display_name == "renamed.docx"


def test_content_change_changes_version_and_nodes() -> None:
    parser = LegacyDocxIrParser()
    first = parser.parse(_source(build_docx(PARAGRAPH)), _policy())
    changed = parser.parse(
        _source(build_docx(PARAGRAPH.replace("第一步", "第二步"))),
        _policy(),
    )

    assert first.document_ir.version != changed.document_ir.version
    assert (
        first.document_ir.nodes[0].node_id
        != changed.document_ir.nodes[0].node_id
    )


def test_table_has_explicit_parent_representation_and_loss_issue() -> None:
    result = LegacyDocxIrParser().parse(_source(build_docx(TABLE)), _policy())
    table, representation = result.document_ir.nodes

    assert table.kind is NodeKind.TABLE
    assert representation.kind is NodeKind.TABLE_REPRESENTATION
    assert table.child_ids == (representation.node_id,)
    assert representation.parent_node_id == table.node_id
    assert representation.text == "A | B"
    assert dict(table.metadata)["legacy_flattened_table"] is True
    assert "LEGACY_TABLE_STRUCTURE_LOSS" in result.report.warnings


def test_image_binary_is_replaced_by_blob_ref() -> None:
    relationship = (
        '<Relationship Id="rIdImage" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/image" Target="media/pixel.png"/>'
    )
    store = InMemoryBlobStore()
    result = LegacyDocxIrParser(store).parse(
        _source(
            build_docx(
                IMAGE,
                relationships=relationship,
                extra_entries={"word/media/pixel.png": b"synthetic-png"},
            )
        ),
        _policy(),
    )
    node = result.document_ir.nodes[0]

    assert node.kind is NodeKind.IMAGE
    assert node.image_attributes is not None
    assert store.get(node.image_attributes.blob_ref) is not None
    rendered = result.document_ir.model_dump_json()
    assert "synthetic-png" not in rendered
    assert "binary_data" not in rendered


def test_external_relationship_is_reported_or_rejected_without_access() -> None:
    relationship = (
        '<Relationship Id="rIdExternal" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/hyperlink" Target="https://example.invalid/" '
        'TargetMode="External"/>'
    )
    content = build_docx(PARAGRAPH, relationships=relationship)

    result = LegacyDocxIrParser().parse(_source(content), _policy())
    assert "DOCX_EXTERNAL_RELATIONSHIP_SKIPPED" in result.report.warnings

    with pytest.raises(InvalidDocument):
        LegacyDocxIrParser().parse(
            _source(content),
            _policy(
                external_relationships=ExternalRelationshipsPolicy.REJECT
            ),
        )


def test_unsafe_archive_path_is_rejected() -> None:
    with pytest.raises(InvalidDocument):
        LegacyDocxIrParser().parse(
            _source(build_docx(PARAGRAPH, unsafe_entry=True)),
            _policy(),
        )


def test_best_effort_records_unknown_text_without_relaxing_size_limit() -> None:
    unknown = (
        "<w:customXml><w:p><w:r><w:t>未知证据</w:t></w:r></w:p>"
        "</w:customXml>"
    )
    content = build_docx(unknown)
    result = LegacyDocxIrParser().parse(
        _source(content),
        _policy(
            mode=ParsingMode.BEST_EFFORT,
            unknown_indexable_content=UnknownIndexableContentPolicy.ISSUE,
        ),
    )

    assert result.document_ir.nodes == ()
    assert result.report.visible_text_nodes == 1
    assert result.report.represented_visible_text_nodes == 0
    assert result.report.unsupported_with_text == 1
    assert result.report.coverage == 0.0
    assert "DOCX_UNSUPPORTED_NODE" in result.report.warnings

    with pytest.raises(InvalidDocument):
        LegacyDocxIrParser().parse(
            _source(content),
            _policy(
                mode=ParsingMode.BEST_EFFORT,
                unknown_indexable_content=(
                    UnknownIndexableContentPolicy.ISSUE
                ),
                max_file_bytes=1,
            ),
        )


def test_unsupported_image_media_is_counted_without_binary_leakage() -> None:
    relationship = (
        '<Relationship Id="rIdImage" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/image" Target="media/vector.svg"/>'
    )
    result = LegacyDocxIrParser().parse(
        _source(
            build_docx(
                IMAGE,
                relationships=relationship,
                extra_entries={"word/media/vector.svg": b"<svg/>"},
            )
        ),
        _policy(),
    )

    assert result.report.unsupported_with_media == 1
    assert "DOCX_IMAGE_UNSUPPORTED_MEDIA" in result.report.warnings
    assert result.document_ir.nodes == ()


def test_registry_exposes_truthful_legacy_ir_capabilities() -> None:
    registry = ComponentRegistry()
    register_builtin_components(registry)
    parser = registry.get_parser("legacy-docx-ir")

    assert isinstance(parser, LegacyDocxIrParser)
    assert parser.parser_capabilities.supports_tables == "partial"
    assert parser.parser_capabilities.supports_revisions is False
    assert parser.parser_capabilities.schema_version == "1"


class _FailingBlobStore:
    descriptor = ComponentDescriptor(
        kind="blob_store",
        name="failing-test",
        version="1",
        mode="deterministic",
    )

    def __init__(self) -> None:
        self.items: dict[str, BlobWriteRequest] = {}
        self.deleted: list[str] = []

    def put(self, request: BlobWriteRequest) -> None:
        if self.items:
            raise OSError("injected")
        self.items[request.blob_id] = request

    def get(self, blob_id: str) -> BlobReadResult | None:
        del blob_id
        return None

    def delete(self, blob_id: str) -> None:
        self.deleted.append(blob_id)
        self.items.pop(blob_id, None)

    def close(self) -> None:
        return None


def test_blob_failure_cleans_already_written_document() -> None:
    relationship = (
        '<Relationship Id="rIdImage" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/image" Target="media/pixel.png"/>'
    )
    store = _FailingBlobStore()
    content = build_docx(
        IMAGE,
        relationships=relationship,
        extra_entries={"word/media/pixel.png": b"synthetic-png"},
    )

    with pytest.raises(InvalidDocument):
        LegacyDocxIrParser(store).parse(_source(content), _policy())

    expected_version = hashlib.sha256(content).hexdigest()
    assert store.items == {}
    assert len(store.deleted) == 1
    assert expected_version not in repr(store)
