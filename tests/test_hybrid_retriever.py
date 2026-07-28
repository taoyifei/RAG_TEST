import uuid
from datetime import UTC, datetime

import httpx
from qdrant_client import QdrantClient

from rag_app.clients.model_services import (
    EmbeddingClientConfig,
    TeiEmbeddingClient,
)
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.contracts import Chunk, ElementKind, Locator
from rag_app.index import IndexedChunk, QdrantIndex
from rag_app.retrieval.bm25 import QdrantBm25Encoder
from rag_app.retrieval.filters import MetadataPolicy
from rag_app.retrieval.hybrid import (
    HybridRetrievalConfig,
    HybridRetrievalServices,
    HybridRetriever,
)
from rag_app.retrieval.rewrite import QueryVariants
from rag_app.retrieval.routing import SoftRouteDecision

_API_KEY = "test-only-qdrant-key"
_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64


class _RecordingRouter:
    def __init__(self) -> None:
        self.questions: list[str] = []

    def route(self, question: str) -> SoftRouteDecision:
        self.questions.append(question)
        return SoftRouteDecision(
            route_id=None,
            source_ids=(),
            confidence=0.0,
            routed=False,
        )


def _qdrant() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _embedding() -> TeiEmbeddingClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = httpx.Response(200, content=request.read()).json()
        vectors = []
        for index, text in enumerate(payload["input"]):
            vector = [0.0] * 1024
            vector[0 if "独立" in text else 1] = 1.0
            vectors.append({"index": index, "embedding": vector})
        return httpx.Response(
            200,
            json={
                "model": "Qwen3-Embedding-0.6B",
                "data": vectors,
            },
        )

    return TeiEmbeddingClient(
        ResilientHttpPool(
            ("http://embedding",),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            policy=ResiliencePolicy(
                max_attempts=1,
                failure_threshold=1,
                cooldown_seconds=30,
                max_concurrency=1,
            ),
        ),
        config=EmbeddingClientConfig(
            model="Qwen3-Embedding-0.6B",
            dimension=1024,
            max_batch_size=8,
            max_batch_chars=1000,
        ),
        api_token=None,
    )


def _chunk(
    position: int,
    text: str,
    dense_axis: int,
    bm25: QdrantBm25Encoder,
) -> IndexedChunk:
    digest_character = f"{position:x}"
    dense = [0.0] * 1024
    dense[dense_axis] = 1.0
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
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    return IndexedChunk(
        chunk=chunk,
        dense=dense,
        sparse=bm25.embed_document(text),
    )


def test_hybrid_retriever_keeps_original_and_rewritten_channels() -> None:
    client = _qdrant()
    collection = f"rag-hybrid-{uuid.uuid4().hex}"
    index = QdrantIndex(
        client,
        collection_name=collection,
        dense_dimension=1024,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    bm25 = QdrantBm25Encoder(
        tokenizer="multilingual",
        language="none",
    )
    chunks = [
        _chunk(1, "需求快验负责人是产品经理", 0, bm25),
        _chunk(2, "项目交付需要验收", 1, bm25),
    ]
    try:
        index.create_collection()
        index.stage_chunks(chunks)
        for item in chunks:
            index.activate_source_version(
                item.chunk.source_id,
                item.chunk.doc_version,
            )
        router = _RecordingRouter()
        retriever = HybridRetriever(
            HybridRetrievalServices(
                index=index,
                embedding=_embedding(),
                bm25=bm25,
                metadata_policy=MetadataPolicy(
                    allowed_statuses=("active",),
                    allowed_authority_levels=("official",),
                ),
                router=router,
            ),
            HybridRetrievalConfig(
                dense_limit=40,
                bm25_limit=40,
                rrf_rank_constant=60,
                candidate_limit=24,
                query_instruction="检索相关规范证据",
            ),
        )

        result = retriever.retrieve(
            QueryVariants(
                queries=(
                    "其中负责人是谁？",
                    "独立：需求快验负责人是谁？",
                ),
                resolved_query="独立：需求快验负责人是谁？",
                rewritten=True,
                call=None,
            ),
            as_of=datetime(2026, 7, 27, tzinfo=UTC),
        )

        assert {item.chunk_id for item in result.candidates} == {
            "chunk-1",
            "chunk-2",
        }
        assert result.query_count == 2
        assert result.embedding_calls == 1
        assert result.route_fallback is True
        assert router.questions == ["独立：需求快验负责人是谁？"]
        assert {
            channel
            for item in result.candidates
            for channel, _ in item.channel_ranks
        } == {
            "q0:dense",
            "q0:bm25",
            "q1:dense",
            "q1:bm25",
        }
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
