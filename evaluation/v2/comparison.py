"""P08 immutable Run 的基线和关键类别回归比较。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue

from evaluation.v2.models import MetricReport, RunManifest

_QUALITY_METRICS = (
    "recall_at_5",
    "mrr_at_10",
    "ndcg_at_10",
    "answerable_accuracy",
    "refusal_f1",
    "citation_validity_rate",
    "source_range_coverage",
)
_SAFETY_COUNTERS = (
    "negative_leakage_at_10",
    "unsupported_claim_count",
    "wrong_scope_hit_count",
    "wrong_revision_hit_count",
    "wrong_vector_space_attempt_count",
)


def compare_run_directories(
    baseline: Path, candidate: Path
) -> dict[str, object]:
    """比较同数据集、Profile 和模式的两个不可变 Run。

    Args:
        baseline: 基线 Run 目录。
        candidate: 候选 Run 目录。

    Returns:
        总体、关键类别和安全回归的机器可读结果。

    Raises:
        ValueError: 两个 Run 身份不兼容。

    """
    baseline_manifest, baseline_metrics = _load_run(baseline)
    candidate_manifest, candidate_metrics = _load_run(candidate)
    if baseline_manifest.dataset_sha256 != candidate_manifest.dataset_sha256:
        raise ValueError("基线与候选 dataset SHA 不一致。")
    if baseline_manifest.profile_id != candidate_manifest.profile_id:
        raise ValueError("基线与候选 Profile 不一致。")
    if baseline_manifest.lane != candidate_manifest.lane:
        raise ValueError("基线与候选 Lane 不一致。")
    comparisons: dict[str, JsonValue] = {}
    passed = True
    for name in _QUALITY_METRICS:
        result = _not_lower(baseline_metrics, candidate_metrics, name)
        comparisons[name] = result
        passed = passed and bool(result["passed"])
    for name in _SAFETY_COUNTERS:
        result = _not_higher(baseline_metrics, candidate_metrics, name)
        comparisons[name] = result
        passed = passed and bool(result["passed"])
    baseline_tuning = _load_selected_tuning(
        baseline, baseline_manifest.selected_candidate
    )
    candidate_tuning = _load_selected_tuning(
        candidate, candidate_manifest.selected_candidate
    )
    critical = _critical_category_comparison(
        (baseline_metrics, baseline_tuning),
        (candidate_metrics, candidate_tuning),
    )
    passed = passed and all(bool(item["passed"]) for item in critical.values())
    return {
        "schema_version": "1",
        "baseline_run_id": baseline_manifest.run_id,
        "candidate_run_id": candidate_manifest.run_id,
        "baseline_not_regressed": passed,
        "metrics": comparisons,
        "critical_categories": critical,
    }


def summarize_run(run_directory: Path) -> dict[str, object]:
    """读取 Run 的安全身份和最终门禁摘要。

    Args:
        run_directory: 已完成的不可变 Run 目录。

    Returns:
        不含 query、正文或绝对路径的摘要。

    """
    manifest, metrics = _load_run(run_directory)
    gate_payload = json.loads(
        (run_directory / "gates.json").read_text(encoding="utf-8")
    )
    return {
        "run_id": manifest.run_id,
        "dataset_sha256": manifest.dataset_sha256,
        "profile_id": manifest.profile_id,
        "lane": manifest.lane,
        "selected_candidate": manifest.selected_candidate,
        "external_services_actually_called": list(
            manifest.external_services_actually_called
        ),
        "holdout_metrics": {
            name: value.model_dump(mode="json")
            for name, value in metrics.metrics.items()
        },
        "engineering_metrics": metrics.engineering,
        "gates": gate_payload,
    }


def _load_run(directory: Path) -> tuple[RunManifest, MetricReport]:
    manifest = RunManifest.model_validate_json(
        (directory / "manifest.json").read_text(encoding="utf-8")
    )
    metrics = MetricReport.model_validate_json(
        (directory / "selected-metrics.json").read_text(encoding="utf-8")
    )
    return manifest, metrics


def _load_selected_tuning(directory: Path, variant_id: str) -> MetricReport:
    payload = json.loads(
        (directory / "tuning-metrics.json").read_text(encoding="utf-8")
    )
    if variant_id not in payload:
        raise ValueError("不可变 Run 缺少已选候选的 tuning 指标。")
    return MetricReport.model_validate(payload[variant_id])


def _not_lower(
    baseline: MetricReport, candidate: MetricReport, name: str
) -> dict[str, JsonValue]:
    base = baseline.metrics[name].value
    observed = candidate.metrics[name].value
    passed = base is not None and observed is not None and observed >= base
    return {"baseline": base, "candidate": observed, "passed": passed}


def _not_higher(
    baseline: MetricReport, candidate: MetricReport, name: str
) -> dict[str, JsonValue]:
    base = baseline.metrics[name].value
    observed = candidate.metrics[name].value
    passed = base is not None and observed is not None and observed <= base
    return {"baseline": base, "candidate": observed, "passed": passed}


def _critical_category_comparison(
    baseline_reports: tuple[MetricReport, ...],
    candidate_reports: tuple[MetricReport, ...],
) -> dict[str, dict[str, JsonValue]]:
    critical = {
        "document_identity",
        "table_structure",
        "revision_isolation",
        "scope_isolation",
        "negative_refusal",
    }
    output: dict[str, dict[str, JsonValue]] = {}
    for category in sorted(critical):
        baseline_report = next(
            (
                report
                for report in baseline_reports
                if category in report.categories
            ),
            None,
        )
        candidate_report = next(
            (
                report
                for report in candidate_reports
                if category in report.categories
            ),
            None,
        )
        baseline_metric = (
            baseline_report.categories[category].get("answerable_accuracy")
            if baseline_report is not None
            else None
        )
        baseline_value = (
            baseline_metric.value if baseline_metric is not None else None
        )
        candidate_value = (
            candidate_report.categories[category].get("answerable_accuracy")
            if candidate_report is not None
            else None
        )
        observed = (
            candidate_value.value if candidate_value is not None else None
        )
        passed = (
            baseline_value is not None
            and observed is not None
            and observed >= baseline_value
        )
        output[category] = {
            "metric": "answerable_accuracy",
            "baseline": baseline_value,
            "candidate": observed,
            "split": (
                baseline_report.split
                if baseline_report is not None
                else "missing"
            ),
            "passed": passed,
        }
    return output


__all__ = ["compare_run_directories", "summarize_run"]
