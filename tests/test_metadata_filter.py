import uuid
from datetime import UTC, datetime

from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import Chunk, ElementKind, Locator
from rag_app.index import IndexedChunk, QdrantIndex
from rag_app.retrieval.filters import MetadataPolicy

_API_KEY = "test-only-qdrant-key"
_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _chunk(
    position: int,
    *,
    status: str,
    authority: str,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> IndexedChunk:
    digest_character = f"{position:x}"
    text = f"证据-{position}"
    chunk = Chunk(
        chunk_id=f"chunk-{position}",
        source_id=f"src_{position:032x}",
        doc_version="sha256:" + digest_character * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        text=text,
        embedding_text=text,
        element_kind=ElementKind.PARAGRAPH,
        locators=(
            Locator(
                file_path=f"{position}.docx",
                paragraph_index=1,
                fragment=text,
            ),
        ),
        content_sha256=digest_character * 64,
        document_status=status,
        authority_level=authority,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    return IndexedChunk(
        chunk=chunk,
        dense=[1.0] + [0.0] * 1023,
        sparse=models.SparseVector(indices=[position], values=[1.0]),
    )


def test_real_qdrant_metadata_filter_excludes_invalid_evidence() -> None:
    client = _client()
    collection = f"rag-filter-{uuid.uuid4().hex}"
    index = QdrantIndex(
        client,
        collection_name=collection,
        dense_dimension=1024,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    as_of = datetime(2026, 7, 27, tzinfo=UTC)
    chunks = [
        _chunk(
            1,
            status="published",
            authority="official",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            effective_to=datetime(2026, 12, 31, tzinfo=UTC),
        ),
        _chunk(
            2,
            status="published",
            authority="official",
            effective_to=datetime(2025, 12, 31, tzinfo=UTC),
        ),
        _chunk(3, status="draft", authority="official"),
        _chunk(4, status="published", authority="unverified"),
        _chunk(5, status="published", authority="official"),
    ]
    try:
        index.create_collection()
        index.stage_chunks(chunks)
        for item in chunks:
            index.activate_source_version(
                item.chunk.source_id,
                item.chunk.doc_version,
            )
        policy = MetadataPolicy(
            allowed_statuses=("published",),
            allowed_authority_levels=("official",),
        )

        hits = index.query_dense(
            [1.0] + [0.0] * 1023,
            limit=10,
            additional_filter=policy.to_qdrant_filter(as_of=as_of),
        )

        assert {hit.payload["text"] for hit in hits} == {
            "证据-1",
            "证据-5",
        }
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
