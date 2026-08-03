"""从同次 calibration 证据原子生成冻结配置。"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _path in (_REPOSITORY_ROOT, _REPOSITORY_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evaluation.dataset import EvaluationDataset, load_dataset  # noqa: E402
from rag_app.contracts import PipelineSpec  # noqa: E402
from rag_app.freeze_evidence import (  # noqa: E402
    FreezeCandidate,
    FreezeCandidateConfig,
    FreezeDecision,
    FreezeEvidence,
    FreezeThresholds,
    ModelFleetIdentity,
    build_candidate_pipeline,
    canonical_tuning_digest,
    verify_model_fleet,
)
from rag_app.runtime import load_pipeline  # noqa: E402
from rag_app.settings import (  # noqa: E402
    ConfigurationState,
    RetrievalSettings,
)
from rag_app.strict_json import load_json_file  # noqa: E402
from scripts.freeze_corpus_manifest import (  # noqa: E402
    CorpusManifest,
    load_corpus_manifest,
)

__all__ = ["FreezeReleaseInputs", "freeze_release"]

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_STRUCTURAL_ZERO_FIELDS = (
    "standalone_heading_chunks",
    "cross_section_chunks",
    "cross_neighbor_group_links",
    "hard_max_violations",
    "uncovered_source_elements",
    "table_row_split_violations",
    "blank_chunks",
    "duplicate_chunk_ids",
    "ambiguous_quote_locator_cases",
    "quote_locator_contract_violations",
)
_RECALL_AT_20_REQUIRED = 0.95
_RERANK_RECALL_AT_5_REQUIRED = 0.90
_FINAL_HOLDOUT_CASES = 15


@dataclass(frozen=True, slots=True)
class FreezeReleaseInputs:
    """冻结命令的全部 operator 文件与候选输入。"""

    pipeline_path: Path
    retrieval_path: Path
    structural_report_path: Path
    retrieval_report_path: Path
    fleet_report_path: Path
    model_contract_directory: Path
    calibration_corpus_manifest_path: Path
    final_corpus_manifest_path: Path
    final_evaluation_manifest_path: Path
    output_directory: Path
    selected_candidate: str


class _CalibrationIdentity(BaseModel):
    """结构与检索报告共享的 calibration 身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"]
    calibration_source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    pipeline_file_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    pipeline_index_fingerprint: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    corpus_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    corpus_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_manifest_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )


class _RetrievalIdentity(_CalibrationIdentity):
    """真实检索报告额外绑定的配置、数据集与模型身份。"""

    retrieval_file_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    retrieval_serving_fingerprint: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    evaluation_dataset_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    tuning_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fleet: ModelFleetIdentity


class _CandidateRecord(BaseModel):
    """结构报告中的一个候选及聚合指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: str = Field(min_length=1, max_length=128)
    strategy: str = Field(min_length=1, max_length=128)
    config: FreezeCandidateConfig
    report: dict[str, object]


class _RetrievalCandidateRecord(_CandidateRecord):
    """真实检索候选的精确索引和服务指纹。"""

    index_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    serving_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class _OcrStates(BaseModel):
    """calibration 使用生产 OCR 后的元素状态计数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    succeeded: int = Field(ge=0)
    low_confidence: int = Field(ge=0)
    failed: int = Field(ge=0)
    pending: int = Field(ge=0)


class _StructuralReport(BaseModel):
    """由真实 DOCX 生成的结构消融报告。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["structural"]
    status: Literal["provisional_no_parameter_selection"]
    identity: _CalibrationIdentity
    documents: int = Field(gt=0)
    parser_counts: dict[str, object]
    candidates: tuple[_CandidateRecord, ...] = Field(min_length=1)


class _RetrievalReport(BaseModel):
    """同次模型与临时 collection 产生的 tuning 检索报告。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["retrieval"]
    split: Literal["tuning"]
    status: Literal["real_model_results_provisional"]
    identity: _RetrievalIdentity
    cases: int = Field(gt=0)
    ocr_calibrated: bool
    ocr_states: _OcrStates
    candidates: tuple[_RetrievalCandidateRecord, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _LoadedCalibration:
    """已完成严格解析的 calibration 输入。"""

    pipeline: PipelineSpec
    retrieval: RetrievalSettings
    structural: _StructuralReport
    retrieval_report: _RetrievalReport
    corpus: CorpusManifest


def freeze_release(
    inputs: FreezeReleaseInputs,
) -> FreezeDecision:
    """验证身份闭环并原子生成 pipeline、retrieval 与决策。

    Args:
        inputs: 全部 calibration、final、输出路径与入选候选。

    Returns:
        已写入输出目录的严格冻结决策。

    Raises:
        FileExistsError: 输出目录已存在或并发创建。
        ValueError: 任一身份、OCR、指标或最终数据集不满足冻结条件。

    """
    pipeline = load_pipeline(inputs.pipeline_path)
    retrieval = RetrievalSettings.load(inputs.retrieval_path)
    if (
        retrieval.status != ConfigurationState.PROVISIONAL
        or retrieval.freeze_decision_sha256 is not None
    ):
        raise ValueError("calibration retrieval 必须保持 provisional。")
    structural = _StructuralReport.model_validate(
        load_json_file(inputs.structural_report_path, label="structural report")
    )
    retrieval_report = _RetrievalReport.model_validate(
        load_json_file(inputs.retrieval_report_path, label="retrieval report")
    )
    calibration_corpus = load_corpus_manifest(
        inputs.calibration_corpus_manifest_path
    )
    final_corpus = load_corpus_manifest(inputs.final_corpus_manifest_path)
    final_dataset = load_dataset(inputs.final_evaluation_manifest_path)
    _validate_base_identities(
        inputs=inputs,
        loaded=_LoadedCalibration(
            pipeline=pipeline,
            retrieval=retrieval,
            structural=structural,
            retrieval_report=retrieval_report,
            corpus=calibration_corpus,
        ),
    )
    _validate_final_evidence(
        calibration_corpus=calibration_corpus,
        final_corpus=final_corpus,
        final_dataset=final_dataset,
        expected_tuning_digest=retrieval_report.identity.tuning_digest,
    )
    fleet = verify_model_fleet(
        inputs.fleet_report_path,
        inputs.model_contract_directory,
        pipeline=pipeline,
        calibration_source_revision=(
            retrieval_report.identity.calibration_source_revision
        ),
    )
    if fleet != retrieval_report.identity.fleet:
        raise ValueError("retrieval 报告的 fleet 身份与原始报告目录不一致。")
    selected = _validate_selected_candidate(
        structural,
        retrieval_report,
        inputs.selected_candidate,
    )
    candidate_pipeline = build_candidate_pipeline(
        pipeline,
        selected.config,
        strategy=selected.strategy,
        fleet=fleet,
    )
    retrieval_candidate = _find_retrieval_candidate(
        retrieval_report.candidates,
        inputs.selected_candidate,
    )
    expected_serving = retrieval.serving_fingerprint(candidate_pipeline)
    if (
        retrieval_candidate.index_fingerprint
        != candidate_pipeline.index_fingerprint()
    ):
        raise ValueError("入选候选的 index fingerprint 与实测报告不一致。")
    if retrieval_candidate.serving_fingerprint != expected_serving:
        raise ValueError("入选候选的 serving fingerprint 与实测报告不一致。")
    thresholds = _thresholds(retrieval_candidate)
    decision = FreezeDecision(
        attempt_id=fleet.attempt_id,
        index_fingerprint=candidate_pipeline.index_fingerprint(),
        serving_fingerprint=expected_serving,
        model_revisions=fleet.model_revisions,
        selected_candidate=FreezeCandidate(
            label=selected.candidate,
            strategy="section_pack_v2",
            chunker_revision=candidate_pipeline.chunker_revision,
            config=selected.config,
        ),
        thresholds=thresholds,
        evidence=FreezeEvidence(
            pipeline_file_sha256=_sha256_file(inputs.pipeline_path),
            retrieval_file_sha256=_sha256_file(inputs.retrieval_path),
            calibration_corpus_manifest_sha256=(
                _sha256_file(inputs.calibration_corpus_manifest_path)
            ),
            final_corpus_manifest_sha256=(
                _sha256_file(inputs.final_corpus_manifest_path)
            ),
            final_evaluation_manifest_sha256=(
                _sha256_file(inputs.final_evaluation_manifest_path)
            ),
            final_corpus_id=final_corpus.corpus_id,
            final_corpus_digest=final_corpus.corpus_digest,
            tuning_digest=retrieval_report.identity.tuning_digest,
            calibration_structural_report_sha256=(
                _sha256_file(inputs.structural_report_path)
            ),
            calibration_retrieval_report_sha256=(
                _sha256_file(inputs.retrieval_report_path)
            ),
            model_contract_reports=fleet.reports,
            model_contract_summary_sha256=fleet.summary_sha256,
        ),
    )
    frozen_retrieval = RetrievalSettings.model_validate(
        {
            **retrieval.model_dump(mode="json"),
            "status": ConfigurationState.FROZEN,
            "freeze_decision_sha256": decision.sha256(),
        }
    )
    if (
        frozen_retrieval.serving_fingerprint(candidate_pipeline)
        != expected_serving
    ):
        raise ValueError("frozen retrieval 产生了循环或指纹漂移。")
    _write_frozen_bundle(
        inputs.output_directory,
        pipeline=candidate_pipeline,
        retrieval=frozen_retrieval,
        decision=decision,
    )
    return decision


def _validate_base_identities(
    *,
    inputs: FreezeReleaseInputs,
    loaded: _LoadedCalibration,
) -> None:
    pipeline_fingerprint = loaded.pipeline.index_fingerprint()
    base_expected = {
        "schema_version": "1",
        "calibration_source_revision": (
            loaded.retrieval_report.identity.calibration_source_revision
        ),
        "pipeline_file_sha256": _sha256_file(inputs.pipeline_path),
        "pipeline_index_fingerprint": pipeline_fingerprint,
        "corpus_id": loaded.corpus.corpus_id,
        "corpus_digest": loaded.corpus.corpus_digest,
        "corpus_manifest_sha256": _sha256_file(
            inputs.calibration_corpus_manifest_path
        ),
    }
    if loaded.structural.identity.model_dump(mode="json") != base_expected:
        raise ValueError("structural report 的 calibration 身份不一致。")
    retrieval_base = {
        key: value
        for key, value in loaded.retrieval_report.identity.model_dump(
            mode="json"
        ).items()
        if key in base_expected
    }
    if retrieval_base != base_expected:
        raise ValueError("retrieval report 的 calibration 身份不一致。")
    if loaded.retrieval_report.identity.retrieval_file_sha256 != _sha256_file(
        inputs.retrieval_path
    ):
        raise ValueError("retrieval report 的配置文件 SHA256 不一致。")
    if (
        loaded.retrieval_report.identity.retrieval_serving_fingerprint
        != loaded.retrieval.serving_fingerprint(loaded.pipeline)
    ):
        raise ValueError("retrieval report 的基础 serving fingerprint 不一致。")
    states = loaded.retrieval_report.ocr_states
    if (
        not loaded.retrieval_report.ocr_calibrated
        or states.succeeded <= 0
        or states.pending != 0
        or states.failed != 0
    ):
        raise ValueError("retrieval calibration 未完成真实 OCR。")


def _validate_final_evidence(
    *,
    calibration_corpus: CorpusManifest,
    final_corpus: CorpusManifest,
    final_dataset: EvaluationDataset,
    expected_tuning_digest: str,
) -> None:
    if calibration_corpus.corpus_digest != final_corpus.corpus_digest:
        raise ValueError("calibration 与 final DOCX corpus_digest 不一致。")
    if calibration_corpus.corpus_id == final_corpus.corpus_id:
        raise ValueError("final corpus 必须使用新的 corpus_id。")
    manifest_paths = {document.path for document in final_corpus.documents}
    dataset_paths = tuple(final_dataset.documents.values())
    if (
        len(set(dataset_paths)) != len(dataset_paths)
        or set(dataset_paths) != manifest_paths
    ):
        raise ValueError("final dataset 文档映射与 corpus exact set 不一致。")
    final_tuning_digest = canonical_tuning_digest(
        final_dataset.documents,
        tuple(case.model_dump(mode="json") for case in final_dataset.cases),
    )
    if final_tuning_digest != expected_tuning_digest:
        raise ValueError("final dataset 的 tuning 内容发生漂移。")
    holdout = tuple(
        case for case in final_dataset.cases if case.split == "holdout"
    )
    blocked = tuple(
        case.id for case in holdout if case.validation_state != "verified_text"
    )
    if len(holdout) != _FINAL_HOLDOUT_CASES or blocked:
        raise ValueError("final dataset 必须恰含 15 个已核验 holdout。")


def _validate_selected_candidate(
    structural: _StructuralReport,
    retrieval: _RetrievalReport,
    selected: str,
) -> _CandidateRecord:
    structural_candidate = _find_candidate(
        structural.candidates,
        selected,
        label="structural",
    )
    retrieval_candidate = _find_retrieval_candidate(
        retrieval.candidates,
        selected,
    )
    if (
        structural_candidate.strategy != retrieval_candidate.strategy
        or structural_candidate.config != retrieval_candidate.config
    ):
        raise ValueError("structural 与 retrieval 的入选候选身份不一致。")
    if structural_candidate.strategy != "section_pack_v2":
        raise ValueError("冻结候选必须使用 section_pack_v2。")
    report = structural_candidate.report
    if not _is_positive_int(report.get("chunks")):
        raise ValueError("structural 入选候选没有有效 chunk。")
    if report.get("source_coverage_ratio") != 1.0:
        raise ValueError("structural 入选候选没有完整来源覆盖。")
    failures = [
        field for field in _STRUCTURAL_ZERO_FIELDS if report.get(field) != 0
    ]
    if failures:
        raise ValueError("structural 入选候选违反结构不变量。")
    return structural_candidate


def _find_candidate(
    candidates: tuple[_CandidateRecord, ...],
    selected: str,
    *,
    label: str,
) -> _CandidateRecord:
    matched = tuple(item for item in candidates if item.candidate == selected)
    if len(matched) != 1:
        raise ValueError(f"{label} report 必须恰有一个入选候选。")
    return matched[0]


def _find_retrieval_candidate(
    candidates: tuple[_RetrievalCandidateRecord, ...],
    selected: str,
) -> _RetrievalCandidateRecord:
    matched = tuple(item for item in candidates if item.candidate == selected)
    if len(matched) != 1:
        raise ValueError("retrieval report 必须恰有一个入选候选。")
    return matched[0]


def _thresholds(candidate: _RetrievalCandidateRecord) -> FreezeThresholds:
    overall = candidate.report.get("overall")
    if not isinstance(overall, dict):
        raise ValueError("retrieval 入选候选缺少 overall 指标。")
    recall = _metric(overall, "recall_at_20")
    rerank = _metric(overall, "rerank_recall_at_5")
    if recall < _RECALL_AT_20_REQUIRED:
        raise ValueError("recall_at_20 未达到 0.95。")
    if rerank < _RERANK_RECALL_AT_5_REQUIRED:
        raise ValueError("rerank_recall_at_5 未达到 0.90。")
    return FreezeThresholds(
        recall_at_20=recall,
        recall_at_20_passed=True,
        rerank_recall_at_5=rerank,
        rerank_recall_at_5_passed=True,
    )


def _metric(payload: dict[str, object], name: str) -> float:
    value = payload.get(name)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"retrieval {name} 必须是 0 到 1 的数值。")
    return float(value)


def _write_frozen_bundle(
    output: Path,
    *,
    pipeline: PipelineSpec,
    retrieval: RetrievalSettings,
    decision: FreezeDecision,
) -> None:
    if output.exists() or output.is_symlink():
        raise FileExistsError("冻结输出目录已存在，拒绝覆盖。")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(dir=output.parent, prefix=f".{output.name}.freeze-")
    )
    published = False
    try:
        files = {
            "pipeline.json": pipeline.model_dump(mode="json"),
            "retrieval.json": retrieval.model_dump(mode="json"),
            "FREEZE_DECISION.json": decision.model_dump(mode="json"),
        }
        rendered = {
            name: _render_json(value) for name, value in files.items()
        }
        combined = b"\n".join(rendered.values()).decode("utf-8")
        lowered = combined.casefold()
        if "endpoint" in lowered or "api_token" in lowered:
            raise ValueError("冻结输出意外包含 endpoint 或 token 字段。")
        for name, content in rendered.items():
            _write_new_file(stage / name, content)
        _publish_directory_no_replace(stage, output)
        published = True
    finally:
        if not published and stage.exists():
            shutil.rmtree(stage)


def _render_json(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_new_file(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            "冻结输出目录已存在，拒绝覆盖。",
            destination,
        )
    raise OSError(error_number, os.strerror(error_number), destination)


def _sha256_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise ValueError("冻结证据必须是普通文件。")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--retrieval", type=Path, required=True)
    parser.add_argument("--structural-report", type=Path, required=True)
    parser.add_argument("--retrieval-report", type=Path, required=True)
    parser.add_argument("--fleet-report", type=Path, required=True)
    parser.add_argument("--model-contract-directory", type=Path, required=True)
    parser.add_argument("--calibration-corpus", type=Path, required=True)
    parser.add_argument("--final-corpus", type=Path, required=True)
    parser.add_argument("--final-evaluation", type=Path, required=True)
    parser.add_argument("--selected-candidate", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """执行严格冻结命令。

    Args:
        无参数；从命令行读取全部 calibration 与 final 证据路径。

    Returns:
        成功时返回 0。

    """
    arguments = _arguments()
    decision = freeze_release(
        FreezeReleaseInputs(
            pipeline_path=arguments.pipeline,
            retrieval_path=arguments.retrieval,
            structural_report_path=arguments.structural_report,
            retrieval_report_path=arguments.retrieval_report,
            fleet_report_path=arguments.fleet_report,
            model_contract_directory=arguments.model_contract_directory,
            calibration_corpus_manifest_path=(
                arguments.calibration_corpus
            ),
            final_corpus_manifest_path=arguments.final_corpus,
            final_evaluation_manifest_path=arguments.final_evaluation,
            output_directory=arguments.output,
            selected_candidate=arguments.selected_candidate,
        )
    )
    print(f"freeze_decision_sha256={decision.sha256()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
