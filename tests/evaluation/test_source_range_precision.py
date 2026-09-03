from __future__ import annotations

import pytest

from evaluation.v2.metrics import MetricContext, compute_metric_report
from evaluation.v2.models import SourceRangeExpectation
from evaluation.v2.runtime import _resolve_source_expectation
from tests.application.retrieval.helpers import make_ranked_chunk
from tests.evaluation.test_metrics import _case, _observation


def test_duplicate_text_requires_occurrence_or_anchor() -> None:
    chunks = (
        make_ranked_chunk(1, "重复事实", document_number=1).hydrated.chunk,
        make_ranked_chunk(2, "重复事实", document_number=1).hydrated.chunk,
    )
    ambiguous = SourceRangeExpectation(
        document_id=chunks[0].version.document_id,
        exact_text="重复事实",
    )

    with pytest.raises(ValueError, match="occurrence"):
        _resolve_source_expectation(ambiguous, chunks)

    resolved = _resolve_source_expectation(
        ambiguous.model_copy(update={"occurrence": 2}), chunks
    )

    assert resolved.node_id == chunks[1].source_spans[0].node_id


def test_source_range_precision_and_recall_are_distinct() -> None:
    observation = _observation().model_copy(
        update={
            "matched_source_range_count": 1,
            "required_source_range_count": 1,
            "predicted_source_range_count": 2,
            "relevant_predicted_source_range_count": 1,
        }
    )

    report = compute_metric_report(
        (_case(),),
        (observation,),
        context=MetricContext(
            lane="offline-structural",
            variant_id="baseline",
            split="tuning",
            seed=7,
        ),
    )

    assert report.metrics["source_range_recall"].value == 1.0
    assert report.metrics["source_range_precision"].value == 0.5
    assert report.metrics["source_range_f1"].value == pytest.approx(2 / 3)
