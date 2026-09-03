"""P08 单变量消融、参数候选和 tuning-only 选择。"""

from __future__ import annotations

from dataclasses import dataclass

from evaluation.v2.models import MetricReport
from rag_app.core.models import ChunkingPolicy, RetrievalPolicy


@dataclass(frozen=True, slots=True)
class EvaluationVariant:
    """一次只改变一个主要变量的离线候选。"""

    variant_id: str
    changed_variable: str
    retrieval_policy: RetrievalPolicy
    chunking_policy: ChunkingPolicy


def offline_variants() -> tuple[EvaluationVariant, ...]:
    """返回有界且顺序稳定的 P08 离线消融矩阵。

    Args:
        无参数；使用当前 provisional 默认值。

    Returns:
        基线、通道、重排、邻居、表格上下文和 Chunk 候选。

    """
    retrieval = RetrievalPolicy()
    chunking = ChunkingPolicy(
        required_embedding_slots=("primary",),
        max_embedding_tokens_by_slot=(("primary", 32768),),
    )
    return (
        EvaluationVariant("baseline", "none", retrieval, chunking),
        EvaluationVariant(
            "exact-only",
            "enabled_channels",
            retrieval.model_copy(update={"enabled_channels": ("exact",)}),
            chunking,
        ),
        EvaluationVariant(
            "fts5-only",
            "enabled_channels",
            retrieval.model_copy(update={"enabled_channels": ("lexical",)}),
            chunking,
        ),
        EvaluationVariant(
            "dense-primary-only",
            "enabled_channels",
            retrieval.model_copy(update={"enabled_channels": ("dense",)}),
            chunking,
        ),
        EvaluationVariant(
            "exact-fts5",
            "enabled_channels",
            retrieval.model_copy(
                update={"enabled_channels": ("exact", "lexical")}
            ),
            chunking,
        ),
        EvaluationVariant(
            "rrf-without-rerank",
            "rerank_enabled",
            retrieval.model_copy(update={"rerank_enabled": False}),
            chunking,
        ),
        EvaluationVariant(
            "without-neighbor-expansion",
            "neighbor_expansion_enabled",
            retrieval.model_copy(
                update={"neighbor_expansion_enabled": False}
            ),
            chunking,
        ),
        EvaluationVariant(
            "evidence-cap-8",
            "evidence_caps",
            retrieval.model_copy(
                update={"per_document_cap": 8, "per_section_cap": 8}
            ),
            chunking,
        ),
        EvaluationVariant(
            "without-table-structural-context",
            "include_table_header",
            retrieval,
            chunking.model_copy(update={"include_table_header": False}),
        ),
        EvaluationVariant(
            "chunk-256",
            "chunk_target",
            retrieval,
            chunking.model_copy(
                update={
                    "target_tokens": 256,
                    "hard_max_tokens": 384,
                    "overlap_cap_tokens": 48,
                }
            ),
        ),
        EvaluationVariant(
            "chunk-320",
            "chunk_target",
            retrieval,
            chunking.model_copy(
                update={
                    "target_tokens": 320,
                    "hard_max_tokens": 448,
                    "overlap_cap_tokens": 56,
                }
            ),
        ),
        EvaluationVariant(
            "chunk-384",
            "chunk_target",
            retrieval,
            chunking,
        ),
    )


def select_tuning_candidate(
    reports: tuple[MetricReport, ...],
) -> str:
    """只依据 tuning 的质量与负例安全选择稳定候选。

    Args:
        reports: 全部必须明确标记为 tuning 的候选报告。

    Returns:
        目标函数最高且按输入顺序稳定 tie-break 的 variant ID。

    Raises:
        ValueError: 输入为空、混入 holdout 或缺少目标指标。

    """
    if not reports:
        raise ValueError("参数选择至少需要一个 tuning 报告。")
    if any(report.split != "tuning" for report in reports):
        raise ValueError("参数选择禁止读取 holdout 指标。")
    if "source_range_precision" not in reports[0].metrics:
        return max(reports, key=_selection_score).variant_id
    baseline = next(
        (report for report in reports if report.variant_id == "baseline"),
        reports[0],
    )
    eligible = tuple(
        report for report in reports if _passes_v3_safety(report)
    )
    if not eligible:
        raise ValueError("没有候选通过 Evaluation V3 安全与精度门。")
    return max(
        eligible,
        key=lambda report: _v3_selection_key(report, baseline),
    ).variant_id


def _passes_v3_safety(report: MetricReport) -> bool:
    requirements = {
        "wrong_scope_hit_count": ("eq", 0.0),
        "wrong_revision_hit_count": ("eq", 0.0),
        "wrong_vector_space_attempt_count": ("eq", 0.0),
        "citation_document_precision": ("ge", 0.8),
        "citation_chunk_precision": ("ge", 0.8),
        "source_range_precision": ("ge", 0.75),
        "source_range_recall": ("ge", 0.9),
        "source_range_f1": ("ge", 0.8),
        "irrelevant_evidence_count": ("eq", 0.0),
    }
    for name, (operator, expected) in requirements.items():
        value = report.metrics[name].value
        if value is None:
            return False
        observed = float(value)
        if operator == "eq" and observed != expected:
            return False
        if operator == "ge" and observed < expected:
            return False
    return True


def _v3_selection_key(
    report: MetricReport, baseline: MetricReport
) -> tuple[float, ...]:
    def value(name: str) -> float:
        """读取参与选择的有限指标值。

        Args:
            name: 指标名称。

        Returns:
            转换后的浮点指标值。

        """
        observed = report.metrics[name].value
        if observed is None:
            raise ValueError(f"参数选择缺少指标值：{name}")
        return float(observed)

    baseline_recall = baseline.metrics["fusion_recall_at_5"].value
    non_regression = float(
        baseline_recall is not None
        and value("fusion_recall_at_5") >= float(baseline_recall)
    )
    latency = _engineering_number(report, "p95_latency_ms")
    calls = _engineering_number(report, "provider_call_count")
    return (
        value("source_range_recall"),
        min(
            value("citation_document_precision"),
            value("citation_chunk_precision"),
            value("evidence_document_precision"),
            value("evidence_chunk_precision"),
        ),
        non_regression,
        value("fusion_mrr_at_10"),
        value("rerank_ndcg_at_10"),
        -latency,
        -calls,
    )


def _engineering_number(report: MetricReport, name: str) -> float:
    value = report.engineering.get(name, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"工程指标不是数值：{name}")
    return float(value)


def _selection_score(report: MetricReport) -> float:
    required = (
        "recall_at_5",
        "refusal_f1",
        "citation_validity_rate",
        "source_range_coverage",
        "negative_leakage_at_10",
        "wrong_scope_hit_count",
        "wrong_revision_hit_count",
        "wrong_vector_space_attempt_count",
    )
    values: dict[str, float] = {}
    for name in required:
        metric = report.metrics[name]
        if metric.value is None:
            raise ValueError(f"参数选择缺少指标值：{name}")
        values[name] = float(metric.value)
    safety_penalty = (
        values["negative_leakage_at_10"]
        + values["wrong_scope_hit_count"]
        + values["wrong_revision_hit_count"]
        + values["wrong_vector_space_attempt_count"]
    )
    return (
        values["recall_at_5"]
        + values["refusal_f1"]
        + values["citation_validity_rate"]
        + values["source_range_coverage"]
        - 2.0 * safety_penalty
    )


__all__ = [
    "EvaluationVariant",
    "offline_variants",
    "select_tuning_candidate",
]
