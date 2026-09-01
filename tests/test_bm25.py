import uuid

import pytest
from qdrant_client import QdrantClient

from rag_app.contracts import (
    Chunk,
    ChunkRole,
    ChunkSourceSpan,
    ElementKind,
    Locator,
)
from rag_app.index import IndexedChunk, QdrantIndex
from rag_app.retrieval.bm25 import QdrantBm25Encoder

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


def _indexed(
    text: str,
    position: int,
    encoder: QdrantBm25Encoder,
) -> IndexedChunk:
    digest_character = chr(ord("a") + position)
    locator = Locator(
        file_path="规范.docx",
        paragraph_index=position,
        segment_index=1,
        fragment=text,
    )
    chunk = Chunk(
        chunk_id=f"chunk_{position}",
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
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    return IndexedChunk(
        chunk=chunk,
        dense=[1.0] + [0.0] * 1023,
        sparse=encoder.embed_document(text),
    )


def test_real_qdrant_multilingual_bm25_handles_chinese_substring() -> None:
    client = _client()
    collection = f"rag-bm25-{uuid.uuid4().hex}"
    index = QdrantIndex(
        client,
        collection_name=collection,
        dense_dimension=1024,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    multilingual = QdrantBm25Encoder(
        tokenizer="multilingual",
        language="none",
    )
    word = QdrantBm25Encoder(tokenizer="word", language="none")
    try:
        index.create_collection()
        chunks = [
            _indexed("需求评审应在开发前完成", 1, multilingual),
            _indexed("项目交付需要验收", 2, multilingual),
            _indexed("快验流程包含结果确认", 3, multilingual),
        ]
        index.stage_chunks(chunks)
        for chunk in chunks:
            index.activate_source_version(
                chunk.chunk.source_id,
                chunk.chunk.doc_version,
            )

        multilingual_hits = index.query_sparse(
            multilingual.embed_query("需求评审"),
            limit=3,
        )
        word_hits = index.query_sparse(
            word.embed_query("需求评审"),
            limit=3,
        )

        assert multilingual_hits[0].payload["text"] == "需求评审应在开发前完成"
        assert word_hits == []
        assert multilingual.revision().startswith("sha256:")
        assert multilingual.revision() != word.revision()
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
