import pytest

from evaluation.dataset import EvaluationDataset
from evaluation.metrics import (
    PublishedCitation,
    QueryEvaluationResult,
    RankedEvidence,
    Thresholds,
    evaluate_results,
)
from rag_app.active_evidence import TrustedActiveEvidence
from tests.active_evidence_fixtures import (
    active_evidence_record,
    trusted_active_evidence,
)
from tests.synthetic_evaluation import synthetic_evaluation_dataset


def _oracle_results(
    dataset: EvaluationDataset,
) -> tuple[QueryEvaluationResult, ...]:
    results = []
    for case in dataset.cases:
        if case.validation_state != "verified_text":
            continue
        ranked = tuple(
            RankedEvidence(
                chunk_id=f"chunk-{case.id}-{index}",
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
                latencies_ms={"retrieve": 100, "answer": 1000},
            )
        )
    return tuple(results)


def _active_manifest(
    dataset: EvaluationDataset,
) -> TrustedActiveEvidence:
    records = tuple(
        active_evidence_record(
            chunk_id=f"chunk-{case.id}-{index}",
            source_path=dataset.documents[label.document],
            locator=label.locator_contains,
            text=label.quote or "OCR",
        )
        for case in dataset.cases
        if case.validation_state == "verified_text"
        for index, label in enumerate(case.expected.evidence, start=1)
    )
    return trusted_active_evidence(records)


def test_evaluator_accepts_complete_human_reviewed_results() -> None:
    dataset = synthetic_evaluation_dataset()

    report = evaluate_results(
        dataset,
        _oracle_results(dataset),
        active_evidence_manifest=_active_manifest(dataset),
    )

    assert report.passed
    assert report.metrics["recall_at_5"] == 1.0
    assert report.metrics["recall_at_10"] == 1.0
    assert report.metrics["recall_at_20"] == 1.0
    assert report.metrics["rerank_recall_at_5"] == 1.0
    assert report.metrics["ocr_evaluated_characters"] == 0
    assert report.metrics["user_feedback_count"] == 0
    assert report.metrics["unanswerable_refusal"] == 1.0


def test_evaluator_aggregates_ocr_cer_and_user_feedback() -> None:
    dataset = synthetic_evaluation_dataset()
    results = list(_oracle_results(dataset))
    results[0] = results[0].model_copy(
        update={
            "ocr_character_errors": 2,
            "ocr_reference_characters": 100,
            "user_feedback": "useful",
        }
    )
    results[1] = results[1].model_copy(
        update={
            "ocr_character_errors": 3,
            "ocr_reference_characters": 50,
            "user_feedback": "not_useful",
        }
    )

    report = evaluate_results(
        dataset,
        tuple(results),
        active_evidence_manifest=_active_manifest(dataset),
    )

    assert report.metrics["ocr_cer"] == 5 / 150
    assert report.metrics["ocr_evaluated_characters"] == 150
    assert report.metrics["user_feedback_count"] == 2
    assert report.metrics["user_feedback_useful_rate"] == 0.5


def test_wrong_source_makes_quality_gate_fail() -> None:
    dataset = synthetic_evaluation_dataset()
    results = list(_oracle_results(dataset))
    answerable_indexes = [
        index
        for index, result in enumerate(results)
        if result.answer_status == "answered"
    ]
    for index in answerable_indexes[:10]:
        result = results[index]
        wrong = RankedEvidence(
            chunk_id="wrong",
            source_path="不存在.docx",
            locator="错误定位",
        )
        results[index] = result.model_copy(
            update={"retrieved": (wrong,), "reranked": (wrong,)}
        )

    report = evaluate_results(
        dataset,
        tuple(results),
        active_evidence_manifest=_active_manifest(dataset),
    )

    assert not report.passed
    assert any("recall_at_20" in failure for failure in report.failures)


def test_completion_thresholds_are_not_weaker_than_task_contract() -> None:
    thresholds = Thresholds()

    assert thresholds.recall_at_20 == 0.95
    assert thresholds.rerank_recall_at_5 == 0.90
    assert thresholds.answerable_false_refusal_max == 0.10
    assert thresholds.unanswerable_refusal == 0.95
    assert thresholds.citation_precision == 1.0
    assert thresholds.citation_recall == 1.0
    assert thresholds.citation_fact_support == 1.0
    assert thresholds.prompt_injection_pass_rate == 1.0


def test_wrong_citation_quote_fails_quality_gate() -> None:
    dataset = synthetic_evaluation_dataset()
    results = list(_oracle_results(dataset))
    result = next(item for item in results if item.citations)
    wrong = result.citations[0].model_copy(update={"quote": "错误引用原文"})
    result_index = results.index(result)
    results[result_index] = result.model_copy(update={"citations": (wrong,)})

    report = evaluate_results(
        dataset,
        tuple(results),
        active_evidence_manifest=_active_manifest(dataset),
    )

    assert report.metrics["citation_precision"] < 1.0
    assert report.metrics["citation_recall"] < 1.0
    assert not report.passed


def test_ocr_error_counts_must_be_complete_pairs() -> None:
    dataset = synthetic_evaluation_dataset()
    result = _oracle_results(dataset)[0]

    with pytest.raises(ValueError, match="OCR CER"):
        QueryEvaluationResult.model_validate(
            {
                **result.model_dump(),
                "ocr_character_errors": 1,
                "ocr_reference_characters": None,
            }
        )
