"""重排后按需相邻块扩展。"""

from __future__ import annotations

import uuid

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
from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.neighbors import NeighborExpander
from rag_app.retrieval.rerank import RerankedHit
from rag_app.tracing.reasons import DecisionCode

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
    *,
    group_id: str = "group_" + "b" * 32,
) -> IndexedChunk:
    locator = Locator(
        file_path="规范.docx",
        paragraph_index=1,
        segment_index=1,
        fragment=text,
    )
    chunk = Chunk(
        chunk_id=chunk_id,
        source_id=_SOURCE,
        doc_version=_VERSION,
        pipeline_fingerprint=_PIPELINE,
        section_id="section_" + "a" * 32,
        neighbor_group_id=group_id,
        chunk_role=ChunkRole.TEXT,
        source_spans=(
            ChunkSourceSpan(
                element_id=f"element-{chunk_id}",
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
                    "neighbor_group_id": "group_" + "b" * 32,
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


def test_neighbor_expander_rejects_missing_group_identity() -> None:
    class _PayloadReader:
        def fetch_active_payloads(
            self,
            chunk_ids: tuple[str, ...],
        ) -> dict[str, dict[str, object]]:
            return {
                chunk_ids[0]: {
                    "chunk_id": chunk_ids[0],
                    "source_id": _SOURCE,
                    "doc_version": _VERSION,
                    "neighbor_group_id": "group_" + "b" * 32,
                }
            }

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
                "next_chunk_id": "chunk_next",
            },
        ),
    )

    try:
        NeighborExpander(_PayloadReader(), max_items=2).expand((seed,))
    except ValueError as error:
        assert "neighbor_group_id" in str(error)
    else:
        raise AssertionError("相邻种子缺少 neighbor_group_id 必须失败关闭。")


def test_neighbor_expander_does_not_cross_neighbor_group() -> None:
    class _PayloadReader:
        def fetch_active_payloads(
            self,
            chunk_ids: tuple[str, ...],
        ) -> dict[str, dict[str, object]]:
            return {
                chunk_ids[0]: {
                    "chunk_id": chunk_ids[0],
                    "source_id": _SOURCE,
                    "doc_version": _VERSION,
                    "neighbor_group_id": "group_" + "d" * 32,
                }
            }

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
                "neighbor_group_id": "group_" + "b" * 32,
                "next_chunk_id": "chunk_next",
            },
        ),
    )

    result = NeighborExpander(
        _PayloadReader(),
        max_items=2,
    ).expand_with_trace(
        (seed,),
    )

    assert result.hits == (seed,)
    assert result.decisions[0].reason_code is (
        DecisionCode.NEIGHBOR_GROUP_MISMATCH
    )


@pytest.mark.parametrize(
    ("neighbor_overrides", "expected"),
    (
        ({"source_id": "src_" + "d" * 32}, DecisionCode.SOURCE_MISMATCH),
        ({"doc_version": "sha256:" + "e" * 64}, DecisionCode.VERSION_MISMATCH),
    ),
)
def test_neighbor_trace_distinguishes_identity_mismatch(
    neighbor_overrides: dict[str, object],
    expected: DecisionCode,
) -> None:
    neighbor_payload: dict[str, object] = {
        "chunk_id": "chunk_next",
        "source_id": _SOURCE,
        "doc_version": _VERSION,
        "neighbor_group_id": "group_" + "b" * 32,
        **neighbor_overrides,
    }

    class _PayloadReader:
        def fetch_active_payloads(
            self,
            chunk_ids: tuple[str, ...],
        ) -> dict[str, dict[str, object]]:
            return {chunk_ids[0]: neighbor_payload}

    result = NeighborExpander(
        _PayloadReader(),
        max_items=2,
    ).expand_with_trace((_neighbor_seed("chunk_next"),))

    assert result.decisions[0].reason_code is expected


def test_neighbor_trace_records_missing_duplicate_accept_and_capacity() -> None:
    class _PayloadReader:
        def fetch_active_payloads(
            self,
            chunk_ids: tuple[str, ...],
        ) -> dict[str, dict[str, object]]:
            del chunk_ids
            return {
                "chunk_previous": {
                    "chunk_id": "chunk_previous",
                    "source_id": _SOURCE,
                    "doc_version": _VERSION,
                    "neighbor_group_id": "group_" + "b" * 32,
                }
            }

    accepted_and_capacity = NeighborExpander(
        _PayloadReader(),
        max_items=2,
    ).expand_with_trace(
        (
            _neighbor_seed(
                "chunk_next",
                previous="chunk_previous",
            ),
        )
    )
    missing = NeighborExpander(
        _PayloadReader(),
        max_items=3,
    ).expand_with_trace((_neighbor_seed("chunk_missing"),))
    duplicate = NeighborExpander(
        _PayloadReader(),
        max_items=3,
    ).expand_with_trace((_neighbor_seed("chunk_seed"),))

    assert [item.reason_code for item in accepted_and_capacity.decisions] == [
        DecisionCode.ACCEPTED,
        DecisionCode.CAPACITY_LIMIT,
    ]
    assert missing.decisions[0].reason_code is DecisionCode.MISSING_PAYLOAD
    assert duplicate.decisions[0].reason_code is DecisionCode.DUPLICATE


def _neighbor_seed(
    next_chunk: str,
    *,
    previous: str | None = None,
) -> RerankedHit:
    return RerankedHit(
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
                "neighbor_group_id": "group_" + "b" * 32,
                "previous_chunk_id": previous,
                "next_chunk_id": next_chunk,
            },
        ),
    )
