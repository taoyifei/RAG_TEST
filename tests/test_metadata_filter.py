import uuid
from datetime import UTC, datetime

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import (
    Chunk,
    ChunkRole,
    ChunkSourceSpan,
    ElementKind,
    Locator,
)
from rag_app.index import IndexedChunk, QdrantIndex
from rag_app.retrieval.filters import MetadataPolicy

pytestmark = pytest.mark.local_integration

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
    effective_from: str | None = None,
    effective_to: str | None = None,
) -> IndexedChunk:
    digest_character = f"{position:x}"
    text = f"证据-{position}"
    locator = Locator(
        file_path=f"{position}.docx",
        paragraph_index=1,
        segment_index=1,
        fragment=text,
    )
    chunk = Chunk(
        chunk_id=f"chunk-{position}",
        source_id=f"src_{position:032x}",
        doc_version="sha256:" + digest_character * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        section_id=f"section_{position:032x}",
        neighbor_group_id=f"group_{position:032x}",
        chunk_role=ChunkRole.TEXT,
        source_spans=(
            ChunkSourceSpan(
                element_id=f"element-{position}",
                locator=locator,
                start_char=0,
                end_char=len(text),
                source_start_char=0,
                source_end_char=len(text),
            ),
        ),
        text=text,
        embedding_text=text,
        element_kind=ElementKind.PARAGRAPH,
        locators=(locator,),
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
            status="active",
            authority="official",
            effective_from="2026-01-01T00:00:00Z",
            effective_to="2026-12-31T00:00:00Z",
        ),
        _chunk(
            2,
            status="active",
            authority="official",
            effective_to="2025-12-31T00:00:00Z",
        ),
        _chunk(3, status="draft", authority="official"),
        _chunk(4, status="active", authority="unverified"),
        _chunk(5, status="active", authority="official"),
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
            allowed_statuses=("active",),
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
