from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evaluation.dataset import load_dataset
from evaluation.freeze_release import FreezeReleaseInputs, freeze_release
from rag_app.freeze_evidence import (
    FreezeCandidateConfig,
    FreezeDecision,
    RetrievalModelEndpoints,
    build_candidate_pipeline,
    canonical_tuning_digest,
    verify_model_fleet,
)
from rag_app.runtime import load_pipeline
from rag_app.settings import RetrievalSettings
from rag_app.worker_runtime import require_indexable_configuration
from scripts.freeze_corpus_manifest import freeze_corpus_manifest
from tests.synthetic_evaluation import (
    write_synthetic_dataset,
    write_synthetic_evidence_docx,
)

_MODEL_REPORT_NAMES = (
    "model-contract-embedding.json",
    "model-contract-reranker.json",
    "model-contract-llm-1.json",
    "model-contract-llm-2.json",
    "model-contract-llm-3.json",
    "model-contract-llm-4.json",
)
_ATTEMPT_ID = "1" * 32
_CALIBRATION_REVISION = "2" * 40
_SELECTED = "section-pack-v2-384-512-64"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _model_probe(service: str) -> dict[str, object]:
    if service == "embedding":
        return {
            "count": 2,
            "dimension": 1024,
            "indexes": [0, 1],
            "finite": True,
        }
    if service == "reranker":
        return {
            "count": 2,
            "indexes": [0, 1],
            "score_range": [0.0, 1.0],
        }
    return {
        "rewrite": {},
        "answer_initial_max": {},
        "answer_repair_max": {},
        "temperature": 0,
        "thinking_enabled": False,
    }


def _write_model_reports(directory: Path) -> Path:
    directory.mkdir()
    services = ("embedding", "reranker", "llm", "llm", "llm", "llm")
    models = (
        "Qwen3-Embedding-0.6B",
        "Qwen3-Reranker-0.6B",
        "Qwen/Qwen3-8B-AWQ",
        "Qwen/Qwen3-8B-AWQ",
        "Qwen/Qwen3-8B-AWQ",
        "Qwen/Qwen3-8B-AWQ",
    )
    for index, (name, service, model) in enumerate(
        zip(_MODEL_REPORT_NAMES, services, models, strict=True),
        start=1,
    ):
        report = {
            "schema_version": "1",
            "status": "passed",
            "service": service,
            "endpoint": f"http://{service}-{index}.internal:8000",
            "model": model,
            "endpoint_revision": f"{service}-revision-{index}",
            "revision_source": "endpoint",
            "health": "passed",
            "model_id": "passed",
            "probe": _model_probe(service),
        }
        (directory / name).write_text(
            json.dumps(report, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    summary_reports = []
    for name, service in sorted(
        zip(_MODEL_REPORT_NAMES, services, strict=True),
        key=lambda item: item[0],
    ):
        summary_reports.append(
            {
                "name": name,
                "service": service,
                "sha256": _sha256_file(directory / name),
            }
        )
    summary_path = directory / "FLEET_REPORT.json"
    summary_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "attempt_id": _ATTEMPT_ID,
                "source_revision": _CALIBRATION_REVISION,
                "status": "passed",
                "reports": summary_reports,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary_path


def _write_synthetic_evidence(tmp_path: Path) -> tuple[Path, Path, Path]:
    docs = tmp_path / "synthetic-docs"
    docs.mkdir()
    write_synthetic_evidence_docx(docs / "synthetic.docx")
    calibration_corpus = tmp_path / "synthetic-corpus-v1.json"
    freeze_corpus_manifest(
        docs_root=docs,
        corpus_id="synthetic-public-v1",
        output_path=calibration_corpus,
    )
    final_corpus = tmp_path / "synthetic-corpus-v2.json"
    payload = json.loads(calibration_corpus.read_text(encoding="utf-8"))
    payload["corpus_id"] = "synthetic-public-v2"
    final_corpus.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    final_dataset = tmp_path / "synthetic-dataset.json"
    write_synthetic_dataset(final_dataset)
    return calibration_corpus, final_corpus, final_dataset


def _structural_candidate() -> dict[str, object]:
    return {
        "candidate": _SELECTED,
        "strategy": "section_pack_v2",
        "config": {
            "target_tokens": 384,
            "hard_max_tokens": 512,
            "overlap_tokens": 64,
        },
        "report": {
            "chunks": 12,
            "standalone_heading_chunks": 0,
            "cross_section_chunks": 0,
            "cross_neighbor_group_links": 0,
            "hard_max_violations": 0,
            "uncovered_source_elements": 0,
            "source_coverage_ratio": 1.0,
            "table_row_split_violations": 0,
            "blank_chunks": 0,
            "duplicate_chunk_ids": 0,
            "ambiguous_quote_locator_cases": 0,
            "quote_locator_contract_violations": 0,
        },
    }


def _write_reports(
    tmp_path: Path,
    model_reports: Path,
    fleet_report: Path,
    *,
    recall_at_20: float = 0.96,
    ocr_calibrated: bool = True,
) -> tuple[Path, Path]:
    root = _project_root()
    pipeline_path = root / "deployment/config/pipeline.json"
    retrieval_path = root / "deployment/config/retrieval.json"
    corpus_path = tmp_path / "synthetic-corpus-v1.json"
    dataset_path = tmp_path / "synthetic-dataset.json"
    pipeline = load_pipeline(pipeline_path)
    retrieval = RetrievalSettings.load(retrieval_path)
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    dataset = load_dataset(dataset_path)
    fleet = verify_model_fleet(
        fleet_report,
        model_reports,
        pipeline=pipeline,
        calibration_source_revision=_CALIBRATION_REVISION,
    )
    identity = {
        "schema_version": "1",
        "calibration_source_revision": _CALIBRATION_REVISION,
        "pipeline_file_sha256": _sha256_file(pipeline_path),
        "pipeline_index_fingerprint": pipeline.index_fingerprint(),
        "corpus_id": corpus["corpus_id"],
        "corpus_digest": corpus["corpus_digest"],
        "corpus_manifest_sha256": _sha256_file(corpus_path),
    }
    structural_path = tmp_path / "structural.json"
    structural_path.write_text(
        json.dumps(
            {
                "mode": "structural",
                "status": "provisional_no_parameter_selection",
                "identity": identity,
                "documents": corpus["document_count"],
                "parser_counts": {"documents": corpus["document_count"]},
                "candidates": [_structural_candidate()],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    config = FreezeCandidateConfig(
        target_tokens=384,
        hard_max_tokens=512,
        overlap_tokens=64,
    )
    candidate_pipeline = build_candidate_pipeline(
        pipeline,
        config,
        strategy="section_pack_v2",
        fleet=fleet,
    )
    retrieval_path_out = tmp_path / "retrieval-report.json"
    retrieval_path_out.write_text(
        json.dumps(
            {
                "mode": "retrieval",
                "split": "tuning",
                "status": "real_model_results_provisional",
                "identity": {
                    **identity,
                    "retrieval_file_sha256": _sha256_file(retrieval_path),
                    "retrieval_serving_fingerprint": (
                        retrieval.serving_fingerprint(pipeline)
                    ),
                    "evaluation_dataset_sha256": _sha256_file(dataset_path),
                    "tuning_digest": canonical_tuning_digest(
                        dataset.documents,
                        tuple(
                            case.model_dump(mode="json")
                            for case in dataset.cases
                        ),
                    ),
                    "fleet": fleet.model_dump(mode="json"),
                },
                "cases": sum(
                    case.split == "tuning" for case in dataset.cases
                ),
                "ocr_calibrated": ocr_calibrated,
                "ocr_states": {
                    "succeeded": 3,
                    "low_confidence": 0,
                    "failed": 0,
                    "pending": 0,
                },
                "candidates": [
                    {
                        **_structural_candidate(),
                        "index_fingerprint": (
                            candidate_pipeline.index_fingerprint()
                        ),
                        "serving_fingerprint": (
                            retrieval.serving_fingerprint(candidate_pipeline)
                        ),
                        "report": {
                            "overall": {
                                "recall_at_20": recall_at_20,
                                "rerank_recall_at_5": 0.91,
                            }
                        },
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return structural_path, retrieval_path_out


def _freeze_inputs(
    tmp_path: Path,
    *,
    recall_at_20: float = 0.96,
    ocr_calibrated: bool = True,
) -> FreezeReleaseInputs:
    root = _project_root()
    model_reports = tmp_path / "model-contract-attempt"
    fleet_report = _write_model_reports(model_reports)
    calibration_corpus, final_corpus, final_dataset = (
        _write_synthetic_evidence(tmp_path)
    )
    structural, retrieval_report = _write_reports(
        tmp_path,
        model_reports,
        fleet_report,
        recall_at_20=recall_at_20,
        ocr_calibrated=ocr_calibrated,
    )
    return FreezeReleaseInputs(
        pipeline_path=root / "deployment/config/pipeline.json",
        retrieval_path=root / "deployment/config/retrieval.json",
        structural_report_path=structural,
        retrieval_report_path=retrieval_report,
        fleet_report_path=fleet_report,
        model_contract_directory=model_reports,
        calibration_corpus_manifest_path=calibration_corpus,
        final_corpus_manifest_path=final_corpus,
        final_evaluation_manifest_path=final_dataset,
        output_directory=tmp_path / "frozen",
        selected_candidate=_SELECTED,
    )


def _freeze_bundle(tmp_path: Path) -> tuple[Path, FreezeDecision]:
    inputs = _freeze_inputs(tmp_path)
    decision = freeze_release(inputs)
    return inputs.output_directory, decision


def test_freeze_release_writes_bound_outputs_without_endpoints(
    tmp_path: Path,
) -> None:
    output, decision = _freeze_bundle(tmp_path)
    pipeline = load_pipeline(output / "pipeline.json")
    retrieval = RetrievalSettings.load(output / "retrieval.json")
    loaded_decision = FreezeDecision.load(output / "FREEZE_DECISION.json")

    assert loaded_decision == decision
    assert retrieval.status == "frozen"
    assert retrieval.freeze_decision_sha256 == decision.sha256()
    assert pipeline.chunker_revision == (
        f"section_pack_v2@{_CALIBRATION_REVISION}"
    )
    assert pipeline.embedding_revision == "embedding-revision-1"
    assert pipeline.reranker_revision == "reranker-revision-2"
    assert decision.index_fingerprint == pipeline.index_fingerprint()
    assert decision.serving_fingerprint == retrieval.serving_fingerprint(
        pipeline
    )
    assert (
        decision.model_revisions.calibration_source_revision
        == _CALIBRATION_REVISION
    )
    assert decision.evidence.final_corpus_id == "synthetic-public-v2"
    assert len(decision.evidence.model_contract_reports) == 6
    require_indexable_configuration(pipeline, retrieval, decision)

    rendered = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(output.iterdir())
    )
    assert ".internal" not in rendered
    assert "endpoint" not in rendered
    assert "api_token" not in rendered.casefold()


@pytest.mark.parametrize(
    ("status", "decision_sha256"),
    (("provisional", "sha256:" + "c" * 64), ("frozen", None)),
)
def test_retrieval_state_requires_matching_decision_presence(
    status: str,
    decision_sha256: str | None,
) -> None:
    root = _project_root()
    payload = RetrievalSettings.load(
        root / "deployment/config/retrieval.json"
    ).model_dump(mode="json")
    payload.update(
        {"status": status, "freeze_decision_sha256": decision_sha256}
    )

    with pytest.raises(ValidationError, match="freeze_decision_sha256"):
        RetrievalSettings.model_validate(payload)


def test_freeze_release_rejects_threshold_failure(tmp_path: Path) -> None:
    inputs = _freeze_inputs(tmp_path, recall_at_20=0.949)

    with pytest.raises(ValueError, match="recall_at_20"):
        freeze_release(inputs)

    assert not inputs.output_directory.exists()


def test_freeze_release_rejects_uncalibrated_ocr(tmp_path: Path) -> None:
    inputs = _freeze_inputs(tmp_path, ocr_calibrated=False)

    with pytest.raises(ValueError, match="OCR"):
        freeze_release(inputs)

    assert not inputs.output_directory.exists()


def test_freeze_release_rejects_holdout_only_tuning_drift(
    tmp_path: Path,
) -> None:
    inputs = _freeze_inputs(tmp_path)
    final_dataset = inputs.final_evaluation_manifest_path
    payload = json.loads(final_dataset.read_text(encoding="utf-8"))
    tuning = next(
        case for case in payload["cases"] if case["split"] == "tuning"
    )
    tuning["question"] += "篡改"
    final_dataset.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="tuning"):
        freeze_release(inputs)


def test_freeze_release_requires_complete_single_model_attempt(
    tmp_path: Path,
) -> None:
    inputs = _freeze_inputs(tmp_path)
    (
        inputs.model_contract_directory / "model-contract-llm-4.json"
    ).unlink()

    with pytest.raises(ValueError, match=r"六份|七份|summary"):
        freeze_release(inputs)

    assert not inputs.output_directory.exists()


def test_model_fleet_rejects_endpoint_not_used_by_retrieval(
    tmp_path: Path,
) -> None:
    root = _project_root()
    model_reports = tmp_path / "model-contract-attempt"
    fleet_report = _write_model_reports(model_reports)

    with pytest.raises(ValueError, match=r"embedding.*endpoint"):
        verify_model_fleet(
            fleet_report,
            model_reports,
            pipeline=load_pipeline(root / "deployment/config/pipeline.json"),
            calibration_source_revision=_CALIBRATION_REVISION,
            retrieval_endpoints=RetrievalModelEndpoints(
                embedding=("http://different.internal:8000",),
                reranker=("http://reranker-2.internal:8000",),
            ),
        )


def test_model_fleet_rejects_endpoint_with_path(tmp_path: Path) -> None:
    root = _project_root()
    model_reports = tmp_path / "model-contract-attempt"
    fleet_report = _write_model_reports(model_reports)
    embedding_report = model_reports / "model-contract-embedding.json"
    payload = json.loads(embedding_report.read_text(encoding="utf-8"))
    payload["endpoint"] = "http://embedding-1.internal:8000/v1"
    embedding_report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=r"origin|endpoint"):
        verify_model_fleet(
            fleet_report,
            model_reports,
            pipeline=load_pipeline(root / "deployment/config/pipeline.json"),
            calibration_source_revision=_CALIBRATION_REVISION,
        )


def test_freeze_release_is_atomic_and_refuses_existing_output(
    tmp_path: Path,
) -> None:
    inputs = _freeze_inputs(tmp_path)
    output = inputs.output_directory
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")

    with pytest.raises(FileExistsError):
        freeze_release(inputs)

    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert tuple(output.iterdir()) == (sentinel,)


def test_index_gate_rejects_missing_hash_and_fingerprint_mutations(
    tmp_path: Path,
) -> None:
    output, decision = _freeze_bundle(tmp_path)
    pipeline = load_pipeline(output / "pipeline.json")
    retrieval = RetrievalSettings.load(output / "retrieval.json")

    with pytest.raises(ValueError, match="decision"):
        require_indexable_configuration(pipeline, retrieval, None)
    with pytest.raises(ValueError, match="hash"):
        require_indexable_configuration(
            pipeline,
            retrieval.model_copy(
                update={"freeze_decision_sha256": "sha256:" + "0" * 64}
            ),
            decision,
        )
    with pytest.raises(ValueError, match="index fingerprint"):
        require_indexable_configuration(
            pipeline.model_copy(update={"embedding_revision": "edited"}),
            retrieval,
            decision,
        )
    with pytest.raises(ValueError, match="serving fingerprint"):
        require_indexable_configuration(
            pipeline,
            retrieval.model_copy(update={"dense_limit": 41}),
            decision,
        )


def test_freeze_decision_forbids_unknown_fields(tmp_path: Path) -> None:
    _, decision = _freeze_bundle(tmp_path)
    payload = decision.model_dump(mode="json")
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        FreezeDecision.model_validate(payload)
