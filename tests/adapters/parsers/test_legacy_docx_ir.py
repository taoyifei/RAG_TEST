from __future__ import annotations

import pytest

from rag_app.adapters.parsers.legacy_docx_ir import LegacyDocxIrParser
from rag_app.application.artifacts import persist_artifacts_transactionally
from rag_app.composition.registry import (
    ComponentRegistry,
    register_builtin_components,
)
from rag_app.core.capabilities import ComponentDescriptor
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import (
    DocumentRef,
    NodeKind,
    ParseContext,
    ParseSource,
)
from rag_app.core.policies import (
    ExternalRelationshipsPolicy,
    ParsingMode,
    ParsingPolicy,
    UnknownIndexableContentPolicy,
)
from rag_app.core.ports.blob_store import (
    BlobPutResult,
    BlobReadResult,
    BlobWriteRequest,
)
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
    return ParsingPolicy.model_validate(updates)


def _context(document_id: str | None = None) -> ParseContext:
    return ParseContext(
        document=DocumentRef(
            project_id=f"prj_{'1' * 32}",
            knowledge_base_id=f"kb_{'2' * 32}",
            document_id=document_id or f"doc_{'3' * 32}",
            display_name="sample.docx",
        )
    )


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

    first = parser.parse(
        _source(content, "first-name.bin"), _policy(), _context()
    )
    renamed = parser.parse(
        _source(content, "renamed.docx"), _policy(), _context()
    )

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
    first = parser.parse(
        _source(build_docx(PARAGRAPH)), _policy(), _context()
    )
    changed = parser.parse(
        _source(build_docx(PARAGRAPH.replace("第一步", "第二步"))),
        _policy(),
        _context(),
    )

    assert first.document_ir.version != changed.document_ir.version
    assert (
        first.document_ir.nodes[0].node_id
        != changed.document_ir.nodes[0].node_id
    )


def test_table_has_explicit_parent_representation_and_loss_issue() -> None:
    result = LegacyDocxIrParser().parse(
        _source(build_docx(TABLE)), _policy(), _context()
    )
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
    result = LegacyDocxIrParser().parse(
        _source(
            build_docx(
                IMAGE,
                relationships=relationship,
                extra_entries={"word/media/pixel.png": b"synthetic-png"},
            )
        ),
        _policy(),
        _context(),
    )
    node = result.document_ir.nodes[0]

    assert node.kind is NodeKind.IMAGE
    assert node.image_attributes is not None
    assert any(
        artifact.artifact_id == node.image_attributes.blob_ref
        for artifact in result.artifacts
    )
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

    result = LegacyDocxIrParser().parse(
        _source(content), _policy(), _context()
    )
    assert "DOCX_EXTERNAL_RELATIONSHIP_SKIPPED" in result.report.warnings

    with pytest.raises(InvalidDocument):
        LegacyDocxIrParser().parse(
            _source(content),
            _policy(
                external_relationships=ExternalRelationshipsPolicy.REJECT
            ),
            _context(),
        )


def test_unsafe_archive_path_is_rejected() -> None:
    with pytest.raises(InvalidDocument):
        LegacyDocxIrParser().parse(
            _source(build_docx(PARAGRAPH, unsafe_entry=True)),
            _policy(),
            _context(),
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
        _context(),
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
            _context(),
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
        _context(),
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

    def __init__(self, existing: BlobWriteRequest) -> None:
        self.items = {
            existing.blob_id: BlobReadResult(**existing.model_dump())
        }
        self.deleted: list[str] = []

    def put_if_absent(self, request: BlobWriteRequest) -> BlobPutResult:
        if request.blob_id in self.items:
            return BlobPutResult.EXISTING
        raise OSError("injected")

    def read(self, blob_id: str) -> BlobReadResult | None:
        return self.items.get(blob_id)

    def exists(self, blob_id: str) -> bool:
        return blob_id in self.items

    def delete(self, blob_id: str) -> None:
        self.deleted.append(blob_id)
        self.items.pop(blob_id, None)

    def close(self) -> None:
        return None


def test_blob_failure_preserves_preexisting_shared_artifact() -> None:
    relationship = (
        '<Relationship Id="rIdImage" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/image" Target="media/pixel.png"/>'
    )
    content = build_docx(
        IMAGE,
        relationships=relationship,
        extra_entries={"word/media/pixel.png": b"synthetic-png"},
    )

    result = LegacyDocxIrParser().parse(
        _source(content), _policy(), _context()
    )
    source_artifact = result.artifacts[0]
    existing = BlobWriteRequest(
        blob_id=source_artifact.artifact_id,
        content_sha256=source_artifact.content_sha256,
        media_type=source_artifact.media_type,
        content=source_artifact.content,
    )
    store = _FailingBlobStore(existing)

    with pytest.raises(OSError, match="injected"):
        persist_artifacts_transactionally(result.artifacts, store)

    assert store.exists(source_artifact.artifact_id)
    assert store.deleted == []
