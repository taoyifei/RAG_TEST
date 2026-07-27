from qdrant_client.http import models

from rag_app.retrieval.fusion import reciprocal_rank_fusion


def _point(chunk_id: str, score: float) -> models.ScoredPoint:
    return models.ScoredPoint(
        id=chunk_id,
        version=1,
        score=score,
        payload={"chunk_id": chunk_id, "text": chunk_id},
    )


def test_rrf_uses_rank_only_and_merges_query_channels() -> None:
    results = reciprocal_rank_fusion(
        {
            "original:dense": (
                _point("chunk-a", 0.99),
                _point("chunk-b", 0.98),
            ),
            "original:bm25": (
                _point("chunk-b", 100.0),
                _point("chunk-c", 99.0),
            ),
            "rewritten:dense": (
                _point("chunk-c", -100.0),
                _point("chunk-b", -200.0),
            ),
        },
        rank_constant=60,
        limit=3,
    )

    assert [item.chunk_id for item in results] == [
        "chunk-b",
        "chunk-c",
        "chunk-a",
    ]
    assert results[0].channel_ranks == (
        ("original:bm25", 1),
        ("original:dense", 2),
        ("rewritten:dense", 2),
    )


def test_rrf_rejects_payload_drift_for_same_chunk() -> None:
    first = _point("chunk-a", 1.0)
    second = _point("chunk-a", 2.0)
    second.payload = {"chunk_id": "chunk-a", "text": "changed"}

    try:
        reciprocal_rank_fusion(
            {"dense": (first,), "bm25": (second,)},
            rank_constant=60,
            limit=2,
        )
    except ValueError as error:
        assert "payload" in str(error)
    else:
        raise AssertionError("payload 漂移必须失败。")
