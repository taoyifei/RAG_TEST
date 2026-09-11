from __future__ import annotations

from pathlib import Path

import pytest
from qdrant_client.http import models

import rag_app.adapters.stores.qdrant_vector as qdrant_vector_module
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


def test_qdrant_upsert_splits_requests_by_serialized_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整 Point 总体过大时分批，且每批保持全部 named vectors。"""
    spec = _spec()
    points = tuple(
        _point(spec, suffix, (0.5, 0.5)) for suffix in ("a", "b", "c")
    )
    first = points[0]
    qdrant_point = models.PointStruct(
        id=first.point_id,
        vector={
            name: list(vector) for name, vector in first.vector_map().items()
        },
        payload=first.payload.model_dump(mode="json"),
    )
    max_bytes = len(
        models.PointsList(points=[qdrant_point])
        .model_dump_json(exclude_none=True)
        .encode("utf-8")
    )
    monkeypatch.setattr(
        qdrant_vector_module,
        "_MAX_UPSERT_BATCH_BYTES",
        max_bytes,
    )
    batches: list[tuple[models.PointStruct, ...]] = []
    store = QdrantRevisionVectorStore()
    store.create_revision(spec)

    def record_upsert(
        *,
        collection_name: str,
        points: list[models.PointStruct],
        wait: bool,
    ) -> None:
        assert collection_name == spec.physical_namespace
        assert wait is True
        batches.append(tuple(points))

    monkeypatch.setattr(store._client, "upsert", record_upsert)
    try:
        store.upsert_complete_points(spec, points)
    finally:
        store.close()

    assert [len(batch) for batch in batches] == [1, 1, 1]
    assert all(
        set(point.vector) == {"dense_primary", "dense_standby"}
        for batch in batches
        for point in batch
    )
    assert all(
        len(
            models.PointsList(points=list(batch))
            .model_dump_json(exclude_none=True)
            .encode("utf-8")
        )
        <= max_bytes
        for batch in batches
    )


@pytest.mark.parametrize(
    "store_type", [MemoryRevisionVectorStore, QdrantRevisionVectorStore]
)
def test_deleted_document_is_filtered_before_vector_limit(
    store_type: type,
) -> None:
    """高分已删除文档不能占满候选窗口并挤掉活动文档。"""
    store = store_type()
    spec = _spec()
    deleted = _point(spec, "deleted", (1.0, 0.0))
    active = _point(spec, "active", (0.5, 0.5))
    try:
        store.create_revision(spec)
        store.upsert_complete_points(spec, (deleted, active))
        hits = store.search_named(
            spec,
            slot_id="primary",
            vector_name="dense_primary",
            query_vector=(1.0, 0.0),
            limit=1,
            excluded_document_ids=(deleted.payload.document_id,),
        )
        assert len(hits) == 1
        assert hits[0].document_id == active.payload.document_id
        assert hits[0].rank == 1
    finally:
        store.close()


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
