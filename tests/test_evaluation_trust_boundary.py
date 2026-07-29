from pathlib import Path

import pytest

from evaluation import evaluate
from evaluation.dataset import EvaluationDataset
from evaluation.metrics import (
    ActiveEvidenceManifest,
    PublishedCitation,
    QueryEvaluationResult,
    RankedEvidence,
    evaluate_results,
)
from tests.active_evidence_fixtures import (
    active_evidence_manifest,
    active_evidence_record,
)
from tests.synthetic_evaluation import synthetic_evaluation_dataset


def _result_and_manifest(
    dataset: EvaluationDataset,
) -> tuple[
    tuple[QueryEvaluationResult, ...],
    ActiveEvidenceManifest,
]:
    results = []
    evidence_records = []
    for case in dataset.cases:
        if case.validation_state != "verified_text":
            continue
        ranked = tuple(
            RankedEvidence(
                chunk_id=f"active-{case.id}-{index}",
                source_path=dataset.documents[label.document],
                locator=label.locator_contains,
            )
            for index, label in enumerate(case.expected.evidence, start=1)
        )
        citations = tuple(
            PublishedCitation(
                evidence_id=f"E{index}",
                chunk_id=item.chunk_id,
                source_path=item.source_path,
                locator=item.locator,
                quote=case.expected.evidence[index - 1].quote or "OCR",
            )
            for index, item in enumerate(ranked, start=1)
        )
        for index, item in enumerate(ranked, start=1):
            evidence_records.append(
                active_evidence_record(
                    chunk_id=item.chunk_id,
                    source_path=item.source_path,
                    locator=item.locator,
                    text=case.expected.evidence[index - 1].quote or "OCR",
                )
            )
        answerable = bool(case.expected.answerable)
        results.append(
            QueryEvaluationResult(
                id=case.id,
                retrieved=ranked,
                reranked=ranked,
                answer_status="answered" if answerable else "refused",
                refusal_code=None if answerable else "NO_EVIDENCE",
                citations=citations if answerable else (),
                answer_correct=True if answerable else None,
                answer_complete=True if answerable else None,
                citations_fact_supported=True if answerable else None,
                human_reviewer="人工验收员" if answerable else None,
            )
        )
    manifest = active_evidence_manifest(tuple(evidence_records))
    return tuple(results), manifest


def test_forged_chunk_id_is_computed_as_invalid() -> None:
    dataset = synthetic_evaluation_dataset()
    results, manifest = _result_and_manifest(dataset)
    mutable = list(results)
    target_index = next(
        index for index, result in enumerate(mutable) if result.citations
    )
    target = mutable[target_index]
    forged_ranked = target.reranked[0].model_copy(
        update={"chunk_id": "forged-chunk"}
    )
    forged_citation = target.citations[0].model_copy(
        update={"chunk_id": "forged-chunk"}
    )
    mutable[target_index] = target.model_copy(
        update={
            "reranked": (forged_ranked,),
            "citations": (forged_citation,),
        }
    )

    report = evaluate_results(
        dataset,
        tuple(mutable),
        active_evidence_manifest=active_evidence_manifest(
            manifest.records
        ),
    )

    assert report.metrics["invalid_citation_ids"] == 1
    assert not report.passed


def test_result_cannot_self_report_invalid_citation_count() -> None:
    dataset = synthetic_evaluation_dataset()
    results, _ = _result_and_manifest(dataset)
    payload = results[0].model_dump()
    payload["invalid_citation_ids"] = 0

    with pytest.raises(ValueError, match="invalid_citation_ids"):
        QueryEvaluationResult.model_validate(payload)


def test_results_and_audit_manifest_cannot_be_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 evaluator 拒绝 audit manifest 回灌参数。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的命令行隔离器。

    Returns:
        无返回值。

    """
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate.py",
            "--dataset",
            str(tmp_path / "dataset.json"),
            "--results",
            str(tmp_path / "results.jsonl"),
            "--qdrant-url",
            "http://127.0.0.1:6333",
            "--qdrant-alias",
            "rag-active",
            "--manifest-database",
            str(tmp_path / "state.sqlite3"),
            "--active-evidence-input",
            str(tmp_path / "audit.json"),
        ],
    )

    with pytest.raises(SystemExit) as error:
        evaluate.main()

    assert error.value.code == 2
