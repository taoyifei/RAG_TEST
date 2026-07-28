"""重排后按需相邻块扩展。"""

from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import Chunk, ElementKind, Locator
from rag_app.index import IndexedChunk, QdrantIndex
from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.neighbors import NeighborExpander
from rag_app.retrieval.rerank import RerankedHit

_API_KEY = "test-only-qdrant-key"
_PIPELINE = f"sha256:{'a' * 64}"
_SOURCE = f"src_{'b' * 32}"
_VERSION = f"sha256:{'c' * 64}"


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _indexed(
    chunk_id: str,
    text: str,
    previous: str | None,
    next_: str | None,
) -> IndexedChunk:
    chunk = Chunk(
        chunk_id=chunk_id,
        source_id=_SOURCE,
        doc_version=_VERSION,
        pipeline_fingerprint=_PIPELINE,
        text=text,
        embedding_text=text,
        element_kind=ElementKind.PARAGRAPH,
        locators=(
            Locator(
                file_path="规范.docx",
                paragraph_index=1,
                fragment=text,
            ),
        ),
        content_sha256="d" * 64,
        previous_chunk_id=previous,
        next_chunk_id=next_,
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    return IndexedChunk(
        chunk=chunk,
        dense=[1.0, 0.0, 0.0],
        sparse=models.SparseVector(indices=[1], values=[1.0]),
    )


def test_real_qdrant_expands_only_active_same_version_neighbors() -> None:
    client = _client()
    collection = f"rag-neighbors-{uuid.uuid4().hex}"
    index = QdrantIndex(
        client,
        collection_name=collection,
        dense_dimension=3,
        pipeline_fingerprint=_PIPELINE,
    )
    try:
        index.create_collection()
        chunks = (
            _indexed("chunk_previous", "前文", None, "chunk_seed"),
            _indexed(
                "chunk_seed",
                "命中",
                "chunk_previous",
                "chunk_next",
            ),
            _indexed("chunk_next", "后文", "chunk_seed", None),
        )
        index.stage_chunks(chunks)
        index.activate_source_version(_SOURCE, _VERSION)
        seed = RerankedHit(
            rank=1,
            rerank_score=0.9,
            hit=FusedHit(
                chunk_id="chunk_seed",
                rrf_score=0.1,
                channel_ranks=(("dense", 1),),
                payload={
                    "chunk_id": "chunk_seed",
                    "source_id": _SOURCE,
                    "doc_version": _VERSION,
                    "previous_chunk_id": "chunk_previous",
                    "next_chunk_id": "chunk_next",
                },
            ),
        )

        expanded = NeighborExpander(index, max_items=3).expand((seed,))

        assert [item.hit.chunk_id for item in expanded] == [
            "chunk_seed",
            "chunk_previous",
            "chunk_next",
        ]
        assert [item.rank for item in expanded] == [1, 2, 3]
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
