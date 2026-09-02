from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from rag_app.core.models import (
    DocumentIR,
    DocumentNode,
    DocumentRef,
    DocumentRelationship,
    DocumentSource,
    DocumentVersionRef,
    NodeKind,
    ParseReport,
    ParseResult,
    SourceAnchor,
    StoryKind,
    canonical_document_ir_json,
    text_payload,
)

_DOCUMENT_ID = f"doc_{'1' * 32}"
_VERSION_ID = f"dver_{'2' * 32}"
_CONTENT_HASH = "3" * 64


def _node(
    suffix: str,
    *,
    parent: str | None = None,
    children: tuple[str, ...] = (),
    order: int = 0,
) -> DocumentNode:
    return DocumentNode(
        node_id=f"node_{suffix * 32}",
        kind=NodeKind.PARAGRAPH,
        parent_node_id=parent,
        child_ids=children,
        order=order,
        anchor=SourceAnchor(
            part_uri="/word/document.xml",
            story_kind=StoryKind.BODY,
            structural_path=("body", f"p:{suffix}"),
            ordinal=int(suffix, 16),
            source_start_char=0,
            source_end_char=3,
        ),
        text_payload=text_payload("abc"),
    )


def _ir(nodes: tuple[DocumentNode, ...], roots: tuple[str, ...]) -> DocumentIR:
    return DocumentIR(
        source=DocumentSource(
            document_id=_DOCUMENT_ID,
            document_version_id=_VERSION_ID,
            display_name="renamed.docx",
            media_type="application/docx",
            extension=".docx",
            content_sha256=_CONTENT_HASH,
            size_bytes=10,
            blob_ref="document:test",
        ),
        document=DocumentRef(
            project_id=f"prj_{'4' * 32}",
            knowledge_base_id=f"kb_{'5' * 32}",
            document_id=_DOCUMENT_ID,
            display_name="renamed.docx",
        ),
        version=DocumentVersionRef(
            document_id=_DOCUMENT_ID,
            document_version_id=_VERSION_ID,
            content_sha256=_CONTENT_HASH,
        ),
        root_node_ids=roots,
        nodes=nodes,
        parse_report=ParseReport(node_count=len(nodes)),
    )


def test_ir_round_trip_is_canonical_and_content_can_be_redacted() -> None:
    node = _node("a")
    document_ir = _ir((node,), (node.node_id,))

    rendered = canonical_document_ir_json(document_ir)
    restored = DocumentIR.model_validate_json(rendered)
    redacted = canonical_document_ir_json(document_ir, include_content=False)

    assert restored == document_ir
    assert json.loads(rendered)["schema_version"] == "1"
    assert "abc" not in redacted
    assert "renamed.docx" in rendered
    assert "PosixPath" not in rendered
    assert "binary_data" not in rendered


@pytest.mark.parametrize("broken", ["parent", "order", "cycle"])
def test_global_invariants_reject_invalid_graphs(broken: str) -> None:
    first_id = f"node_{'a' * 32}"
    second_id = f"node_{'b' * 32}"
    if broken == "parent":
        nodes = (_node("a", parent=f"node_{'f' * 32}"),)
        roots: tuple[str, ...] = ()
    elif broken == "order":
        nodes = (_node("a", order=0), _node("b", order=2))
        roots = (first_id, second_id)
    else:
        nodes = (
            _node("a", parent=second_id, children=(second_id,)),
            _node("b", parent=first_id, children=(first_id,)),
        )
        roots = ()
    with pytest.raises(ValidationError):
        _ir(nodes, roots)


def test_relationship_targets_and_text_hashes_fail_closed() -> None:
    node = _node("a")
    with pytest.raises(ValidationError):
        DocumentNode(
            **{
                **node.model_dump(),
                "text_payload": {
                    "exact_text": "changed",
                    "semantic_text": "abc",
                    "exact_sha256": node.text_payload.exact_sha256,
                    "semantic_sha256": node.text_payload.semantic_sha256,
                },
            }
        )
    base = _ir((node,), (node.node_id,))
    with pytest.raises(ValidationError):
        DocumentIR(
            **{
                **base.model_dump(),
                "relationships": (
                    DocumentRelationship(
                        relationship_id="missing",
                        relationship_type="reference",
                        source_node_id=node.node_id,
                        target_node_id=f"node_{'f' * 32}",
                    ),
                ),
            }
        )


def test_parse_report_rejects_impossible_coverage() -> None:
    with pytest.raises(ValidationError):
        ParseReport(
            visible_text_nodes=1,
            represented_visible_text_nodes=2,
        )


def test_p01_constructor_shape_migrates_with_read_compatibility() -> None:
    old_node = DocumentNode(
        node_id=f"node_{'a' * 32}",
        node_type="paragraph",
        structural_path=("body", "p:1"),
        text="abc",
        content_sha256=text_payload("abc").semantic_sha256,
    )
    old_ir = DocumentIR(
        document=DocumentRef(
            project_id=f"prj_{'4' * 32}",
            knowledge_base_id=f"kb_{'5' * 32}",
            document_id=_DOCUMENT_ID,
            display_name="legacy.docx",
        ),
        version=DocumentVersionRef(
            document_id=_DOCUMENT_ID,
            document_version_id=_VERSION_ID,
            content_sha256=_CONTENT_HASH,
        ),
        nodes=(old_node,),
    )
    report = ParseReport(node_count=1, warnings=("LEGACY_WARNING",))
    result = ParseResult(document_ir=old_ir, report=report)

    assert old_node.kind is NodeKind.PARAGRAPH
    assert old_node.node_type == "paragraph"
    assert old_node.text == "abc"
    assert old_ir.root_node_ids == (old_node.node_id,)
    assert old_ir.source.blob_ref == f"legacy-source:{_CONTENT_HASH}"
    assert result.document_ir.parse_report == report
