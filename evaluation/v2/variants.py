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
    best_identifier = reports[0].variant_id
    best_score = float("-inf")
    for report in reports:
        score = _selection_score(report)
        if score > best_score:
            best_identifier = report.variant_id
            best_score = score
    return best_identifier


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
