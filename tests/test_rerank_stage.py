import httpx
import pytest

from rag_app.clients.model_services import RerankerClient
from rag_app.clients.resilience import ResiliencePolicy, ResilientHttpPool
from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.rerank import RerankConfig, RerankStage


def _hit(chunk_id: str, score: float) -> FusedHit:
    return FusedHit(
        chunk_id=chunk_id,
        rrf_score=score,
        channel_ranks=(("q0:dense", 1),),
        payload={
            "chunk_id": chunk_id,
            "text": f"原文-{chunk_id}",
            "embedding_text": f"标题\n原文-{chunk_id}",
        },
    )


def test_rerank_stage_sorts_by_model_then_rrf() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "score": 0.5},
                    {"index": 1, "score": 0.9},
                    {"index": 2, "score": 0.5},
                ]
            },
        )

    client = RerankerClient(
        ResilientHttpPool(
            ("http://reranker",),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            policy=ResiliencePolicy(
                max_attempts=1,
                failure_threshold=1,
                cooldown_seconds=30,
                max_concurrency=1,
            ),
        ),
        api_token=None,
    )
    result = RerankStage(
        client,
        RerankConfig(candidate_limit=24, final_limit=3, max_final_limit=8),
    ).rerank(
        "问题",
        (
            _hit("chunk-a", 0.2),
            _hit("chunk-b", 0.1),
            _hit("chunk-c", 0.3),
        ),
    )

    assert [item.hit.chunk_id for item in result.hits] == [
        "chunk-b",
        "chunk-c",
        "chunk-a",
    ]
    assert result.call_count == 1


def test_rerank_stage_rejects_missing_embedding_text() -> None:
    invalid = _hit("chunk-a", 0.1)
    invalid.payload.pop("embedding_text")

    with pytest.raises(ValueError, match="embedding_text"):
        RerankStage(
            object(),
            RerankConfig(
                candidate_limit=24,
                final_limit=6,
                max_final_limit=8,
            ),
        ).rerank("问题", (invalid,))
