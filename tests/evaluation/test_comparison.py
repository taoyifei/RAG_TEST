"""P08 基线回归比较。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from evaluation.v2.artifacts import write_json
from evaluation.v2.comparison import compare_run_directories
from evaluation.v2.models import (
    ComponentIdentity,
    MetricReport,
    MetricValue,
    ProviderRunIdentity,
    RunManifest,
)


def _metric(value: float) -> MetricValue:
    return MetricValue(value=value, sample_count=8, status="ok")


def _report(*, table_accuracy: float) -> MetricReport:
    metrics = {
        name: _metric(value)
        for name, value in {
            "recall_at_5": 1.0,
            "mrr_at_10": 1.0,
            "ndcg_at_10": 1.0,
            "answerable_accuracy": table_accuracy,
            "refusal_f1": 1.0,
            "citation_validity_rate": 1.0,
            "source_range_coverage": 1.0,
            "negative_leakage_at_10": 0.0,
            "unsupported_claim_count": 0.0,
            "wrong_scope_hit_count": 0.0,
            "wrong_revision_hit_count": 0.0,
            "wrong_vector_space_attempt_count": 0.0,
        }.items()
    }
    return MetricReport(
        lane="offline-structural",
        variant_id="evidence-cap-8",
        split="holdout",
        metrics=metrics,
        categories={
            category: {
                "answerable_accuracy": _metric(
                    table_accuracy if category == "table_structure" else 1.0
                )
            }
            for category in (
                "document_identity",
                "negative_refusal",
                "revision_isolation",
                "scope_isolation",
                "table_structure",
            )
        },
        engineering={"status": "ok"},
    )


def _manifest(run_id: str) -> RunManifest:
    now = datetime(2026, 9, 3, tzinfo=UTC)
    return RunManifest(
        run_id=run_id,
        state="complete",
        started_at=now,
        finished_at=now,
        integration_sha="1" * 40,
        index_revision_ids=("irev_test",),
        index_fingerprints=("sha256:" + "2" * 64,),
        serving_fingerprints=("sha256:" + "3" * 64,),
        profile_id="p08-offline",
        providers=(
            ProviderRunIdentity(
                provider="deterministic",
                model="deterministic-v1",
                slot="primary",
                vector_name="dense_primary",
                request_policy_identity="sha256:" + "4" * 64,
                adapter_revision="deterministic-v1",
            ),
        ),
        parser=ComponentIdentity(component_id="parser", version="1"),
        chunker=ComponentIdentity(component_id="chunker", version="1"),
        tokenizer_identities=("tokenizer",),
        parameters={"selected_status": "provisional_offline_only"},
        dataset_id="synthetic-p08",
        dataset_sha256="sha256:" + "5" * 64,
        case_ids=("eval_case",),
        lane="offline-structural",
        seed=7,
        network_authorization_mode="offline",
        external_services_actually_called=(),
        package_versions={"docx-rag": "test"},
        result_files=(),
        selected_candidate="evidence-cap-8",
        holdout_access_count=1,
    )


def _write_run(path: Path, run_id: str, table_accuracy: float) -> None:
    path.mkdir()
    write_json(path / "manifest.json", _manifest(run_id))
    write_json(
        path / "selected-metrics.json",
        _report(table_accuracy=table_accuracy),
    )
    write_json(
        path / "tuning-metrics.json",
        {"evidence-cap-8": _report(table_accuracy=table_accuracy).model_dump(
            mode="json"
        )},
    )


def test_baseline_comparison_detects_critical_category_regression(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    _write_run(baseline, "p08-20260903T000000Z-aaaaaaaa", 1.0)
    _write_run(candidate, "p08-20260903T000001Z-bbbbbbbb", 0.0)

    result = compare_run_directories(baseline, candidate)

    assert result["baseline_not_regressed"] is False
    critical = result["critical_categories"]
    assert isinstance(critical, dict)
    assert critical["table_structure"]["passed"] is False
