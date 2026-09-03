"""P08 tuning 消融、固定候选 holdout 和不可变 Run 编排。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import resource
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from evaluation.v2.artifacts import (
    create_run_directory,
    validate_manifest_safety,
    write_json,
    write_jsonl,
    write_text,
)
from evaluation.v2.dataset import LoadedDataset, load_dataset_directory
from evaluation.v2.fixtures import fixture_catalog_sha256
from evaluation.v2.gates import evaluate_gates, load_gate_configuration
from evaluation.v2.metrics import MetricContext, compute_metric_report
from evaluation.v2.models import (
    CaseObservation,
    ErrorRecord,
    GateReport,
    MetricReport,
    ProviderRunIdentity,
    ResultFile,
    RunManifest,
)
from evaluation.v2.runtime import (
    VariantExecution,
    execute_offline_variant,
    validate_fixture_identities,
)
from evaluation.v2.variants import (
    EvaluationVariant,
    offline_variants,
    select_tuning_candidate,
)
from rag_app.composition.profiles import RagProfile, load_profile

_LIVE_LANES = {"live-primary", "live-standby"}
_PACKAGE_NAMES = (
    "docx-rag",
    "pydantic",
    "qdrant-client",
    "python-docx",
    "tokenizers",
)


class LiveLaneBlockedError(RuntimeError):
    """Live Lane 缺少显式授权、预算或凭据。"""


@dataclass(frozen=True, slots=True)
class RunOptions:
    """CLI 已解析且显式的 P08 Run 选项。"""

    dataset: Path
    profile: Path
    lane: str
    reports_root: Path
    gates: Path
    seed: int = 20260903
    run_id: str | None = None
    live_provider: bool = False
    acknowledge_egress: bool = False
    budget_requests: int | None = None
    budget_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """CLI 返回码所需的最小 Run 结果。"""

    run_directory: Path
    manifest: RunManifest
    gates: GateReport


def run_evaluation(options: RunOptions) -> RunOutcome:
    """执行默认离线 P08 Run 或失败关闭 Live Lane。

    Args:
        options: 数据集、Profile、Lane、预算和输出根。

    Returns:
        已完成的不可变 Run 目录、Manifest 和门禁。

    Raises:
        LiveLaneBlockedError: Live Lane 缺少授权、预算或凭据。
        ValueError: Lane、Profile 或数据集无效。

    """
    _validate_lane(options)
    if options.lane in _LIVE_LANES:
        raise LiveLaneBlockedError(
            "BLOCKED_LIVE_EXECUTION_REQUIRES_SEPARATE_AUTHORIZED_RUNNER"
        )
    return _run_offline(options)


def _validate_lane(options: RunOptions) -> None:
    allowed = {"offline-structural", *_LIVE_LANES}
    if options.lane not in allowed:
        raise ValueError(f"未知 P08 lane：{options.lane}")
    if options.lane == "offline-structural":
        if options.live_provider or options.acknowledge_egress:
            raise ValueError("offline-structural 禁止 Live/Egress 标志。")
        return
    if not options.live_provider or not options.acknowledge_egress:
        raise LiveLaneBlockedError(
            "BLOCKED_MISSING_EXPLICIT_EGRESS_ACKNOWLEDGEMENT"
        )
    if not options.budget_requests or options.budget_requests <= 0:
        raise LiveLaneBlockedError(
            "BLOCKED_MISSING_POSITIVE_REQUEST_BUDGET"
        )
    if not options.budget_tokens or options.budget_tokens <= 0:
        raise LiveLaneBlockedError("BLOCKED_MISSING_POSITIVE_TOKEN_BUDGET")
    required = (
        ("JINA_API_KEY",)
        if options.lane == "live-primary"
        else (
            "DASHSCOPE_API_KEY",
            "ALIYUN_MODEL_STUDIO_WORKSPACE_ID",
            "ALIYUN_MODEL_STUDIO_REGION",
        )
    )
    missing = tuple(name for name in required if not os.environ.get(name))
    if missing:
        raise LiveLaneBlockedError(
            "BLOCKED_NO_CREDENTIALS:" + ",".join(sorted(missing))
        )


def _run_offline(options: RunOptions) -> RunOutcome:
    started_at = datetime.now(UTC)
    dataset = load_dataset_directory(options.dataset)
    identity_checks = validate_fixture_identities(dataset)
    requested_profile = load_profile(options.profile)
    run_id = options.run_id or _new_run_id(
        started_at, dataset.dataset_sha256, options.seed
    )
    run_directory = create_run_directory(options.reports_root, run_id)
    try:
        with tempfile.TemporaryDirectory(prefix="p08-evaluation-") as temporary:
            executions = _run_tuning_variants(
                dataset,
                requested_profile=requested_profile,
                temporary_root=Path(temporary),
            )
            tuning_reports = tuple(
                _metrics_for_execution(
                    execution,
                    split="tuning",
                    seed=options.seed,
                )
                for execution in executions
            )
            selected_identifier = select_tuning_candidate(tuning_reports)
            selected = next(
                variant
                for variant in offline_variants()
                if variant.variant_id == selected_identifier
            )
            holdout = execute_offline_variant(
                dataset,
                dataset.holdout_cases(),
                selected,
                requested_profile=requested_profile,
                data_directory=Path(temporary) / "holdout-selected",
            )
        holdout_report = _metrics_for_execution(
            holdout,
            split="holdout",
            seed=options.seed,
        )
        gate_report = evaluate_gates(
            holdout_report,
            load_gate_configuration(options.gates),
        )
        all_executions = (*executions, holdout)
        result_files = _write_results(
            run_directory,
            executions=all_executions,
            tuning_reports=tuning_reports,
            holdout_report=holdout_report,
            gate_report=gate_report,
            selected=selected,
            identity_checks=identity_checks,
        )
        manifest = _build_manifest(
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            dataset=dataset,
            profile_id="p08-offline",
            requested_profile_id=requested_profile.profile_id,
            executions=all_executions,
            selected=selected,
            seed=options.seed,
            result_files=result_files,
            gate_report=gate_report,
        )
        validate_manifest_safety(manifest)
        manifest_digest = write_json(run_directory / "manifest.json", manifest)
        write_text(
            run_directory / "MANIFEST.sha256",
            f"{manifest_digest.removeprefix('sha256:')}  manifest.json\n",
        )
        return RunOutcome(run_directory, manifest, gate_report)
    except Exception as error:
        _write_failure(run_directory, error)
        raise


def _run_tuning_variants(
    dataset: LoadedDataset,
    *,
    requested_profile: RagProfile,
    temporary_root: Path,
) -> tuple[VariantExecution, ...]:
    return tuple(
        execute_offline_variant(
            dataset,
            dataset.tuning_cases(),
            variant,
            requested_profile=requested_profile,
            data_directory=temporary_root / variant.variant_id,
        )
        for variant in offline_variants()
    )


def _metrics_for_execution(
    execution: VariantExecution,
    *,
    split: str,
    seed: int,
) -> MetricReport:
    report = compute_metric_report(
        execution.cases,
        execution.observations,
        context=MetricContext(
            lane="offline-structural",
            variant_id=execution.variant.variant_id,
            split=split,
            seed=seed,
        ),
    )
    build_seconds = execution.build_elapsed_ms / 1000.0
    engineering: dict[str, JsonValue] = {
        **report.engineering,
        "process_mode": "sync_in_process",
        "hardware_identity": {
            "system": platform.system(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
        },
        "ttft": {"status": "not_applicable_non_streaming"},
        "per_channel_latency": {"status": "not_instrumented"},
        "sqlite_query_count_time": {"status": "not_instrumented"},
        "qdrant_search_time": {"status": "not_applicable_memory_vector"},
        "peak_memory_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "index_build_elapsed_ms": execution.build_elapsed_ms,
        "index_build_chunk_throughput_per_second": (
            execution.chunk_count / build_seconds if build_seconds else None
        ),
        "artifact_blob_reuse_rate": {"status": "not_instrumented"},
    }
    return report.model_copy(update={"engineering": engineering})


def _write_results(  # noqa: PLR0913
    run_directory: Path,
    *,
    executions: tuple[VariantExecution, ...],
    tuning_reports: tuple[MetricReport, ...],
    holdout_report: MetricReport,
    gate_report: GateReport,
    selected: EvaluationVariant,
    identity_checks: dict[str, int],
) -> tuple[ResultFile, ...]:
    observations = tuple(
        item for execution in executions for item in execution.observations
    )
    errors = tuple(
        item for execution in executions for item in execution.errors
    )
    files: list[ResultFile] = []
    _record_jsonl(files, run_directory, "observations.jsonl", observations)
    _record_jsonl(files, run_directory, "errors.jsonl", errors)
    _record_json(
        files,
        run_directory,
        "tuning-metrics.json",
        {
            report.variant_id: report.model_dump(mode="json")
            for report in tuning_reports
        },
    )
    _record_json(
        files,
        run_directory,
        "selected-metrics.json",
        holdout_report,
    )
    _record_json(files, run_directory, "gates.json", gate_report)
    _record_json(
        files,
        run_directory,
        "ablations.json",
        _ablation_summary(
            executions[:-1],
            tuning_reports,
            selected.variant_id,
        ),
    )
    _record_json(
        files,
        run_directory,
        "selected-config.json",
        {
            "schema_version": "1",
            "status": "provisional_offline_only",
            "selected_variant": selected.variant_id,
            "parsing_policy": executions[-1].parsing_policy.model_dump(
                mode="json"
            ),
            "retrieval_policy": selected.retrieval_policy.model_dump(
                mode="json"
            ),
            "chunking_policy": selected.chunking_policy.model_dump(
                mode="json"
            ),
            "identity_checks": identity_checks,
            "fixture_catalog_sha256": fixture_catalog_sha256(),
        },
    )
    return tuple(files)


def _record_json(
    files: list[ResultFile],
    directory: Path,
    name: str,
    value: object,
) -> None:
    digest = write_json(directory / name, value)
    files.append(ResultFile(relative_path=name, sha256=digest))


def _record_jsonl(
    files: list[ResultFile],
    directory: Path,
    name: str,
    values: tuple[CaseObservation, ...] | tuple[ErrorRecord, ...],
) -> None:
    digest = write_jsonl(directory / name, values)
    files.append(ResultFile(relative_path=name, sha256=digest))


def _ablation_summary(
    executions: tuple[VariantExecution, ...],
    reports: tuple[MetricReport, ...],
    selected_identifier: str,
) -> dict[str, JsonValue]:
    report_by_variant = {report.variant_id: report for report in reports}
    return {
        "schema_version": "1",
        "split": "tuning",
        "selected_variant": selected_identifier,
        "selection_policy": {
            "labels": "tuning_only",
            "objective": (
                "recall_at_5 + refusal_f1 + citation_validity_rate + "
                "source_range_coverage - 2 * safety_penalties"
            ),
            "tie_break": "declared_variant_order",
        },
        "executed": [
            {
                "variant_id": execution.variant.variant_id,
                "changed_variable": execution.variant.changed_variable,
                "retrieval_policy": (
                    execution.variant.retrieval_policy.model_dump(mode="json")
                ),
                "chunking_policy": (
                    execution.variant.chunking_policy.model_dump(mode="json")
                ),
                "index_fingerprints": list(execution.index_fingerprints),
                "serving_fingerprints": list(
                    execution.serving_fingerprints
                ),
                "metrics": report_by_variant[
                    execution.variant.variant_id
                ].model_dump(mode="json"),
            }
            for execution in executions
        ],
        "blocked": {
            "dense-standby-only": "BLOCKED_LANE_C_NOT_AUTHORIZED",
            "rrf-jina-rerank": "BLOCKED_LANE_B_NOT_AUTHORIZED",
            "failover-quality": "BLOCKED_LANES_B_C_NOT_AUTHORIZED",
        },
    }


def _build_manifest(  # noqa: PLR0913
    *,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    dataset: LoadedDataset,
    profile_id: str,
    requested_profile_id: str,
    executions: tuple[VariantExecution, ...],
    selected: EvaluationVariant,
    seed: int,
    result_files: tuple[ResultFile, ...],
    gate_report: GateReport,
) -> RunManifest:
    selected_execution = executions[-1]
    providers = _unique_providers(selected_execution.providers)
    revisions = tuple(
        sorted(
            {
                identifier
                for execution in executions
                for identifier in execution.revision_ids
            }
        )
    )
    index_fingerprints = tuple(
        sorted(
            {
                value
                for execution in executions
                for value in execution.index_fingerprints
            }
        )
    )
    serving_fingerprints = tuple(
        sorted(
            {
                value
                for execution in executions
                for value in execution.serving_fingerprints
            }
        )
    )
    parameters: dict[str, JsonValue] = {
        "selected_status": "provisional_offline_only",
        "requested_profile_id": requested_profile_id,
        "parsing_policy": selected_execution.parsing_policy.model_dump(
            mode="json"
        ),
        "retrieval_policy": selected.retrieval_policy.model_dump(mode="json"),
        "chunking_policy": selected.chunking_policy.model_dump(mode="json"),
        "fixture_catalog_sha256": fixture_catalog_sha256(),
        "confidence_refusal_policy": {
            "id": "rule-confidence-v1",
            "minimum_ambiguous_evidence": 2,
            "provisional": True,
        },
        "generator_policy": {
            "id": "extractive-profile-v1",
            "citation_protocol": "support-ids-v1",
        },
        "gate_passed": gate_report.passed,
        "selected_index_revision_ids": list(
            selected_execution.revision_ids
        ),
        "selected_index_fingerprints": list(
            selected_execution.index_fingerprints
        ),
        "selected_serving_fingerprints": list(
            selected_execution.serving_fingerprints
        ),
        "ablation_variant_ids": [
            execution.variant.variant_id for execution in executions[:-1]
        ],
        "peak_memory_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    return RunManifest(
        run_id=run_id,
        state="complete",
        started_at=started_at,
        finished_at=finished_at,
        integration_sha=_git_head(),
        index_revision_ids=revisions,
        index_fingerprints=index_fingerprints,
        serving_fingerprints=serving_fingerprints,
        profile_id=profile_id,
        providers=providers,
        parser=selected_execution.parser,
        chunker=selected_execution.chunker,
        tokenizer_identities=selected_execution.tokenizer_identities,
        parameters=parameters,
        dataset_id=dataset.manifest.dataset_id,
        dataset_sha256=dataset.dataset_sha256,
        case_ids=tuple(case.case_id for case in dataset.cases),
        lane="offline-structural",
        seed=seed,
        network_authorization_mode="offline",
        external_services_actually_called=(),
        package_versions=_package_versions(),
        result_files=result_files,
        selected_candidate=selected.variant_id,
        holdout_access_count=1,
    )


def _unique_providers(
    providers: tuple[ProviderRunIdentity, ...],
) -> tuple[ProviderRunIdentity, ...]:
    unique = {item.model_dump_json(): item for item in providers}
    return tuple(unique[key] for key in sorted(unique))


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in _PACKAGE_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    return versions


def _git_head() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("P08 Manifest 无法找到 Git。")
    completed = subprocess.run(  # noqa: S603
        [executable, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _new_run_id(started: datetime, dataset_sha256: str, seed: int) -> str:
    timestamp = started.strftime("%Y%m%dT%H%M%SZ")
    suffix = hashlib.sha256(
        f"{dataset_sha256}:{seed}".encode()
    ).hexdigest()[:8]
    return f"p08-{timestamp}-{suffix}"


def _write_failure(directory: Path, error: Exception) -> None:
    target = directory / "FAILED.json"
    if target.exists():
        return
    write_json(
        target,
        {
            "state": "failed",
            "error_type": type(error).__name__,
            "safe_message": (
                "P08 run failed; inspect controlled terminal output."
            ),
        },
    )


__all__ = [
    "LiveLaneBlockedError",
    "RunOptions",
    "RunOutcome",
    "run_evaluation",
]
