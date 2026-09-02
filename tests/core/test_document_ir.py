from __future__ import annotations

import hashlib
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
    ParseContext,
    ParsedArtifact,
    ParseReport,
    ParseResult,
    SourceAnchor,
    StoryKind,
    canonical_document_ir_json,
    text_payload,
    validate_document_ref_uniqueness,
)
from rag_app.core.policies import ParsingPolicy

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
    assert "renamed.docx" not in redacted
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


def test_parse_context_is_separate_from_semantic_parsing_policy() -> None:
    policy = ParsingPolicy(tracked_changes="all_with_markers")
    first = ParseContext(
        document=DocumentRef(
            project_id=f"prj_{'4' * 32}",
            knowledge_base_id=f"kb_{'5' * 32}",
            document_id=_DOCUMENT_ID,
            display_name="first.docx",
        )
    )
    second = ParseContext(
        document=first.document.model_copy(
            update={
                "project_id": f"prj_{'6' * 32}",
                "knowledge_base_id": f"kb_{'7' * 32}",
                "document_id": f"doc_{'8' * 32}",
            }
        )
    )

    assert policy == ParsingPolicy(tracked_changes="all_with_markers")
    assert first != second
    with pytest.raises(ValidationError):
        ParsingPolicy.model_validate(
            {"metadata": {"document_id": _DOCUMENT_ID}}
        )


def test_document_id_is_globally_unique_across_project_and_kb() -> None:
    original = DocumentRef(
        project_id=f"prj_{'4' * 32}",
        knowledge_base_id=f"kb_{'5' * 32}",
        document_id=_DOCUMENT_ID,
        display_name="first.docx",
    )
    same_scope = original.model_copy(update={"display_name": "renamed.docx"})
    validate_document_ref_uniqueness((original, same_scope))

    with pytest.raises(ValueError, match="全局唯一"):
        validate_document_ref_uniqueness(
            (
                original,
                original.model_copy(
                    update={"project_id": f"prj_{'6' * 32}"}
                ),
            )
        )


def test_parsed_artifact_binds_identity_to_content_hash() -> None:
    content = b"synthetic artifact"
    digest = hashlib.sha256(content).hexdigest()
    artifact = ParsedArtifact(
        artifact_id=f"sha256:{digest}",
        content_sha256=digest,
        media_type="application/octet-stream",
        content=content,
        role="source_document",
    )

    assert artifact.content == content
    with pytest.raises(ValidationError):
        ParsedArtifact.model_validate(
            {**artifact.model_dump(), "content": b"changed"}
        )


def test_redacted_ir_uses_metadata_allowlist() -> None:
    original = _node("a")
    node = DocumentNode.model_validate(
        {
            **original.model_dump(),
            "metadata": {
                "external_hyperlink_schemes": ["https"],
                "unknown_private_text": "do-not-keep",
            },
        }
    )
    payload = json.loads(
        canonical_document_ir_json(
            _ir((node,), (node.node_id,)),
            include_content=False,
        )
    )

    assert payload["nodes"][0]["metadata"] == {
        "external_hyperlink_schemes": ["https"]
    }
    assert "do-not-keep" not in json.dumps(payload)


def test_root_order_and_relationship_ids_fail_closed() -> None:
    first = _node("a", order=0)
    second = _node("b", order=1)
    with pytest.raises(ValidationError, match="root_node_ids"):
        _ir((first, second), (second.node_id, first.node_id))

    base = _ir((first, second), (first.node_id, second.node_id))
    relationship = DocumentRelationship(
        relationship_id="same-id",
        relationship_type="reference",
        source_node_id=first.node_id,
        target_node_id=second.node_id,
    )
    with pytest.raises(ValidationError, match="relationship ID"):
        DocumentIR.model_validate(
            {
                **base.model_dump(),
                "relationships": (relationship, relationship),
            }
        )
