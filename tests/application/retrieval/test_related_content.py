"""相关原文只是只读预览，不能获得答案资格。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.related import select_related_contents
from rag_app.core.errors import IndexCorrupt
from rag_app.core.models import (
    ChunkRole,
    KnowledgeBaseScope,
    SearchRequest,
    SourceSpanKind,
)
from tests.application.retrieval.helpers import make_ranked_chunk


def _request(text: str = "隐私专员的电话是什么？") -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=f"prj_{'a' * 32}", knowledge_base_id=f"kb_{'b' * 32}"
        ),
        text=text,
        include_related_content=True,
    )


def test_missing_attribute_still_has_independent_original_preview() -> None:
    request = _request()
    candidate = make_ranked_chunk(1, "隐私专员负责受理申请。")
    result = select_related_contents(
        (candidate,),
        request,
        QueryAnalyzer().analyze(request),
        revision_id=f"irev_{'c' * 32}",
        rerank_mode="rerank_bypassed_provider_unavailable",
    )
    assert len(result) == 1
    assert result[0].excerpt == candidate.hydrated.chunk.citation_text
    assert result[0].is_answer_evidence is False
    assert result[0].rerank_verified is False
    assert result[0].relevance_reason == "RELEVANCE_UNVERIFIED"
    assert result[0].related_id.startswith("related_")
    assert "support_id" not in result[0].model_dump()


def test_preview_deduplicates_and_bounds_without_synthetic_text() -> None:
    request = _request()
    candidates = tuple(
        make_ranked_chunk(n, "隐私专员处理资料。" * 50) for n in range(1, 7)
    )
    result = select_related_contents(
        (candidates[0], *candidates),
        request,
        QueryAnalyzer().analyze(request),
        revision_id=f"irev_{'c' * 32}",
        rerank_mode="deterministic",
    )
    assert 1 <= len(result) <= 3
    assert sum(len(item.excerpt) for item in result) <= 720
    for item in result:
        assert len(item.excerpt) <= 240
        assert item.excerpt in candidates[0].hydrated.chunk.citation_text
        assert len(item.source_spans) == 1
        assert item.source_spans[0].source_end_char == len(item.excerpt)


def test_weak_common_words_and_empty_candidates_do_not_fill_cards() -> None:
    request = _request("如何办理申请？")
    for candidates in ((), (make_ranked_chunk(1, "申请需要审核办理。"),)):
        assert (
            select_related_contents(
                candidates,
                request,
                QueryAnalyzer().analyze(request),
                revision_id=f"irev_{'c' * 32}",
                rerank_mode="provider",
            )
            == ()
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("knowledge_base_id", f"kb_{'d' * 32}"),
        ("index_revision_id", f"irev_{'d' * 32}"),
        ("project_id", f"prj_{'d' * 32}"),
    ],
)
def test_related_identity_mismatch_fails_closed(field: str, value: str) -> None:
    request = _request()
    candidate = make_ranked_chunk(1, "隐私专员负责受理申请。")
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={
                    "chunk": candidate.hydrated.chunk.model_copy(
                        update={field: value}
                    )
                }
            )
        }
    )
    with pytest.raises(IndexCorrupt):
        select_related_contents(
            (candidate,),
            request,
            QueryAnalyzer().analyze(request),
            revision_id=f"irev_{'c' * 32}",
            rerank_mode="provider",
        )


def test_long_preview_keeps_the_relevant_sentence_and_true_offsets() -> None:
    request = _request()
    text = "无关介绍。" * 100 + "隐私专员负责受理申请。" + "后续内容。" * 100
    candidate = make_ranked_chunk(1, text)
    result = select_related_contents(
        (candidate,),
        request,
        QueryAnalyzer().analyze(request),
        revision_id=f"irev_{'c' * 32}",
        rerank_mode="provider",
    )
    assert len(result) == 1
    item = result[0]
    assert "隐私专员" in item.excerpt
    span = item.source_spans[0]
    assert span.source_start_char == 500
    assert text[span.chunk_start_char : span.chunk_end_char] == item.excerpt
    assert text[span.source_start_char : span.source_end_char] == item.excerpt


def test_table_and_repeated_heading_are_omitted_instead_of_mislabelled() -> (
    None
):
    request = _request()
    table = make_ranked_chunk(1, "隐私专员：8℃", role=ChunkRole.TABLE)
    repeated = make_ranked_chunk(2, "隐私专员")
    chunk = repeated.hydrated.chunk
    repeated_span = chunk.source_spans[0].model_copy(
        update={
            "span_type": SourceSpanKind.REPEATED_CONTEXT,
            "is_repeated": True,
        }
    )
    repeated = repeated.model_copy(
        update={
            "hydrated": repeated.hydrated.model_copy(
                update={
                    "chunk": chunk.model_copy(
                        update={"source_spans": (repeated_span,)}
                    )
                }
            )
        }
    )
    assert (
        select_related_contents(
            (table, repeated),
            request,
            QueryAnalyzer().analyze(request),
            revision_id=f"irev_{'c' * 32}",
            rerank_mode="provider",
        )
        == ()
    )


def test_actual_rerank_score_required_and_original_order_preserved() -> None:
    request = _request()
    first = make_ranked_chunk(1, "隐私专员负责申请。")
    second = make_ranked_chunk(2, "隐私专员负责收件。")
    first = first.model_copy(update={"rerank_rank": 2})
    second = second.model_copy(update={"rerank_rank": 1, "rerank_score": 0.6})
    result = select_related_contents(
        (second, first),
        request,
        QueryAnalyzer().analyze(request),
        revision_id=f"irev_{'c' * 32}",
        rerank_mode="provider",
    )
    assert [item.chunk_id for item in result] == [
        second.hydrated.chunk.chunk_id,
        first.hydrated.chunk.chunk_id,
    ]
    assert [item.rerank_verified for item in result] == [True, False]
    assert all("rerank_rank" not in item.model_dump() for item in result)


def test_denied_filter_and_broken_span_do_not_leak_preview() -> None:
    request = _request()
    candidate = make_ranked_chunk(1, "隐私专员负责受理申请。")
    restricted = request.model_copy(
        update={"access_filters": (("allowed_document_ids", ()),)}
    )
    with pytest.raises(IndexCorrupt):
        select_related_contents(
            (candidate,),
            restricted,
            QueryAnalyzer().analyze(request),
            revision_id=f"irev_{'c' * 32}",
            rerank_mode="provider",
        )
    span = candidate.hydrated.chunk.source_spans[0]
    broken = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={
                    "chunk": candidate.hydrated.chunk.model_copy(
                        update={
                            "source_spans": (
                                span.model_copy(
                                    update={"source_end_char": 9999}
                                ),
                            )
                        }
                    )
                }
            )
        }
    )
    with pytest.raises(IndexCorrupt):
        select_related_contents(
            (broken,),
            request,
            QueryAnalyzer().analyze(request),
            revision_id=f"irev_{'c' * 32}",
            rerank_mode="provider",
        )
