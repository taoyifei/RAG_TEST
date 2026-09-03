from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rag_app.adapters.stores import (
    MemoryRevisionVectorStore,
    QdrantRevisionVectorStore,
)
from rag_app.application.revision_builder import IngestionDocument
from rag_app.core.errors import IndexCorrupt, ValidationFailed
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    DocumentRef,
    EmbeddingSlotIdentity,
    EmbeddingSlotRole,
    IndexRevisionRef,
    IndexRevisionState,
    NamedVectorPoint,
    RevisionVectorSpec,
    VectorPointPayload,
    vector_point_id,
)
from rag_app.core.models.common import freeze_json_object
from tests.adapters.parsers.docx_fixtures import TABLE, build_docx
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _spec() -> RevisionVectorSpec:
    project_id = deterministic_id("prj", "inventory")
    knowledge_base_id = deterministic_id("kb", project_id, "inventory")
    revision_id = deterministic_id("irev", knowledge_base_id, "inventory")
    slot = EmbeddingSlotIdentity(
        slot_id="primary",
        role=EmbeddingSlotRole.PRIMARY,
        provider_id="deterministic",
        model="deterministic-v1",
        vector_name="dense_primary",
        dimension=2,
        normalization="l2",
        document_request_policy=freeze_json_object({"role": "document"}),
        query_request_policy=freeze_json_object({"role": "query"}),
    )
    return RevisionVectorSpec(
        revision=IndexRevisionRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            index_revision_id=revision_id,
            index_fingerprint=canonical_sha256("inventory"),
            state=IndexRevisionState.CREATED,
        ),
        physical_namespace=revision_id,
        slots=(slot,),
    )


def _point(spec: RevisionVectorSpec) -> NamedVectorPoint:
    document_id = deterministic_id("doc", "inventory")
    chunk_id = deterministic_id("chunk", "inventory")
    return NamedVectorPoint(
        point_id=vector_point_id(spec.revision.index_revision_id, chunk_id),
        payload=VectorPointPayload(
            project_id=spec.revision.project_id,
            knowledge_base_id=spec.revision.knowledge_base_id,
            index_revision_id=spec.revision.index_revision_id,
            document_id=document_id,
            document_version_id=deterministic_id(
                "dver", document_id, "inventory"
            ),
            chunk_id=chunk_id,
            role="text",
            section_id="section-inventory",
            neighbor_group_id="group-inventory",
            content_sha256="1" * 64,
        ),
        vectors=(("dense_primary", (1.0, 0.0)),),
    )


@pytest.mark.parametrize(
    "store_type", [MemoryRevisionVectorStore, QdrantRevisionVectorStore]
)
def test_inventory_reports_every_valid_point(store_type: type[Any]) -> None:
    store = store_type()
    spec = _spec()
    point = _point(spec)
    try:
        store.create_revision(spec)
        store.upsert_complete_points(spec, (point,))

        inventory = store.audit_revision(spec)

        assert inventory.raw_record_count == 1
        assert inventory.converted_record_count == 1
        assert inventory.invalid_record_count == 0
        assert inventory.points[0].point_id == point.point_id
        assert inventory.points[0].vector_dimensions == (
            ("dense_primary", 2),
        )
    finally:
        store.close()


def test_qdrant_malformed_raw_record_is_not_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = QdrantRevisionVectorStore()
    spec = _spec()
    try:
        store.create_revision(spec)
        malformed = SimpleNamespace(
            id="not-a-uuid",
            payload={"source_text": "must not enter audit"},
            vector={"dense_primary": [1.0, "bad-number"]},
        )

        def fake_scroll(**kwargs: object) -> tuple[list[object], None]:
            del kwargs
            return [malformed], None

        monkeypatch.setattr(store._client, "scroll", fake_scroll)
        inventory = store.audit_revision(spec)

        assert inventory.raw_record_count == 1
        assert inventory.converted_record_count == 0
        assert inventory.invalid_record_count == 1
        assert inventory.points[0].reason_code == "PAYLOAD_INVALID"
        assert "must not enter audit" not in inventory.model_dump_json()
    finally:
        store.close()


def test_qdrant_repeated_scroll_offset_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = QdrantRevisionVectorStore()
    spec = _spec()
    point = _point(spec)
    raw = SimpleNamespace(
        id=point.point_id,
        payload=point.payload.model_dump(mode="json"),
        vector={"dense_primary": [1.0, 0.0]},
    )
    try:
        store.create_revision(spec)

        def fake_scroll(**kwargs: object) -> tuple[list[object], str]:
            del kwargs
            return [raw], point.point_id

        monkeypatch.setattr(store._client, "scroll", fake_scroll)
        with pytest.raises(IndexCorrupt, match="offset 重复"):
            store.audit_revision(spec)
    finally:
        store.close()


def test_revision_validator_rejects_replacement_with_duplicate_chunk(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    document = IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id("doc", "inventory-runtime"),
            display_name="inventory.docx",
        ),
        content=build_docx(TABLE + TABLE),
        media_type=_MEDIA_TYPE,
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(document,),
            idempotency_key="inventory-runtime",
            budgets=runtime.default_budgets(),
        )
        spec = runtime.control.revision_vector_spec(result.revision_id)
        chunks = runtime.control.chunk_rows(result.revision_id)
        store = runtime.components.vector_store
        assert isinstance(store, MemoryRevisionVectorStore)
        assert len(chunks) >= 2
        namespace = store._points[spec.physical_namespace]
        missing_id = vector_point_id(result.revision_id, chunks[0].chunk_id)
        duplicate = namespace[
            vector_point_id(result.revision_id, chunks[1].chunk_id)
        ]
        namespace.pop(missing_id)
        namespace[missing_id] = duplicate.model_copy(
            update={"point_id": missing_id}
        )

        with pytest.raises(ValidationFailed, match="全量一致"):
            runtime.validator.validate(
                spec,
                current_index_fingerprint=runtime.components.index_fingerprint,
            )
    finally:
        runtime.close()
