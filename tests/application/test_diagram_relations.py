"""图关系候选、发布门、occurrence 与检索来源类型的离线合同。"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep

import pytest
from PIL import Image

from rag_app.core.errors import PolicyDenied
from rag_app.core.models import DocumentIR, SourceSpanKind
from rag_app.product.diagram_relations import (
    DiagramRelationCandidate,
    DiagramRelationNode,
    RelationDirection,
    RelationEvidenceSource,
    RelationReviewState,
    relation_candidate_id,
)
from tests.adapters.chunkers.test_docx_structural import _chunk
from tests.adapters.parsers.docx_fixtures import IMAGE, build_docx
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _uploaded_diagram(
    harness: ProductHarness,
) -> tuple[str, str, str, DocumentIR]:
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    stream = io.BytesIO()
    Image.new("RGB", (160, 80), color="white").save(stream, format="PNG")
    content = build_docx(
        "<w:p><w:r><w:t>图中的文字只有甲组与乙组。</w:t></w:r></w:p>"
        + IMAGE
        + IMAGE,
        relationships=(
            '<Relationship Id="rIdImage" Type="http://schemas.openxmlformats.'
            'org/officeDocument/2006/relationships/image" '
            'Target="media/relation.png"/>'
        ),
        extra_entries={"word/media/relation.png": stream.getvalue()},
    )
    job = harness.runtime.sdk.create_document(
        project_id,
        knowledge_base_id,
        display_name="公开图关系合同.docx",
        content=content,
        media_type=_MEDIA_TYPE,
        idempotency_key="diagram-relation-fixture",
    )
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            assert current.state.value == "succeeded", current
            assert current.document_id is not None
            with harness.runtime.connections.transaction() as connection:
                row = connection.execute(
                    "SELECT document_ir_json FROM revision_documents "
                    "WHERE revision_id=? AND document_id=?",
                    (current.revision_id, current.document_id),
                ).fetchone()
            assert row is not None
            return (
                project_id,
                knowledge_base_id,
                current.document_id,
                DocumentIR.model_validate_json(str(row[0])),
            )
        sleep(0.01)
    raise AssertionError("图关系合成文档上传超时")


def _candidate(  # noqa: PLR0913
    document: DocumentIR,
    *,
    occurrence_id: str,
    media_sha256: str,
    source: str = "甲组",
    target: str = "乙组",
    direction: RelationDirection = RelationDirection.SOURCE_TO_TARGET,
    ambiguous: bool = False,
) -> DiagramRelationCandidate:
    source_node = DiagramRelationNode(node_id="shape:a", label=source)
    target_node = DiagramRelationNode(node_id="shape:b", label=target)
    candidate_id = relation_candidate_id(
        document_version_id=document.version.document_version_id,
        figure_occurrence_id=occurrence_id,
        media_sha256=media_sha256,
        source_node=source_node,
        target_node=target_node,
        relation_type="reports_to",
        direction=direction,
        model_id="synthetic-relation-consumer-v1",
        policy_version="synthetic-contract-v1",
    )
    return DiagramRelationCandidate(
        candidate_id=candidate_id,
        knowledge_base_id=document.document.knowledge_base_id,
        document_id=document.document.document_id,
        document_version_id=document.version.document_version_id,
        figure_occurrence_id=occurrence_id,
        media_sha256=media_sha256,
        source_node=source_node,
        target_node=target_node,
        relation_type="reports_to",
        direction=direction,
        evidence_source=RelationEvidenceSource.SYNTHETIC_CONTRACT,
        model_id="synthetic-relation-consumer-v1",
        policy_version="synthetic-contract-v1",
        ambiguous=ambiguous,
        created_at=datetime.now(UTC),
    )


def test_only_accepted_relation_becomes_occurrence_bound_evidence(
    tmp_path: Path,
) -> None:
    """同一媒体的待审关系不入索引，接纳后也只绑定真实 occurrence。"""
    harness = build_product_harness(tmp_path)
    try:
        _, knowledge_base_id, document_id, document = _uploaded_diagram(harness)
        images = [item for item in document.nodes if item.image_attributes]
        assert len(images) == 2
        assert (
            images[0].image_attributes is not None
            and images[1].image_attributes is not None
        )
        assert (
            images[0].image_attributes.content_sha256
            == images[1].image_attributes.content_sha256
        )
        candidate = _candidate(
            document,
            occurrence_id=images[0].node_id,
            media_sha256=images[0].image_attributes.content_sha256,
        )
        harness.runtime.relations.upsert_candidates((candidate,))
        pending = harness.runtime.relations.enrich(document)
        assert not any(
            dict(item.metadata).get("origin") == "diagram_relation"
            for item in pending.nodes
        )

        identity_before = harness.runtime.relations.content_identity(
            knowledge_base_id
        )
        assert identity_before is None
        accepted = harness.runtime.relations.review(
            candidate.candidate_id,
            RelationReviewState.ACCEPTED,
            reviewer_id="synthetic-admin",
        )
        assert accepted.reviewer_sha256 is not None
        identity_after = harness.runtime.relations.content_identity(
            knowledge_base_id
        )
        assert identity_after is not None

        enriched = harness.runtime.relations.enrich(document)
        relation_nodes = [
            item
            for item in enriched.nodes
            if dict(item.metadata).get("origin") == "diagram_relation"
        ]
        assert len(relation_nodes) == 1
        assert relation_nodes[0].parent_node_id == images[0].node_id
        assert relation_nodes[0].parent_node_id != images[1].node_id
        assert relation_nodes[0].text == "甲组 --reports_to--> 乙组"
        chunks = _chunk(enriched)
        relation_spans = [
            span
            for chunk in chunks.chunks
            for span in chunk.source_spans
            if span.span_type is SourceSpanKind.DIAGRAM_RELATION
        ]
        assert len(relation_spans) == 1
        assert dict(relation_spans[0].metadata)["review_state"] == "accepted"
        assert chunks.report.source_span_coverage == 1.0

        page = harness.client.get(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/"
            f"{document_id}/diagram-relations"
        )
        assert page.status_code == 200
        assert page.json()[0]["candidate_id"] == candidate.candidate_id
    finally:
        harness.close()


def test_direction_change_changes_relation_evidence_without_ocr_inference(
    tmp_path: Path,
) -> None:
    """文字不变而真实方向改变时证据改变；未接纳名称不能硬推层级。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, _, document = _uploaded_diagram(harness)
        image = next(item for item in document.nodes if item.image_attributes)
        assert image.image_attributes is not None
        forward = _candidate(
            document,
            occurrence_id=image.node_id,
            media_sha256=image.image_attributes.content_sha256,
        )
        reverse = _candidate(
            document,
            occurrence_id=image.node_id,
            media_sha256=image.image_attributes.content_sha256,
            direction=RelationDirection.TARGET_TO_SOURCE,
        )
        harness.runtime.relations.upsert_candidates((forward, reverse))
        without_acceptance = harness.runtime.relations.enrich(document)
        assert "reports_to" not in " ".join(
            item.text for item in without_acceptance.nodes
        )

        harness.runtime.relations.review(
            forward.candidate_id,
            RelationReviewState.ACCEPTED,
            reviewer_id="synthetic-admin",
        )
        first = harness.runtime.relations.enrich(document)
        assert "甲组 --reports_to--> 乙组" in [
            item.text for item in first.nodes
        ]
        harness.runtime.relations.review(
            forward.candidate_id,
            RelationReviewState.REJECTED,
            reviewer_id="synthetic-admin",
        )
        harness.runtime.relations.review(
            reverse.candidate_id,
            RelationReviewState.ACCEPTED,
            reviewer_id="synthetic-admin",
        )
        second = harness.runtime.relations.enrich(document)
        assert "乙组 --reports_to--> 甲组" in [
            item.text for item in second.nodes
        ]
        assert "甲组 --reports_to--> 乙组" not in [
            item.text for item in second.nodes
        ]
    finally:
        harness.close()


def test_admin_review_builds_new_revision_without_mutating_old_index(
    tmp_path: Path,
) -> None:
    """正式接纳通过同源/CSRF API 触发新 Revision，旧活动索引不原地修改。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id, document_id, document = (
            _uploaded_diagram(harness)
        )
        image = next(item for item in document.nodes if item.image_attributes)
        assert image.image_attributes is not None
        candidate = _candidate(
            document,
            occurrence_id=image.node_id,
            media_sha256=image.image_attributes.content_sha256,
        )
        harness.runtime.relations.upsert_candidates((candidate,))
        before = harness.runtime.sdk.get_knowledge_base(
            project_id, knowledge_base_id
        )
        assert before.active_index_revision_id is not None
        with harness.runtime.connections.transaction() as connection:
            old_row = connection.execute(
                "SELECT document_ir_json FROM revision_documents "
                "WHERE revision_id=? AND document_id=?",
                (before.active_index_revision_id, document_id),
            ).fetchone()
        assert old_row is not None
        old_ir_json = str(old_row[0])
        response = harness.client.post(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/documents/"
            f"{document_id}/diagram-relations/{candidate.candidate_id}:review",
            json={"state": "accepted"},
            headers=harness.write_headers,
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["rebuild_job_id"]
        assert isinstance(job_id, str)
        deadline = monotonic() + 10
        while monotonic() < deadline:
            job = harness.runtime.sdk.get_job(job_id)
            if job.state.value not in {"queued", "running"}:
                assert job.state.value == "succeeded", job
                break
            sleep(0.01)
        else:
            raise AssertionError("图关系 Revision 重建超时")
        after = harness.runtime.sdk.get_knowledge_base(
            project_id, knowledge_base_id
        )
        assert after.active_index_revision_id != before.active_index_revision_id
        with harness.runtime.connections.transaction() as connection:
            preserved_old_row = connection.execute(
                "SELECT document_ir_json FROM revision_documents "
                "WHERE revision_id=? AND document_id=?",
                (before.active_index_revision_id, document_id),
            ).fetchone()
            row = connection.execute(
                "SELECT document_ir_json FROM revision_documents "
                "WHERE revision_id=? AND document_id=?",
                (after.active_index_revision_id, document_id),
            ).fetchone()
        assert preserved_old_row is not None
        assert str(preserved_old_row[0]) == old_ir_json
        assert row is not None
        rebuilt = DocumentIR.model_validate_json(str(row[0]))
        relation_nodes = [
            item
            for item in rebuilt.nodes
            if dict(item.metadata).get("origin") == "diagram_relation"
        ]
        assert [item.text for item in relation_nodes] == [
            "甲组 --reports_to--> 乙组"
        ]
    finally:
        harness.close()


@pytest.mark.parametrize(
    "direction,ambiguous",
    [
        (RelationDirection.UNKNOWN, False),
        (RelationDirection.SOURCE_TO_TARGET, True),
    ],
)
def test_unknown_or_ambiguous_relation_cannot_be_accepted(
    tmp_path: Path,
    direction: RelationDirection,
    ambiguous: bool,
) -> None:
    """断线、交叉、侧箭头或方向不明必须保持不可发布。"""
    harness = build_product_harness(tmp_path)
    try:
        _, _, _, document = _uploaded_diagram(harness)
        image = next(item for item in document.nodes if item.image_attributes)
        assert image.image_attributes is not None
        candidate = _candidate(
            document,
            occurrence_id=image.node_id,
            media_sha256=image.image_attributes.content_sha256,
            direction=direction,
            ambiguous=ambiguous,
        )
        harness.runtime.relations.upsert_candidates((candidate,))
        with pytest.raises(PolicyDenied):
            harness.runtime.relations.review(
                candidate.candidate_id,
                RelationReviewState.ACCEPTED,
                reviewer_id="synthetic-admin",
            )
    finally:
        harness.close()
