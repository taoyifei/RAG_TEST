"""P11 正式 Qdrant Server 双槽、审计与恢复验收。"""

from __future__ import annotations

import importlib.metadata
import os
import uuid

import pytest
from pydantic import ValidationError
from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.http.exceptions import UnexpectedResponse

from rag_app.adapters.stores import QdrantRevisionVectorStore
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

pytestmark = pytest.mark.local_integration

_DIMENSION = 1024
_EXPECTED_POINT_COUNT = 2
_DEFAULT_API_KEY = "test-only-qdrant-key"


def _client() -> QdrantClient:
    return QdrantClient(
        url=os.environ.get("RAG_QDRANT_URL", "http://127.0.0.1:6333"),
        api_key=os.environ.get("RAG_QDRANT_API_KEY", _DEFAULT_API_KEY),
        timeout=20,
        check_compatibility=False,
    )


def _spec(collection_name: str) -> RevisionVectorSpec:
    project_id = deterministic_id("prj", "p11-qdrant")
    knowledge_base_id = deterministic_id("kb", project_id, "p11-qdrant")
    revision_id = deterministic_id("irev", knowledge_base_id, "p11-qdrant")
    slots = tuple(
        EmbeddingSlotIdentity(
            slot_id=slot_id,
            role=role,
            provider_id=provider,
            model=model,
            vector_name=vector_name,
            dimension=_DIMENSION,
            normalization="l2-v1",
            document_request_policy=freeze_json_object(
                {"operation": "document"}
            ),
            query_request_policy=freeze_json_object({"operation": "query"}),
        )
        for slot_id, role, provider, model, vector_name in (
            (
                "primary",
                EmbeddingSlotRole.PRIMARY,
                "jina-embedding",
                "jina-embeddings-v5-text-small",
                "dense_primary",
            ),
            (
                "standby",
                EmbeddingSlotRole.STANDBY,
                "aliyun-qwen37-embedding",
                "qwen3.7-text-embedding",
                "dense_standby",
            ),
        )
    )
    return RevisionVectorSpec(
        revision=IndexRevisionRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            index_revision_id=revision_id,
            index_fingerprint=canonical_sha256("p11-qdrant"),
            state=IndexRevisionState.CREATED,
        ),
        physical_namespace=collection_name,
        slots=slots,
    )


def _unit_vector(index: int) -> tuple[float, ...]:
    values = [0.0] * _DIMENSION
    values[index] = 1.0
    return tuple(values)


def _point(
    spec: RevisionVectorSpec,
    suffix: str,
    *,
    primary_index: int,
    standby_index: int,
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
            content_sha256=canonical_sha256(suffix).removeprefix("sha256:"),
        ),
        vectors=(
            ("dense_primary", _unit_vector(primary_index)),
            ("dense_standby", _unit_vector(standby_index)),
        ),
    )


def _raw_vectors(point: NamedVectorPoint) -> dict[str, list[float]]:
    return {name: list(vector) for name, vector in point.vectors}


def _delete(client: QdrantClient, collection: str, point_id: str) -> None:
    client.delete(
        collection_name=collection,
        points_selector=models.PointIdsList(points=[point_id]),
        wait=True,
    )


def test_server_dual_vectors_inventory_corruption_and_snapshot(  # noqa: PLR0915
) -> None:
    client = _client()
    collection = f"p11_server_{uuid.uuid4().hex}"
    restored_collection = f"p11_restored_{uuid.uuid4().hex}"
    spec = _spec(collection)
    store = QdrantRevisionVectorStore(client=client)
    first = _point(spec, "first", primary_index=0, standby_index=1)
    second = _point(spec, "second", primary_index=1, standby_index=0)
    try:
        server_version = client.info().version
        client_version = importlib.metadata.version("qdrant-client")
        assert server_version == "1.18.3"
        assert client_version == "1.18.0"

        store.create_revision(spec)
        store.upsert_complete_points(spec, (first, second))
        inventory = store.audit_revision(spec)
        assert inventory.raw_record_count == _EXPECTED_POINT_COUNT
        assert inventory.invalid_record_count == 0
        assert dict(store.validate_vector_revision(spec).vector_counts) == {
            "dense_primary": _EXPECTED_POINT_COUNT,
            "dense_standby": _EXPECTED_POINT_COUNT,
        }

        primary = store.search_named(
            spec,
            slot_id="primary",
            vector_name="dense_primary",
            query_vector=_unit_vector(0),
            limit=1,
        )
        standby = store.search_named(
            spec,
            slot_id="standby",
            vector_name="dense_standby",
            query_vector=_unit_vector(0),
            limit=1,
        )
        assert primary[0].point_id == first.point_id
        assert standby[0].point_id == second.point_id

        outsider = second.payload.model_copy(
            update={"project_id": deterministic_id("prj", "outside")}
        )
        outsider_id = str(uuid.uuid4())
        client.upsert(
            collection,
            points=[
                models.PointStruct(
                    id=outsider_id,
                    vector=_raw_vectors(second),
                    payload=outsider.model_dump(mode="json"),
                )
            ],
            wait=True,
        )
        scoped = store.search_named(
            spec,
            slot_id="primary",
            vector_name="dense_primary",
            query_vector=_unit_vector(1),
            limit=10,
        )
        assert outsider_id not in {hit.point_id for hit in scoped}
        _delete(client, collection, outsider_id)

        _delete(client, collection, second.point_id)
        assert store.audit_revision(spec).raw_record_count == 1
        store.upsert_complete_points(spec, (second,))

        extra = _point(spec, "extra", primary_index=0, standby_index=1)
        store.upsert_complete_points(spec, (extra,))
        assert (
            store.audit_revision(spec).raw_record_count
            != _EXPECTED_POINT_COUNT
        )
        _delete(client, collection, extra.point_id)

        wrong_id = str(uuid.uuid4())
        client.upsert(
            collection,
            points=[
                models.PointStruct(
                    id=wrong_id,
                    vector=_raw_vectors(first),
                    payload=first.payload.model_dump(mode="json"),
                )
            ],
            wait=True,
        )
        wrong_inventory = store.audit_revision(spec)
        assert wrong_inventory.invalid_record_count == 1
        assert "POINT_ID_CHUNK_MISMATCH" in {
            item.reason_code for item in wrong_inventory.points
        }
        with pytest.raises(IndexCompatibilityError, match="Point ID"):
            store.search_named(
                spec,
                slot_id="primary",
                vector_name="dense_primary",
                query_vector=_unit_vector(0),
                limit=10,
            )
        _delete(client, collection, wrong_id)

        incomplete_point = _point(
            spec,
            "incomplete",
            primary_index=0,
            standby_index=1,
        )
        incomplete_id = incomplete_point.point_id
        client.upsert(
            collection,
            points=[
                models.PointStruct(
                    id=incomplete_id,
                    vector={"dense_primary": list(_unit_vector(0))},
                    payload=incomplete_point.payload.model_dump(mode="json"),
                )
            ],
            wait=True,
        )
        incomplete = store.audit_revision(spec)
        assert incomplete.invalid_record_count == 1
        assert "VECTOR_NAMES_MISMATCH" in {
            item.reason_code for item in incomplete.points
        }
        _delete(client, collection, incomplete_id)

        bad_payload_id = str(uuid.uuid4())
        client.upsert(
            collection,
            points=[
                models.PointStruct(
                    id=bad_payload_id,
                    vector=_raw_vectors(first),
                    payload={"project_id": spec.revision.project_id},
                )
            ],
            wait=True,
        )
        bad_payload = store.audit_revision(spec)
        assert bad_payload.invalid_record_count == 1
        assert "PAYLOAD_INVALID" in {
            item.reason_code for item in bad_payload.points
        }
        _delete(client, collection, bad_payload_id)

        with pytest.raises(UnexpectedResponse):
            client.upsert(
                collection,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector={
                            "dense_primary": [1.0, 0.0],
                            "dense_standby": list(_unit_vector(0)),
                        },
                        payload=first.payload.model_dump(mode="json"),
                    )
                ],
                wait=True,
            )
        with pytest.raises(ValidationError):
            models.PointStruct.model_validate(
                {
                    "id": str(uuid.uuid4()),
                    "vector": {"dense_primary": ["非法数值"]},
                    "payload": {},
                }
            )

        snapshot = client.create_snapshot(collection, wait=True)
        assert snapshot is not None
        assert snapshot.checksum is not None
        recovered = client.recover_snapshot(
            collection_name=restored_collection,
            location=(
                f"file:///qdrant/snapshots/{collection}/{snapshot.name}"
            ),
            checksum=snapshot.checksum,
            priority=models.SnapshotPriority.SNAPSHOT,
            wait=True,
        )
        assert recovered is True
        restored_spec = spec.model_copy(
            update={"physical_namespace": restored_collection}
        )
        store.create_revision(restored_spec)
        restored = store.audit_revision(restored_spec)
        assert restored.raw_record_count == _EXPECTED_POINT_COUNT
        assert restored.invalid_record_count == 0
    finally:
        for name in (restored_collection, collection):
            if client.collection_exists(name):
                client.delete_collection(name)
        store.close()
