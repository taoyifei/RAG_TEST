import pytest

from evaluation.dataset import EvaluationDataset
from evaluation.metrics import (
    ActiveEvidenceManifest,
    PublishedCitation,
    QueryEvaluationResult,
    RankedEvidence,
    evaluate_results,
)
from tests.active_evidence_fixtures import (
    active_evidence_record,
    trusted_active_evidence,
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
    manifest = trusted_active_evidence(
        tuple(evidence_records)
    ).manifest
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
        active_evidence_manifest=trusted_active_evidence(manifest.records),
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


def test_results_and_manifest_cannot_be_forged_together() -> None:
    dataset = synthetic_evaluation_dataset()
    results, forged_manifest = _result_and_manifest(dataset)

    with pytest.raises(TypeError, match="可信活动证据"):
        evaluate_results(
            dataset,
            results,
            active_evidence_manifest=forged_manifest,
        )
