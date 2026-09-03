"""P08 检索、引用、拒答、隔离和工程指标。"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import mean

from pydantic import JsonValue

from evaluation.v2.models import (
    CaseObservation,
    EvaluationCase,
    MetricReport,
    MetricValue,
)

_MINIMUM_CI_SAMPLES = 5
_BOOTSTRAP_ROUNDS = 500


@dataclass(frozen=True, slots=True)
class MetricContext:
    """一组指标共享的 Lane、候选、Split 和随机种子。"""

    lane: str
    variant_id: str
    split: str
    seed: int


def compute_metric_report(
    cases: Sequence[EvaluationCase],
    observations: Sequence[CaseObservation],
    *,
    context: MetricContext,
) -> MetricReport:
    """计算总体、分类切片和工程指标。

    Args:
        cases: 本次允许读取标签的 Case。
        observations: 同一 lane、variant 和 split 的实际观测。
        context: Lane、候选、Split 和 Bootstrap 固定种子。

    Returns:
        带样本量、区间状态和分类切片的报告。

    Raises:
        ValueError: Case 与观测不能形成一一映射。

    """
    paired = _pair_cases(cases, observations)
    metrics = _metric_values(paired, seed=context.seed)
    by_category: dict[str, list[tuple[EvaluationCase, CaseObservation]]] = (
        defaultdict(list)
    )
    for pair in paired:
        by_category[pair[0].category].append(pair)
    categories = {
        category: _metric_values(items, seed=context.seed)
        for category, items in sorted(by_category.items())
    }
    engineering = _engineering_values(observations)
    return MetricReport(
        lane=context.lane,
        variant_id=context.variant_id,
        split=context.split,
        metrics=metrics,
        categories=categories,
        engineering=engineering,
    )


def reciprocal_rank_at_k(
    ranked_ids: Sequence[str], relevant_ids: set[str], *, k: int
) -> float:
    """计算稳定输入顺序下的 Reciprocal Rank。

    Args:
        ranked_ids: 已按稳定 tie-break 排好的候选 ID。
        relevant_ids: 相关候选 ID 集合。
        k: 最大检查深度。

    Returns:
        首个相关候选的倒数排名；未命中时为 0。

    """
    for rank, identifier in enumerate(ranked_ids[:k], start=1):
        if identifier in relevant_ids:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(
    ranked_ids: Sequence[str], relevant_ids: set[str], *, k: int
) -> float:
    """计算二元相关标签的 nDCG。

    Args:
        ranked_ids: 已按稳定 tie-break 排好的候选 ID。
        relevant_ids: 相关候选 ID 集合。
        k: 最大检查深度。

    Returns:
        0 到 1 的 nDCG；无相关标签时为 0。

    """
    if not relevant_ids:
        return 0.0
    unique_ranked_ids = tuple(dict.fromkeys(ranked_ids))
    gains = [
        1.0 / math.log2(rank + 1)
        for rank, identifier in enumerate(unique_ranked_ids[:k], start=1)
        if identifier in relevant_ids
    ]
    ideal_count = min(k, len(relevant_ids))
    ideal = sum(
        1.0 / math.log2(rank + 1)
        for rank in range(1, ideal_count + 1)
    )
    return sum(gains) / ideal


def refusal_scores(
    expected_refusal: Sequence[bool], observed_refusal: Sequence[bool]
) -> tuple[float, float, float]:
    """手算拒答 Precision、Recall 和 F1。

    Args:
        expected_refusal: 标签为不可回答的布尔序列。
        observed_refusal: 系统实际拒答的布尔序列。

    Returns:
        Precision、Recall 和 F1；无分母时返回 0。

    Raises:
        ValueError: 两个序列长度不同。

    """
    if len(expected_refusal) != len(observed_refusal):
        raise ValueError("拒答标签与结果长度不一致。")
    true_positive = sum(
        expected and observed
        for expected, observed in zip(
            expected_refusal, observed_refusal, strict=True
        )
    )
    false_positive = sum(
        not expected and observed
        for expected, observed in zip(
            expected_refusal, observed_refusal, strict=True
        )
    )
    false_negative = sum(
        expected and not observed
        for expected, observed in zip(
            expected_refusal, observed_refusal, strict=True
        )
    )
    precision = _safe_ratio(true_positive, true_positive + false_positive)
    recall = _safe_ratio(true_positive, true_positive + false_negative)
    f1 = _safe_ratio(2.0 * precision * recall, precision + recall)
    return precision, recall, f1


def _pair_cases(
    cases: Sequence[EvaluationCase],
    observations: Sequence[CaseObservation],
) -> tuple[tuple[EvaluationCase, CaseObservation], ...]:
    mapped = {item.case_id: item for item in observations}
    if len(mapped) != len(observations):
        raise ValueError("同一指标输入包含重复 Case 观测。")
    expected = {case.case_id for case in cases}
    if set(mapped) != expected:
        missing = sorted(expected - set(mapped))
        unknown = sorted(set(mapped) - expected)
        raise ValueError(
            f"Case/观测不一致：missing={missing} unknown={unknown}"
        )
    return tuple((case, mapped[case.case_id]) for case in cases)


def _metric_values(
    paired: Sequence[tuple[EvaluationCase, CaseObservation]],
    *,
    seed: int,
) -> dict[str, MetricValue]:
    answerable = [pair for pair in paired if pair[0].expected.answerable]
    negatives = [pair for pair in paired if not pair[0].expected.answerable]
    table = [
        pair for pair in answerable if pair[0].category == "table_structure"
    ]
    identifier = [
        pair for pair in answerable if pair[0].expected.required_identifiers
    ]
    values: dict[str, MetricValue] = {}
    _add_retrieval_stage_metrics(values, answerable, seed=seed)
    for depth in (1, 5, 10):
        values[f"recall_at_{depth}"] = _mean_metric(
            [
                _recall(
                    observed.retrieved_chunk_ids[:depth],
                    set(case.expected.relevant_chunk_ids),
                )
                for case, observed in answerable
            ],
            seed=seed,
        )
    values["mrr_at_10"] = _mean_metric(
        [
            reciprocal_rank_at_k(
                observed.retrieved_chunk_ids,
                set(case.expected.relevant_chunk_ids),
                k=10,
            )
            for case, observed in answerable
        ],
        seed=seed,
    )
    values["ndcg_at_10"] = _mean_metric(
        [
            ndcg_at_k(
                observed.retrieved_chunk_ids,
                set(case.expected.relevant_chunk_ids),
                k=10,
            )
            for case, observed in answerable
        ],
        seed=seed,
    )
    for depth in (1, 5):
        values[f"exact_identifier_hit_at_{depth}"] = _mean_metric(
            [
                float(
                    bool(
                        set(observed.retrieved_chunk_ids[:depth])
                        & set(case.expected.relevant_chunk_ids)
                    )
                    and any(
                        "exact" in origins
                        for origins in observed.retrieval_origins[:depth]
                    )
                )
                for case, observed in identifier
            ],
            seed=seed,
        )
    values["table_row_recall_at_5"] = _mean_metric(
        [
            _recall(
                observed.retrieved_chunk_ids[:5],
                set(case.expected.relevant_chunk_ids),
            )
            for case, observed in table
        ],
        seed=seed,
    )
    values["document_recall_at_10"] = _mean_metric(
        [
            _recall(
                observed.retrieved_document_ids[:10],
                set(case.expected.relevant_document_ids),
            )
            for case, observed in answerable
        ],
        seed=seed,
    )
    values["source_range_recall"] = _ratio_metric(
        sum(item[1].matched_source_range_count for item in answerable),
        sum(item[1].required_source_range_count for item in answerable),
    )
    values["source_range_coverage"] = values["source_range_recall"]
    values["source_range_precision"] = _ratio_metric(
        sum(
            item[1].relevant_predicted_source_range_count
            for item in answerable
        ),
        sum(item[1].predicted_source_range_count for item in answerable),
    )
    source_precision = values["source_range_precision"].value
    source_recall = values["source_range_recall"].value
    values["source_range_f1"] = _scalar_metric(
        _safe_ratio(
            2.0 * float(source_precision or 0.0) * float(source_recall or 0.0),
            float(source_precision or 0.0) + float(source_recall or 0.0),
        ),
        sum(item[1].required_source_range_count for item in answerable),
    )
    values["negative_leakage_at_10"] = _mean_metric(
        [
            float(
                bool(
                    set(observed.retrieved_document_ids[:10])
                    & set(case.constraints.forbidden_document_ids)
                )
            )
            for case, observed in negatives
        ],
        seed=seed,
    )
    _add_answer_metrics(values, paired, answerable, seed=seed)
    for field_name in (
        "unsupported_claim_count",
        "evidence_budget_overflow_count",
        "wrong_scope_hit_count",
        "wrong_revision_hit_count",
        "wrong_vector_space_attempt_count",
    ):
        values[field_name] = MetricValue(
            value=sum(getattr(item, field_name) for _, item in paired),
            sample_count=len(paired),
            status="ok" if paired else "not_executed",
        )
    return values


def _add_retrieval_stage_metrics(
    values: dict[str, MetricValue],
    answerable: Sequence[tuple[EvaluationCase, CaseObservation]],
    *,
    seed: int,
) -> None:
    stages: dict[
        str, Callable[[CaseObservation], tuple[str, ...]]
    ] = {
        "fusion": lambda item: item.fused_chunk_ids,
        "rerank": lambda item: item.reranked_chunk_ids,
    }
    for stage, selector in stages.items():
        for depth in (1, 5, 10):
            values[f"{stage}_recall_at_{depth}"] = _mean_metric(
                [
                    _recall(
                        selector(observed)[:depth],
                        set(case.expected.relevant_chunk_ids),
                    )
                    for case, observed in answerable
                ],
                seed=seed,
            )
        values[f"{stage}_mrr_at_10"] = _mean_metric(
            [
                reciprocal_rank_at_k(
                    selector(observed),
                    set(case.expected.relevant_chunk_ids),
                    k=10,
                )
                for case, observed in answerable
            ],
            seed=seed,
        )
        values[f"{stage}_ndcg_at_10"] = _mean_metric(
            [
                ndcg_at_k(
                    selector(observed),
                    set(case.expected.relevant_chunk_ids),
                    k=10,
                )
                for case, observed in answerable
            ],
            seed=seed,
        )
    for depth in (1, 5, 10):
        values[f"channel_recall_at_{depth}"] = _mean_metric(
            [
                max(
                    (
                        _recall(
                            chunk_ids[:depth],
                            set(case.expected.relevant_chunk_ids),
                        )
                        for _, chunk_ids in observed.channel_chunk_ids
                    ),
                    default=0.0,
                )
                for case, observed in answerable
            ],
            seed=seed,
        )


def _add_answer_metrics(
    values: dict[str, MetricValue],
    paired: Sequence[tuple[EvaluationCase, CaseObservation]],
    answerable: Sequence[tuple[EvaluationCase, CaseObservation]],
    *,
    seed: int,
) -> None:
    expected_refusal = [not case.expected.answerable for case, _ in paired]
    observed_refusal = [item.status != "ANSWERABLE" for _, item in paired]
    precision, recall, f1 = refusal_scores(
        expected_refusal, observed_refusal
    )
    values["answerable_accuracy"] = _mean_metric(
        [
            float(
                (item.status == "ANSWERABLE") == case.expected.answerable
            )
            for case, item in paired
        ],
        seed=seed,
    )
    values["refusal_precision"] = _scalar_metric(precision, len(paired))
    values["refusal_recall"] = _scalar_metric(recall, len(paired))
    values["refusal_f1"] = _scalar_metric(f1, len(paired))
    values["citation_presence_rate"] = _mean_metric(
        [float(item.citation_present) for _, item in answerable],
        seed=seed,
    )
    values["citation_validity_rate"] = _mean_metric(
        [float(item.citation_valid) for _, item in answerable],
        seed=seed,
    )
    values["quote_publishability_rate"] = _mean_metric(
        [float(item.quote_publishable) for _, item in answerable],
        seed=seed,
    )
    values["citation_source_precision"] = _mean_metric(
        [
            _safe_ratio(
                len(
                    set(item.cited_document_ids)
                    & set(case.expected.relevant_document_ids)
                ),
                len(set(item.cited_document_ids)),
            )
            for case, item in answerable
        ],
        seed=seed,
    )
    values["citation_document_precision"] = values[
        "citation_source_precision"
    ]
    values["citation_chunk_precision"] = _mean_metric(
        [
            _safe_ratio(
                len(
                    set(item.cited_chunk_ids)
                    & set(case.expected.relevant_chunk_ids)
                ),
                len(set(item.cited_chunk_ids)),
            )
            for case, item in answerable
        ],
        seed=seed,
    )
    values["evidence_document_precision"] = _mean_metric(
        [
            _safe_ratio(
                len(
                    set(item.evidence_document_ids)
                    & set(case.expected.relevant_document_ids)
                ),
                len(set(item.evidence_document_ids)),
            )
            for case, item in answerable
        ],
        seed=seed,
    )
    values["evidence_chunk_precision"] = _mean_metric(
        [
            _safe_ratio(
                len(
                    set(item.evidence_chunk_ids)
                    & set(case.expected.relevant_chunk_ids)
                ),
                len(set(item.evidence_chunk_ids)),
            )
            for case, item in answerable
        ],
        seed=seed,
    )
    values["evidence_items_per_answer"] = _mean_metric(
        [float(item.evidence_count) for _, item in answerable],
        seed=seed,
    )
    values["irrelevant_evidence_count"] = MetricValue(
        value=sum(
            len(
                set(item.evidence_chunk_ids)
                - set(case.expected.relevant_chunk_ids)
            )
            for case, item in answerable
        ),
        sample_count=len(answerable),
        status="ok" if answerable else "not_executed",
    )


def _engineering_values(
    observations: Sequence[CaseObservation],
) -> dict[str, JsonValue]:
    latencies = sorted(item.latency_ms for item in observations)
    if not latencies:
        return {"status": "not_executed"}
    return {
        "status": "ok",
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "p99_latency_ms": _percentile(latencies, 0.99),
        "provider_call_count": sum(
            item.provider_call_count for item in observations
        ),
        "provider_retry_count": sum(
            item.provider_retry_count for item in observations
        ),
        "embedding_call_count": sum(
            item.embedding_call_count for item in observations
        ),
        "embedding_retry_count": sum(
            item.embedding_retry_count for item in observations
        ),
        "reranker_call_count": sum(
            item.reranker_call_count for item in observations
        ),
        "reranker_retry_count": sum(
            item.reranker_retry_count for item in observations
        ),
        "failover_count": sum(
            item.selected_embedding_slot == "standby"
            for item in observations
        ),
        "reranker_bypass_count": sum(
            "bypass" in item.rerank_mode.casefold() for item in observations
        ),
        "cache_hit_count": sum(item.cache_hit for item in observations),
        "evidence_count": sum(item.evidence_count for item in observations),
        "evidence_tokens": sum(item.evidence_tokens for item in observations),
        "stage_elapsed_ms": _stage_elapsed(observations),
    }


def _stage_elapsed(
    observations: Sequence[CaseObservation],
) -> dict[str, JsonValue]:
    values: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        for stage, elapsed_ms in observation.stage_elapsed_ms:
            values[stage].append(elapsed_ms)
    return {
        stage: mean(elapsed_values)
        for stage, elapsed_values in sorted(values.items())
    }


def _mean_metric(values: Sequence[float], *, seed: int) -> MetricValue:
    if not values:
        return MetricValue(
            value=None,
            sample_count=0,
            status="not_executed",
        )
    interval = _bootstrap_interval(values, seed=seed)
    return MetricValue(
        value=mean(values),
        sample_count=len(values),
        status=(
            "ok"
            if len(values) >= _MINIMUM_CI_SAMPLES
            else "insufficient_sample"
        ),
        ci95=interval,
    )


def _ratio_metric(numerator: int, denominator: int) -> MetricValue:
    if denominator == 0:
        return MetricValue(
            value=None,
            sample_count=0,
            status="not_executed",
        )
    return MetricValue(
        value=numerator / denominator,
        sample_count=denominator,
        status=(
            "ok"
            if denominator >= _MINIMUM_CI_SAMPLES
            else "insufficient_sample"
        ),
    )


def _scalar_metric(value: float, sample_count: int) -> MetricValue:
    if sample_count == 0:
        return MetricValue(
            value=None,
            sample_count=0,
            status="not_executed",
        )
    return MetricValue(
        value=value,
        sample_count=sample_count,
        status=(
            "ok"
            if sample_count >= _MINIMUM_CI_SAMPLES
            else "insufficient_sample"
        ),
    )


def _recall(observed: Sequence[str], relevant: set[str]) -> float:
    return _safe_ratio(len(set(observed) & relevant), len(relevant))


def _safe_ratio(numerator: float | int, denominator: float | int) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _bootstrap_interval(
    values: Sequence[float], *, seed: int
) -> tuple[float, float] | None:
    if len(values) < _MINIMUM_CI_SAMPLES:
        return None
    sample_means = sorted(
        mean(
            values[
                _bootstrap_index(
                    seed, round_index, sample_index, len(values)
                )
            ]
            for sample_index in range(len(values))
        )
        for round_index in range(_BOOTSTRAP_ROUNDS)
    )
    return (
        sample_means[int(0.025 * (_BOOTSTRAP_ROUNDS - 1))],
        sample_means[int(0.975 * (_BOOTSTRAP_ROUNDS - 1))],
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    index = max(0, math.ceil(quantile * len(values)) - 1)
    return float(values[index])


def _bootstrap_index(
    seed: int,
    round_index: int,
    sample_index: int,
    sample_count: int,
) -> int:
    payload = f"{seed}:{round_index}:{sample_index}".encode()
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") % sample_count


__all__ = [
    "MetricContext",
    "compute_metric_report",
    "ndcg_at_k",
    "reciprocal_rank_at_k",
    "refusal_scores",
]
