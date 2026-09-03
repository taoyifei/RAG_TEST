from __future__ import annotations

from evaluation.v2.metrics import MetricContext, compute_metric_report
from tests.evaluation.test_metrics import _case, _observation


def test_retrieval_metrics_use_fusion_and_rerank_diagnostics() -> None:
    case = _case()
    observation = _observation().model_copy(
        update={
            "fused_chunk_ids": ("chunk_expected",),
            "reranked_chunk_ids": ("chunk_expected",),
            "evidence_chunk_ids": ("chunk_irrelevant",),
            "evidence_document_ids": ("doc_" + "9" * 32,),
        }
    )

    report = compute_metric_report(
        (case,),
        (observation,),
        context=MetricContext(
            lane="offline-structural",
            variant_id="baseline",
            split="tuning",
            seed=7,
        ),
    )

    assert report.metrics["fusion_recall_at_1"].value == 1.0
    assert report.metrics["rerank_recall_at_1"].value == 1.0
    assert report.metrics["evidence_chunk_precision"].value == 0.0


def test_engineering_metrics_use_recorded_provider_counts() -> None:
    report = compute_metric_report(
        (_case(),),
        (
            _observation().model_copy(
                update={
                    "embedding_call_count": 2,
                    "embedding_retry_count": 1,
                    "reranker_call_count": 1,
                    "reranker_retry_count": 0,
                    "stage_elapsed_ms": (("retrieve", 4.5),),
                }
            ),
        ),
        context=MetricContext(
            lane="offline-structural",
            variant_id="baseline",
            split="tuning",
            seed=7,
        ),
    )

    assert report.engineering["embedding_call_count"] == 2
    assert report.engineering["embedding_retry_count"] == 1
    assert report.engineering["reranker_call_count"] == 1
    assert report.engineering["stage_elapsed_ms"] == {"retrieve": 4.5}
