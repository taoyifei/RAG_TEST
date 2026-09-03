"""P08 检索、拒答和隔离指标手算回归。"""

from __future__ import annotations

import math

from evaluation.v2.metrics import (
    MetricContext,
    compute_metric_report,
    ndcg_at_k,
    reciprocal_rank_at_k,
    refusal_scores,
)
from evaluation.v2.models import CaseObservation, EvaluationCase


def _case(*, answerable: bool = True) -> EvaluationCase:
    expected = {
        "relevant_document_ids": ["doc_" + "1" * 32] if answerable else [],
        "relevant_chunk_ids": ["chunk_expected"] if answerable else [],
        "required_identifiers": [],
        "required_source_ranges": (
            [{"document_id": "doc_" + "1" * 32, "exact_text": "fact"}]
            if answerable
            else []
        ),
        "answerable": answerable,
        "expected_refusal_reason": None if answerable else "NO_EVIDENCE",
    }
    return EvaluationCase.model_validate(
        {
            "schema_version": "2",
            "case_id": "eval_metric_case",
            "split": "tuning",
            "group_id": "grp_metric_case",
            "category": "exact_identifier",
            "difficulty": "basic",
            "failure_severity": "critical",
            "project_id": "prj_" + "2" * 32,
            "knowledge_base_id": "kb_" + "3" * 32,
            "query": "synthetic query",
            "expected": expected,
            "constraints": {},
        }
    )


def _observation(*, wrong_vector: int = 0) -> CaseObservation:
    return CaseObservation(
        case_id="eval_metric_case",
        split="tuning",
        group_id="grp_metric_case",
        category="exact_identifier",
        failure_severity="critical",
        variant_id="baseline",
        lane="offline-structural",
        status="ANSWERABLE",
        reason_code="SUPPORTED",
        active_index_revision_id="irev_test",
        index_fingerprint="sha256:" + "4" * 64,
        serving_fingerprint="sha256:" + "5" * 64,
        route_reason_code="DENSE_PRIMARY",
        rerank_mode="executed",
        retrieved_document_ids=("doc_" + "1" * 32,),
        retrieved_chunk_ids=("chunk_other", "chunk_expected"),
        retrieval_origins=(("lexical",), ("exact",)),
        cited_document_ids=("doc_" + "1" * 32,),
        cited_chunk_ids=("chunk_expected",),
        matched_source_range_count=1,
        required_source_range_count=1,
        citation_present=True,
        citation_valid=True,
        quote_publishable=True,
        unsupported_claim_count=0,
        evidence_budget_overflow_count=0,
        wrong_scope_hit_count=0,
        wrong_revision_hit_count=0,
        wrong_vector_space_attempt_count=wrong_vector,
        latency_ms=1.0,
        provider_call_count=1,
        provider_retry_count=0,
    )


def test_rank_metrics_match_hand_calculation_and_stable_ties() -> None:
    ranked = ("irrelevant", "relevant", "another")

    assert reciprocal_rank_at_k(ranked, {"relevant"}, k=10) == 0.5
    assert ndcg_at_k(ranked, {"relevant"}, k=10) == 1.0 / math.log2(3)
    assert reciprocal_rank_at_k(ranked, {"relevant"}, k=10) == 0.5


def test_ndcg_deduplicates_multiple_evidence_items_from_one_chunk() -> None:
    ranked = ("relevant", "relevant", "irrelevant")

    assert ndcg_at_k(ranked, {"relevant"}, k=10) == 1.0


def test_refusal_precision_recall_and_f1_match_hand_calculation() -> None:
    precision, recall, f1 = refusal_scores(
        (True, True, False, False),
        (True, False, True, False),
    )

    assert precision == 0.5
    assert recall == 0.5
    assert f1 == 0.5


def test_wrong_vector_space_attempt_is_counted() -> None:
    report = compute_metric_report(
        (_case(),),
        (_observation(wrong_vector=1),),
        context=MetricContext(
            lane="offline-structural",
            variant_id="baseline",
            split="tuning",
            seed=7,
        ),
    )

    assert report.metrics["recall_at_1"].value == 0.0
    assert report.metrics["recall_at_5"].value == 1.0
    assert report.metrics["mrr_at_10"].value == 0.5
    assert report.metrics["wrong_vector_space_attempt_count"].value == 1
