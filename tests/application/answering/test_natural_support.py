"""CQ1 批量审核的格式、原文坐标和发布边界。"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from rag_app.application.answering.natural_answer import (
    NaturalCompletion,
    NaturalReference,
)
from rag_app.application.answering.natural_support import (
    SupportPassage,
    review_and_publish,
)
from rag_app.application.retrieval.natural_context import NaturalBudget
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.models import SourceSpan
from tests.application.retrieval.helpers import make_ranked_chunk


def _passage(text: str, number: int = 1) -> SupportPassage:
    chunk = make_ranked_chunk(number, text).hydrated.chunk
    return SupportPassage(
        NaturalReference(
            alias=f"S{number}",
            document_id=chunk.version.document_id,
            document_version_id=chunk.version.document_version_id,
            document_title=f"来源{number}",
            chunk_ids=(chunk.chunk_id,),
            source_spans=chunk.source_spans,
            citation_basis="original",
            source_complete=True,
        ),
        text,
    )


def _verdict(
    *,
    unit_id: str = "u1",
    verdict: str = "supported",
    handle: str = "c1",
    quote: str = "甲方负责核对记录。",
) -> dict[str, object]:
    return {
        "unit_id": unit_id,
        "verdict": verdict,
        "evidence": (
            [{"source_handle": handle, "quote": quote}]
            if verdict == "supported"
            else []
        ),
        "reason_code": (
            "DIRECT_SUPPORT" if verdict == "supported" else "NOT_SUPPORTED"
        ),
    }


def _run(  # noqa: PLR0913
    draft: str,
    rows: list[dict[str, object]],
    passages: tuple[SupportPassage, ...],
    *,
    response: str | None = None,
    finish_reason: str = "stop",
    input_limit: int | None = None,
) -> tuple[object, Mock]:
    model = Mock()
    model.complete_natural.return_value = NaturalCompletion(
        text=(
            response
            if response is not None
            else json.dumps({"coverage": "complete", "units": rows})
        ),
        model="fixture-reviewer",
        provider_calls=(),
        finish_reason=finish_reason,
    )
    publication = review_and_publish(
        question="谁负责核对记录？",
        draft=draft,
        passages=passages,
        source_scope_digest="sha256:" + "1" * 64,
        index_revision_id="irev_" + "2" * 32,
        model=model,
        source_identities=tuple(
            (item.reference.document_version_id, item.reference.document_id)
            for item in passages
        ),
        cancellation=StreamCancellation(),
        input_limit=input_limit or NaturalBudget().input_limit,
    )
    return publication, model


@pytest.mark.parametrize(
    "draft",
    ("甲方负责核对记录。[S1]", "甲方负责核对记录。"),
)
def test_supported_unit_gets_exact_quote_and_generated_citation(
    draft: str,
) -> None:
    publication, model = _run(
        draft,
        [_verdict()],
        (_passage("甲方负责核对记录。"),),
    )

    assert publication.status == "GROUNDED_ANSWER"
    assert publication.answer == "甲方负责核对记录。[S1]"
    assert publication.supported_units == 1
    assert publication.references[0].excerpt == "甲方负责核对记录。"
    assert publication.references[0].answer_unit_id == "u1"
    assert publication.references[0].support_start_char == 0
    assert model.complete_natural.call_count == 1


def test_partial_answer_keeps_only_complete_supported_paragraph() -> None:
    publication, _model = _run(
        "甲方负责核对记录。[S1]\n\n乙方批准预算。[S1]",
        [_verdict(), _verdict(unit_id="u2", verdict="unsupported")],
        (_passage("甲方负责核对记录。"),),
    )

    assert publication.status == "GROUNDED_PARTIAL"
    assert publication.answer is not None
    assert "甲方负责核对记录。[S1]" in publication.answer
    assert "乙方批准预算" not in publication.answer
    assert "尚未形成可核对的答案" in publication.answer


def test_dependent_paragraph_is_dropped_after_unverified_predecessor() -> None:
    publication, _model = _run(
        "乙方批准预算。\n\n因此该预算需存档。",
        [
            _verdict(verdict="unsupported"),
            _verdict(unit_id="u2", quote="因此该预算需存档。"),
        ],
        (_passage("因此该预算需存档。"),),
    )

    assert publication.status == "INSUFFICIENT_EVIDENCE"
    assert publication.answer is None


def test_covered_unit_does_not_claim_complete_multi_part_answer() -> None:
    publication, _model = _run(
        "甲方负责核对记录。",
        [_verdict()],
        (_passage("甲方负责核对记录。"),),
        response=json.dumps(
            {"coverage": "partial", "units": [_verdict()]},
            ensure_ascii=False,
        ),
    )

    assert publication.status == "GROUNDED_PARTIAL"
    assert publication.supported_units == 1


@pytest.mark.parametrize(
    ("source", "quote", "handle"),
    (
        ("甲方负责核对记录。", "甲方负责批准预算。", "c1"),
        ("甲方甲方", "甲方", "c1"),
        ("甲甲甲", "甲甲", "c1"),
        ("甲方负责核对记录。", "甲方负责核对记录。", "c9"),
    ),
)
def test_missing_ambiguous_or_unknown_quote_never_publishes(
    source: str, quote: str, handle: str
) -> None:
    publication, _model = _run(
        "甲方负责核对记录。[S1]",
        [_verdict(handle=handle, quote=quote)],
        (_passage(source),),
    )

    assert publication.status == "INSUFFICIENT_EVIDENCE"
    assert publication.answer is None
    assert not publication.references


def test_valid_but_wrong_inline_citation_cannot_be_silently_rebound() -> None:
    publication, _model = _run(
        "甲方负责核对记录。[S1]",
        [_verdict(handle="c2")],
        (_passage("其他职责。"), _passage("甲方负责核对记录。", 2)),
    )

    assert publication.status == "INSUFFICIENT_EVIDENCE"
    assert publication.answer is None


@pytest.mark.parametrize(
    "response",
    (
        "not json",
        '{"units":[]}',
        json.dumps({"coverage": "complete", "units": [_verdict(), _verdict()]}),
        json.dumps(
            {"coverage": "complete", "units": [{**_verdict(), "verdict": []}]}
        ),
    ),
)
def test_invalid_or_uncovered_review_is_technical_failure(
    response: str,
) -> None:
    publication, _model = _run(
        "甲方负责核对记录。",
        [],
        (_passage("甲方负责核对记录。"),),
        response=response,
    )

    assert publication.status == "EXECUTION_ERROR"
    assert publication.reason_code == "SUPPORT_REVIEW_FORMAT_INVALID"


def test_review_budget_and_truncation_never_release_draft() -> None:
    passage = (_passage("甲方负责核对记录。"),)
    budget, model = _run(
        "甲方负责核对记录。", [_verdict()], passage, input_limit=1
    )
    assert budget.reason_code == "SUPPORT_REVIEW_BUDGET_EXCEEDED"
    model.complete_natural.assert_not_called()

    truncated, _model = _run(
        "甲方负责核对记录。", [_verdict()], passage, finish_reason="length"
    )
    assert truncated.reason_code == "SUPPORT_REVIEW_TRUNCATED"
    assert truncated.answer is None


def test_unicode_quote_coordinates_follow_real_source_span() -> None:
    publication, _model = _run(
        "乙字代表目标。",
        [_verdict(quote="乙")],
        (_passage("甲乙丙"),),
    )

    assert publication.status == "GROUNDED_ANSWER"
    reference = publication.references[0]
    assert (reference.support_start_char, reference.support_end_char) == (1, 2)
    assert reference.source_spans[0].source_start_char == 1
    assert reference.source_spans[0].source_end_char == 2


def test_table_quote_needs_header_and_target_row_context() -> None:
    text = "等级|金额\nA|1000"
    base = _passage(text)
    source = base.reference.source_spans[0]

    def row_span(start: int, end: int, row: int) -> SourceSpan:
        path = ("body", "tbl:1", f"tr:{row}", "tc:0")
        anchor = source.source_anchor
        assert anchor is not None
        return SourceSpan.model_validate(
            {
                **source.model_dump(mode="python"),
                "node_id": f"node_{row + 1:032x}",
                "structural_path": path,
                "source_anchor": {
                    **anchor.model_dump(mode="python"),
                    "structural_path": path,
                    "table_index": 1,
                    "row_index": row,
                    "source_start_char": start,
                    "source_end_char": end,
                },
                "chunk_start_char": start,
                "chunk_end_char": end,
                "source_start_char": start,
                "source_end_char": end,
            }
        )

    passage = SupportPassage(
        base.reference.model_copy(
            update={"source_spans": (row_span(0, 5, 0), row_span(6, 12, 1))}
        ),
        text,
    )
    full, _model = _run(
        "A 级金额为 1000。",
        [_verdict(quote=text)],
        (passage,),
    )
    value_only, _model = _run(
        "A 级金额为 1000。",
        [_verdict(quote="1000")],
        (passage,),
    )

    assert full.status == "GROUNDED_ANSWER"
    assert len(full.references[0].source_spans) == 2
    assert value_only.status == "INSUFFICIENT_EVIDENCE"
