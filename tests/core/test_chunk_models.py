import hashlib

import pytest
from pydantic import ValidationError

from rag_app.adapters.chunkers.docx_structural.validation import (
    quote_is_publishable,
)
from rag_app.core.models import (
    Chunk,
    ChunkingPolicy,
    ChunkingReport,
    DocumentVersionRef,
    SourceAnchor,
    SourceSpan,
    SourceSpanKind,
    StoryKind,
)


def _anchor() -> SourceAnchor:
    return SourceAnchor(
        part_uri="/word/document.xml",
        story_kind=StoryKind.BODY,
        structural_path=("body", "p:0"),
        ordinal=0,
        paragraph_index=0,
        source_start_char=0,
        source_end_char=3,
    )


def _chunk(spans: tuple[SourceSpan, ...], text: str = "abc") -> Chunk:
    return Chunk(
        chunk_id=f"chunk_{'1' * 32}",
        version=DocumentVersionRef(
            document_id=f"doc_{'2' * 32}",
            document_version_id=f"dver_{'3' * 32}",
            content_sha256="4" * 64,
        ),
        chunker_fingerprint=f"sha256:{'5' * 64}",
        source_spans=spans,
        citation_text=text,
        embedding_text=text,
        lexical_text=text,
        token_count=len(text),
        token_count_is_estimate=False,
        tokenizer_id="test-tokenizer",
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def test_source_span_types_fail_closed() -> None:
    anchor = _anchor()
    with pytest.raises(ValidationError):
        SourceSpan(
            span_type=SourceSpanKind.SEPARATOR,
            node_id=f"node_{'6' * 32}",
            chunk_start_char=0,
            chunk_end_char=1,
            is_citable=False,
        )
    with pytest.raises(ValidationError):
        SourceSpan(
            span_type=SourceSpanKind.DERIVED_NUMBERING,
            node_id=f"node_{'6' * 32}",
            source_anchor=anchor,
            structural_path=anchor.structural_path,
            chunk_start_char=0,
            chunk_end_char=2,
            source_start_char=0,
            source_end_char=2,
        )


def test_chunk_requires_gapless_citation_span_coverage() -> None:
    anchor = _anchor()
    span = SourceSpan(
        node_id=f"node_{'6' * 32}",
        source_anchor=anchor,
        structural_path=anchor.structural_path,
        chunk_start_char=1,
        chunk_end_char=3,
        source_start_char=1,
        source_end_char=3,
    )
    with pytest.raises(ValidationError):
        _chunk((span,))


def test_quote_validator_rejects_separator_and_cross_source_quote() -> None:
    anchor = _anchor()
    original = SourceSpan(
        node_id=f"node_{'6' * 32}",
        source_anchor=anchor,
        structural_path=anchor.structural_path,
        chunk_start_char=0,
        chunk_end_char=1,
        source_start_char=0,
        source_end_char=1,
    )
    separator = SourceSpan(
        span_type=SourceSpanKind.SEPARATOR,
        chunk_start_char=1,
        chunk_end_char=2,
        is_citable=False,
    )
    final = original.model_copy(
        update={
            "chunk_start_char": 2,
            "chunk_end_char": 3,
            "source_start_char": 2,
            "source_end_char": 3,
        }
    )
    chunk = _chunk((original, separator, final), "a\nb")
    assert quote_is_publishable(chunk, 0, 1) is True
    assert quote_is_publishable(chunk, 0, 3) is False


def test_policy_uses_strictest_required_embedding_limit() -> None:
    policy = ChunkingPolicy(
        profile_hard_cap=500,
        max_embedding_tokens_by_slot=(
            ("primary", 480),
            ("standby", 900),
        ),
    )
    assert policy.effective_embedding_max == 480


def test_chunking_report_metrics_must_be_internally_consistent() -> None:
    report = ChunkingReport(
        chunk_count=1,
        total_citable_source_chars=10,
        unique_covered_source_chars=8,
        missing_source_chars=2,
        source_span_coverage=0.8,
        cross_boundary_violations=2,
        cross_section_violations=1,
        cross_group_violations=1,
    )
    assert report.source_span_coverage == 0.8

    with pytest.raises(ValidationError, match="coverage"):
        ChunkingReport(
            chunk_count=1,
            total_citable_source_chars=10,
            unique_covered_source_chars=8,
            missing_source_chars=2,
            source_span_coverage=1.0,
        )
