"""V3-02C 固定 Chunk 基线的构建、指标和边界回归。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import evaluation.v3_02_baseline as baseline_module
from evaluation.v3_02_baseline import (
    B0_EXECUTION_REVISION,
    B0_SOURCE_REVISION,
    B1_LABEL_REVISION,
    TRACE_MODE_ORDER,
    ActiveRevision,
    CacheCondition,
    ChannelCandidates,
    ComponentIdentity,
    EmbeddingIdentity,
    FixedChunkCase,
    FrozenBaselineIdentity,
    FrozenBuild,
    FrozenChunk,
    FrozenDocument,
    HardwareIdentity,
    ProcessIdentity,
    ProfileIdentity,
    PublicSlice,
    QueryMeasurement,
    SourceIdentity,
    StageDuration,
    TraceMeasurement,
    UpstreamGateEvidence,
    create_baseline_bundle,
    freeze_chunk_set,
    run_b1_fixed_chunk_baseline,
    run_concrete_fixed_chunk_baseline,
    validate_frozen_b0_manifest,
    write_baseline_bundle,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.tracing.models import TraceMode

_CHUNK_ALPHA = "chunk_alpha_0001"
_CHUNK_BETA = "chunk_beta_0002"


def _digest(value: object) -> str:
    return canonical_sha256(value)


def _cases() -> tuple[FixedChunkCase, ...]:
    slices: tuple[tuple[str, PublicSlice, str], ...] = (
        ("case_exact", "exact", "标准号 ZX-42 是什么"),
        ("case_enumeration", "enumeration", "列举维护步骤"),
        ("case_list", "multilevel_list", "二级职责有哪些"),
        ("case_table", "table", "ZX-42 同行的单位是什么"),
        ("case_colloquial", "colloquial_synonym", "这东西咋维护"),
        (
            "case_numeric",
            "numeric_unit_negation_exception",
            "除低温例外外，仅限 12.5 kg 吗",
        ),
        ("case_ocr", "ocr_only", "图中 OCR-7 的数值"),
        (
            "case_relation",
            "accepted_relation_consumer",
            "已接纳箭头从哪个节点指向哪个节点",
        ),
    )
    answerable = tuple(
        FixedChunkCase(
            case_id=case_id,
            slice_id=slice_id,
            query=query,
            relevant_chunk_ids=(_CHUNK_ALPHA,),
            answerable=True,
        )
        for case_id, slice_id, query in slices
    )
    return (
        *answerable,
        FixedChunkCase(
            case_id="case_unanswerable",
            slice_id="unanswerable",
            query="不存在的公开合成事实",
            relevant_chunk_ids=(),
            answerable=False,
        ),
    )


def _identity() -> FrozenBaselineIdentity:
    chunks = freeze_chunk_set(
        (
            FrozenChunk(
                chunk_id=_CHUNK_ALPHA,
                content_sha256=_digest("alpha-content"),
                source_spans_sha256=_digest("alpha-spans"),
            ),
            FrozenChunk(
                chunk_id=_CHUNK_BETA,
                content_sha256=_digest("beta-content"),
                source_spans_sha256=_digest("beta-spans"),
            ),
        )
    )
    return FrozenBaselineIdentity(
        parser=ComponentIdentity(
            component_id="word-document-v1",
            version="1",
            policy_sha256=_digest("parsing-policy"),
        ),
        chunker=ComponentIdentity(
            component_id="docx-structural-v3",
            version="3.0.2",
            policy_sha256=_digest("chunking-policy"),
        ),
        chunks=chunks,
        active_revisions=(
            ActiveRevision(
                project_id="prj_public",
                knowledge_base_id="kb_public",
                revision_id="irev_public",
            ),
        ),
        embedding_slots=(
            EmbeddingIdentity(
                slot_id="primary",
                vector_name="dense_primary",
                provider_id="deterministic",
                model="deterministic-embedding-v1",
                dimension=32,
                adapter_revision="deterministic-v1",
                request_policy_sha256=_digest("request-policy"),
            ),
        ),
        profile=ProfileIdentity(
            profile_id="product-runtime",
            profile_sha256=_digest("profile"),
            retrieval_policy_sha256=_digest("retrieval-policy"),
            generation_policy_sha256=_digest("generation-policy"),
            cache_identity="retrieval-cache-v1",
            index_fingerprints=(_digest("index"),),
            serving_fingerprints=(_digest("serving"),),
        ),
        hardware=HardwareIdentity(
            system="Linux",
            machine="x86_64",
            cpu_model="public-synthetic-cpu",
            logical_cpu_count=8,
            memory_bytes=16 * 1024**3,
            accelerator="none",
            hardware_fingerprint=_digest("hardware"),
        ),
        process=ProcessIdentity(
            process_id=1234,
            python_version="3.11.15",
            executable_sha256=_digest("python"),
            dependency_lock_sha256=_digest("lock"),
            image_identity="source-tree",
            process_fingerprint=_digest("process"),
        ),
        source=SourceIdentity(
            execution_source_revision="a" * 40,
            label_source_revision=B1_LABEL_REVISION,
            dataset_id="v3-02-public-synthetic",
            dataset_version="1.0.0",
            corpus_sha256=_digest("corpus"),
            documents=(
                FrozenDocument(
                    document_id="doc_public",
                    content_sha256=_digest("document"),
                ),
            ),
        ),
    )


@dataclass(slots=True)
class _FakeProductRuntime:
    """模拟同一 Product 查询链，不在测试中复制基线编排。"""

    identity: FrozenBaselineIdentity = field(default_factory=_identity)
    build_calls: int = 0
    reset_modes: list[TraceMode] = field(default_factory=list)
    extra_provider_mode: TraceMode | None = None
    drift_mode: TraceMode | None = None
    external_mode: TraceMode | None = None

    def build_once(self) -> FrozenBuild:
        self.build_calls += 1
        return FrozenBuild(identity=self.identity, build_duration_ns=10_000)

    def reset_query_cache(self, mode: TraceMode) -> None:
        self.reset_modes.append(mode)

    def execute(
        self,
        case: FixedChunkCase,
        mode: TraceMode,
        cache_condition: CacheCondition,
    ) -> QueryMeasurement:
        cold = cache_condition == "cold"
        provider_calls = int(cold)
        if mode is self.extra_provider_mode:
            provider_calls += 1
        mode_cost = {
            TraceMode.SAFE: 0,
            TraceMode.DIAGNOSTIC: 200,
            TraceMode.FULL: 500,
        }[mode]
        trace_units = {
            TraceMode.SAFE: 1,
            TraceMode.DIAGNOSTIC: 2,
            TraceMode.FULL: 3,
        }[mode]
        active_revisions = tuple(
            item.revision_id for item in self.identity.active_revisions
        )
        if mode is self.drift_mode:
            active_revisions = ("irev_drift",)
        total_duration_ns = (500 if not cold else 1_000) + mode_cost
        retrieval_duration_ns = 400 if not cold else 800
        return QueryMeasurement(
            case_id=case.case_id,
            mode=mode,
            cache_condition=cache_condition,
            chunk_digest=self.identity.chunks.digest,
            active_revision_ids=active_revisions,
            index_fingerprints=self.identity.profile.index_fingerprints,
            serving_fingerprints=self.identity.profile.serving_fingerprints,
            profile_sha256=self.identity.profile.profile_sha256,
            channel_candidates=(
                ChannelCandidates(
                    channel="exact",
                    chunk_ids=case.relevant_chunk_ids,
                ),
            ),
            evidence_chunk_ids=case.relevant_chunk_ids,
            cited_chunk_ids=case.relevant_chunk_ids,
            answered=case.answerable,
            answer_correct=True if case.answerable else None,
            refusal_reason=None if case.answerable else "INSUFFICIENT_EVIDENCE",
            total_duration_ns=total_duration_ns,
            retrieval_duration_ns=retrieval_duration_ns,
            trace_capture_overhead_ns=(
                total_duration_ns - retrieval_duration_ns
            ),
            provider_duration_ns=200 if cold else 0,
            stage_durations=(
                StageDuration(stage="retrieve", duration_ns=300 + mode_cost),
                StageDuration(stage="generate", duration_ns=200 if cold else 0),
            ),
            provider_call_count=provider_calls,
            provider_retry_count=0,
            observed_tokens=4 * provider_calls,
            estimated_cost_microunits=0,
            external_service_call_count=int(mode is self.external_mode),
            cache_hit=not cold,
            singleflight_hit=False,
            retrieval_execution_count=int(cold),
            generation_execution_count=int(cold and case.answerable),
            trace=TraceMeasurement(
                submitted_count=trace_units,
                written_count=trace_units,
                dropped_count=0,
                queue_high_water=trace_units,
                storage_bytes=100 * trace_units,
                artifact_count=int(mode is TraceMode.FULL),
                artifact_bytes=256 if mode is TraceMode.FULL else 0,
                synchronous_artifact_read_count=0,
            ),
        )


def test_b1_builds_once_and_reuses_one_chunk_identity() -> None:
    runtime = _FakeProductRuntime()

    report = run_b1_fixed_chunk_baseline(runtime, _cases())

    assert runtime.build_calls == 1
    assert runtime.reset_modes == list(TRACE_MODE_ORDER)
    assert len(report.results) == len(_cases()) * 3 * 2
    assert {result.measurement.chunk_digest for result in report.results} == {
        runtime.identity.chunks.digest
    }
    assert report.quality_equivalent_across_trace_modes
    assert report.provider_calls_equivalent_across_trace_modes
    assert report.external_services_actually_called == ()
    assert not report.real_provider_quality_claimed
    assert not report.real_visual_quality_claimed


def test_metrics_cover_quality_performance_provider_cache_and_trace() -> None:
    report = run_b1_fixed_chunk_baseline(_FakeProductRuntime(), _cases())
    metrics = {
        (item.mode, item.cache_condition): item for item in report.mode_metrics
    }

    safe_cold = metrics[(TraceMode.SAFE, "cold")]
    diagnostic_cold = metrics[(TraceMode.DIAGNOSTIC, "cold")]
    full_cold = metrics[(TraceMode.FULL, "cold")]
    safe_warm = metrics[(TraceMode.SAFE, "warm")]
    assert safe_cold.evidence_recall_at_5 == 1.0
    assert safe_cold.citation_accuracy == 1.0
    assert safe_cold.answer_accuracy == 1.0
    assert safe_cold.erroneous_refusal_rate == 0.0
    assert safe_cold.erroneous_answer_rate == 0.0
    assert safe_cold.provider_call_count == len(_cases())
    assert safe_warm.provider_call_count == 0
    assert safe_warm.cache_hit_count == len(_cases())
    assert diagnostic_cold.p95_added_vs_safe_ns == 200
    assert full_cold.p95_added_vs_safe_ns == 500
    assert safe_cold.p95_trace_capture_overhead_ns == 200
    assert diagnostic_cold.p95_trace_capture_overhead_ns == 400
    assert full_cold.p95_trace_capture_overhead_ns == 700
    assert safe_cold.trace_artifact_count == 0
    assert full_cold.trace_artifact_count == len(_cases())
    assert full_cold.trace_storage_bytes > diagnostic_cold.trace_storage_bytes
    assert safe_cold.channel_candidate_count["exact"] == len(_cases()) - 1


def test_failed_upstream_quality_gate_is_frozen_without_becoming_pass() -> None:
    report = run_b1_fixed_chunk_baseline(
        _FakeProductRuntime(),
        _cases(),
        upstream_gates=(
            UpstreamGateEvidence(
                gate_id="evaluation_v3_candidate_selection",
                status="FAIL",
                reason="没有候选通过 Evaluation V3 安全与精度门。",
                metrics={
                    "citation_precision": 0.56818,
                    "source_range_recall": 0.68965,
                },
            ),
        ),
    )

    assert report.status == "FAIL"
    assert report.upstream_gates[0].status == "FAIL"
    gate = next(
        item
        for item in report.gates
        if item.gate_id == "evaluation_v3_candidate_selection"
    )
    assert not gate.passed
    assert gate.observed == "FAIL"
    bundle = create_baseline_bundle(report)
    assert json.loads(bundle.manifest_json)["report_status"] == "FAIL"


def test_trace_mode_cannot_add_provider_calls() -> None:
    runtime = _FakeProductRuntime(extra_provider_mode=TraceMode.DIAGNOSTIC)

    with pytest.raises(ValueError, match="Provider 调用"):
        run_b1_fixed_chunk_baseline(runtime, _cases())


def test_query_identity_cannot_drift_between_trace_modes() -> None:
    runtime = _FakeProductRuntime(drift_mode=TraceMode.FULL)

    with pytest.raises(ValueError, match="固定身份发生漂移"):
        run_b1_fixed_chunk_baseline(runtime, _cases())


def test_offline_baseline_rejects_external_service_calls() -> None:
    runtime = _FakeProductRuntime(external_mode=TraceMode.SAFE)

    with pytest.raises(ValueError, match="禁止调用外部服务"):
        run_b1_fixed_chunk_baseline(runtime, _cases())


def test_all_required_public_slices_must_be_present() -> None:
    cases = tuple(case for case in _cases() if case.slice_id != "ocr_only")

    with pytest.raises(ValueError, match="ocr_only"):
        run_b1_fixed_chunk_baseline(_FakeProductRuntime(), cases)


def test_case_labels_must_reference_frozen_chunks() -> None:
    cases = list(_cases())
    cases[0] = cases[0].model_copy(
        update={"relevant_chunk_ids": ("chunk_unknown_9999",)}
    )

    with pytest.raises(ValueError, match="未知固定 Chunk"):
        run_b1_fixed_chunk_baseline(_FakeProductRuntime(), cases)


def test_b0_accepts_only_pre_v3_02_frozen_source() -> None:
    digest = _digest("b0-manifest")

    reference = validate_frozen_b0_manifest(
        {
            "baseline_id": "B0",
            "entry_source_revision": B0_SOURCE_REVISION,
            "execution_source_revision": B0_EXECUTION_REVISION,
            "label_source_revision": B1_LABEL_REVISION,
            "manifest_sha256": digest,
        }
    )

    assert reference.entry_source_revision == B0_SOURCE_REVISION
    assert reference.execution_source_revision == B0_EXECUTION_REVISION
    assert reference.manifest_sha256 == digest
    with pytest.raises(ValueError, match="0505fac"):
        validate_frozen_b0_manifest(
            {
                "baseline_id": "B0",
                "entry_source_revision": "b" * 40,
                "execution_source_revision": B0_EXECUTION_REVISION,
                "label_source_revision": B1_LABEL_REVISION,
                "manifest_sha256": digest,
            }
        )


def test_canonical_bundle_is_deterministic_and_manifest_is_verifiable(
    tmp_path: Path,
) -> None:
    report = run_b1_fixed_chunk_baseline(_FakeProductRuntime(), _cases())

    first = create_baseline_bundle(report)
    second = create_baseline_bundle(report)
    assert first == second
    assert first.report_sha256 == (
        "sha256:" + hashlib.sha256(first.report_json).hexdigest()
    )
    assert first.manifest_sha256 == (
        "sha256:" + hashlib.sha256(first.manifest_json).hexdigest()
    )
    manifest = json.loads(first.manifest_json)
    assert manifest["report_sha256"] == first.report_sha256
    assert manifest["fixed_chunk_digest"] == report.build.identity.chunks.digest
    assert manifest["b0_entry_source_revision"] == B0_SOURCE_REVISION
    assert manifest["b0_execution_source_revision"] == B0_EXECUTION_REVISION
    assert manifest["b1_label_source_revision"] == B1_LABEL_REVISION

    output = tmp_path / "fixed-chunk-baseline"
    write_baseline_bundle(output, first)
    assert (output / "report.json").read_bytes() == first.report_json
    assert (output / "manifest.json").read_bytes() == first.manifest_json
    assert (output / "MANIFEST.sha256").read_bytes() == first.checksums
    with pytest.raises(FileExistsError):
        write_baseline_bundle(output, first)


def test_concrete_runner_writes_52_case_and_trace_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 runner 即使门禁失败也落盘 canonical bundle。"""
    monkeypatch.setattr(
        baseline_module,
        "_validate_execution_revisions",
        lambda _b0, _b1: None,
    )
    failure = {
        "error_type": "ValueError",
        "safe_message": "P08 candidate selection failed.",
        "state": "failed",
    }
    b0_failure = tmp_path / "b0-failed.json"
    b1_failure = tmp_path / "b1-failed.json"
    b0_failure.write_text(json.dumps(failure), "utf-8")
    b1_failure.write_text(json.dumps(failure), "utf-8")
    output = tmp_path / "concrete-output"

    bundle = run_concrete_fixed_chunk_baseline(
        output_directory=output,
        dataset_directory=Path("evaluation/datasets/synthetic"),
        profile_path=Path("configs/profiles/dev-offline.json"),
        gate_path=Path("evaluation/gates/p08-gates.json"),
        b0_revision=B0_SOURCE_REVISION,
        execution_revision="a" * 40,
        upstream_b0_failure=b0_failure,
        upstream_b1_failure=b1_failure,
    )

    report = json.loads(bundle.report_json)
    assert report["production_retrieval"]["execution_path"] == (
        "production_retrieval_path"
    )
    assert report["production_retrieval"]["case_count"] == 52
    assert report["production_retrieval"]["build_execution_count"] == 1
    assert report["production_retrieval"]["status"] == "FAIL"
    assert len(report["results"]) == 9 * 3 * 2
    assert report["status"] == "FAIL"
    safe_results = [
        item
        for item in report["results"]
        if item["measurement"]["mode"] == "SAFE"
    ]
    assert all(
        item["measurement"]["trace"]["artifact_count"] == 0
        and item["measurement"]["trace"]["artifact_bytes"] == 0
        for item in safe_results
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in report["results"]:
        measurement = item["measurement"]
        key = (item["case_id"], measurement["cache_condition"])
        grouped.setdefault(key, []).append(measurement)
    for measurements in grouped.values():
        assert len(measurements) == 3
        assert (
            len(
                {
                    (
                        json.dumps(item["channel_candidates"], sort_keys=True),
                        tuple(item["evidence_chunk_ids"]),
                        tuple(item["cited_chunk_ids"]),
                        item["answered"],
                        item["answer_correct"],
                        item["refusal_reason"],
                    )
                    for item in measurements
                }
            )
            == 1
        )
        assert (
            len(
                {
                    (item["provider_call_count"], item["provider_retry_count"])
                    for item in measurements
                }
            )
            == 1
        )
    consumer = report["trace_consumer"]
    assert consumer["execution_path"] == "synthetic_trace_consumer"
    assert {item["slice_id"] for item in consumer["contracts"]} == {
        "exact",
        "enumeration",
        "multilevel_list",
        "table",
        "colloquial_synonym",
        "numeric_unit_negation_exception",
        "unanswerable",
        "ocr_only",
        "accepted_relation_consumer",
    }
    assert all(
        not item["product_quality_claimed"] for item in consumer["contracts"]
    )
    metrics = {
        (item["mode"], item["cache_condition"]): item
        for item in report["mode_metrics"]
    }
    for mode in ("SAFE", "DIAGNOSTIC", "FULL"):
        cold = metrics[(mode, "cold")]
        warm = metrics[(mode, "warm")]
        for field_name in (
            "evidence_recall_at_5",
            "citation_accuracy",
            "answer_accuracy",
            "erroneous_refusal_rate",
            "erroneous_answer_rate",
        ):
            assert cold[field_name] == warm[field_name]
    for cache_condition in ("cold", "warm"):
        safe = metrics[("SAFE", cache_condition)]
        diagnostic = metrics[("DIAGNOSTIC", cache_condition)]
        full = metrics[("FULL", cache_condition)]
        assert safe["trace_storage_bytes"] < diagnostic["trace_storage_bytes"]
        assert diagnostic["trace_storage_bytes"] < full["trace_storage_bytes"]
    assert (output / "report.json").read_bytes() == bundle.report_json
    assert (output / "manifest.json").read_bytes() == bundle.manifest_json
    assert (output / "MANIFEST.sha256").read_bytes() == bundle.checksums
