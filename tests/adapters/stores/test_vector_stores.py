from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.adapters.stores import (
    MemoryRevisionVectorStore,
    QdrantRevisionVectorStore,
)
from rag_app.core.errors import IndexCompatibilityError
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
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


def _spec(*, dual: bool = True) -> RevisionVectorSpec:
    project_id = deterministic_id("prj", "vector")
    knowledge_base_id = deterministic_id("kb", project_id, "vector")
    revision_id = deterministic_id("irev", knowledge_base_id, "vector")
    slots = [
        EmbeddingSlotIdentity(
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
    ]
    if dual:
        slots.append(
            EmbeddingSlotIdentity(
                slot_id="standby",
                role=EmbeddingSlotRole.STANDBY,
                provider_id="deterministic",
                model="deterministic-v2",
                vector_name="dense_standby",
                dimension=2,
                normalization="l2",
                document_request_policy=freeze_json_object(
                    {"role": "document"}
                ),
                query_request_policy=freeze_json_object({"role": "query"}),
            )
        )
    return RevisionVectorSpec(
        revision=IndexRevisionRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            index_revision_id=revision_id,
            index_fingerprint=canonical_sha256("vector-test"),
            state=IndexRevisionState.CREATED,
        ),
        physical_namespace=revision_id,
        slots=tuple(slots),
    )


def _point(
    spec: RevisionVectorSpec, suffix: str, vector: tuple[float, float]
) -> NamedVectorPoint:
    chunk_id = deterministic_id("chunk", suffix)
    document_id = deterministic_id("doc", suffix)
    return NamedVectorPoint(
        point_id=vector_point_id(spec.revision.index_revision_id, chunk_id),
        payload=VectorPointPayload(
            project_id=spec.revision.project_id,
            knowledge_base_id=spec.revision.knowledge_base_id,
            index_revision_id=spec.revision.index_revision_id,
            document_id=document_id,
            document_version_id=deterministic_id("dver", document_id, suffix),
            chunk_id=chunk_id,
            role="text",
            section_id=f"section-{suffix}",
            neighbor_group_id=f"group-{suffix}",
            content_sha256="1" * 64,
        ),
        vectors=tuple((slot.vector_name, vector) for slot in spec.slots),
    )


@pytest.mark.parametrize(
    "store_type", [MemoryRevisionVectorStore, QdrantRevisionVectorStore]
)
def test_named_vector_store_requires_exact_slot_and_stable_ties(
    store_type: type,
) -> None:
    store = store_type()
    spec = _spec()
    points = (_point(spec, "b", (1.0, 0.0)), _point(spec, "a", (1.0, 0.0)))
    store.create_revision(spec)
    store.upsert_complete_points(spec, points)

    hits = store.search_named(
        spec,
        slot_id="primary",
        vector_name="dense_primary",
        query_vector=(1.0, 0.0),
        limit=2,
    )

    assert {hit.point_id for hit in hits} == {
        point.point_id for point in points
    }
    if isinstance(store, MemoryRevisionVectorStore):
        assert [hit.point_id for hit in hits] == sorted(
            point.point_id for point in points
        )
    with pytest.raises(IndexCompatibilityError):
        store.search_named(
            spec,
            slot_id="primary",
            vector_name="dense_standby",
            query_vector=(1.0, 0.0),
            limit=1,
        )
    assert dict(store.validate_vector_revision(spec).vector_counts) == {
        "dense_primary": 2,
        "dense_standby": 2,
    }
    store.close()


def test_qdrant_local_path_reopens_complete_named_vectors(
    tmp_path: Path,
) -> None:
    spec = _spec()
    point = _point(spec, "persisted", (0.5, 0.5))
    store = QdrantRevisionVectorStore(tmp_path / "qdrant")
    try:
        store.create_revision(spec)
        store.upsert_complete_points(spec, (point,))
        store.upsert_complete_points(spec, (point,))
        fetched = store.fetch_points(spec, (point.point_id,))
        assert fetched[0].payload == point.payload
        assert set(fetched[0].vector_map()) == set(point.vector_map())
    finally:
        store.close()

    reopened = QdrantRevisionVectorStore(tmp_path / "qdrant")
    try:
        reopened.create_revision(spec)
        fetched = reopened.fetch_points(spec, (point.point_id,))
        assert fetched[0].payload == point.payload
        assert set(fetched[0].vector_map()) == {
            "dense_primary",
            "dense_standby",
        }
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "store_type", [MemoryRevisionVectorStore, QdrantRevisionVectorStore]
)
def test_complete_point_missing_required_slot_is_rejected(
    store_type: type,
) -> None:
    store = store_type()
    spec = _spec()
    point = _point(spec, "missing", (1.0, 0.0))
    incomplete = point.model_copy(
        update={"vectors": (("dense_primary", (1.0, 0.0)),)}
    )
    try:
        store.create_revision(spec)
        with pytest.raises(IndexCompatibilityError):
            store.upsert_complete_points(spec, (incomplete,))
    finally:
        store.close()
