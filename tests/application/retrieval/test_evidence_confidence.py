from __future__ import annotations

import pytest

from rag_app.application.answering.validation import validate_extractive_draft
from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.confidence import ConfidenceEvaluator
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.errors import ValidationFailed
from rag_app.core.models import (
    AnswerDraft,
    ChunkRole,
    ConfidenceStatus,
    KnowledgeBaseScope,
    QueryAnalysis,
    QueryKind,
    RetrievalPolicy,
    SearchRequest,
    SourceSpanKind,
)
from tests.application.retrieval.helpers import make_ranked_chunk

_SCOPE = KnowledgeBaseScope(
    project_id=f"prj_{'1' * 32}",
    knowledge_base_id=f"kb_{'2' * 32}",
)


def _analysis(text: str) -> QueryAnalysis:
    return QueryAnalyzer().analyze(SearchRequest(scope=_SCOPE, text=text))


def test_evidence_dedup_preserves_diversity_with_budget() -> None:
    first = make_ranked_chunk(1, "one", document_number=1)
    duplicate = first.model_copy()
    same_document = make_ranked_chunk(2, "two", document_number=1)
    other_document = make_ranked_chunk(3, "tri", document_number=2)

    evidence = EvidenceAssembler().assemble(
        (first, duplicate, same_document, other_document),
        RetrievalPolicy(
            evidence_token_budget=2,
            per_document_cap=1,
            per_section_cap=1,
        ),
    )

    assert len(evidence) == 2
    assert evidence[0].document_id != evidence[1].document_id
    assert [item.support_id for item in evidence] == ["S1", "S2"]


def test_evidence_publishes_each_table_cell_but_not_separator() -> None:
    candidate = make_ranked_chunk(1, "A|C", role=ChunkRole.TABLE)
    source = candidate.hydrated.chunk.source_spans[0]
    first = source.model_copy(
        update={
            "chunk_end_char": 1,
            "source_end_char": 1,
        }
    )
    separator = source.model_copy(
        update={
            "span_type": SourceSpanKind.SEPARATOR,
            "node_id": None,
            "source_anchor": None,
            "structural_path": (),
            "chunk_start_char": 1,
            "chunk_end_char": 2,
            "source_start_char": None,
            "source_end_char": None,
            "is_citable": False,
        }
    )
    last = source.model_copy(
        update={
            "chunk_start_char": 2,
            "chunk_end_char": 3,
            "source_start_char": 2,
            "source_end_char": 3,
        }
    )
    chunk = candidate.hydrated.chunk.model_copy(
        update={"source_spans": (first, separator, last)}
    )
    candidate = candidate.model_copy(
        update={
            "hydrated": candidate.hydrated.model_copy(
                update={"chunk": chunk}
            )
        }
    )

    evidence = EvidenceAssembler().assemble(
        (candidate,),
        RetrievalPolicy(max_evidence_items_per_chunk=2),
    )

    assert [item.citation_text for item in evidence] == ["A", "C"]
    assert all(item.source_spans[0].is_citable for item in evidence)


def test_confidence_refuses_unsupported_evidence() -> None:
    evaluator = ConfidenceEvaluator()
    policy = RetrievalPolicy()
    lexical = make_ranked_chunk(1, "answer")
    metadata = make_ranked_chunk(
        2,
        "metadata",
        role=ChunkRole.IMAGE_METADATA,
    )
    lexical_evidence = EvidenceAssembler().assemble((lexical,), policy)
    metadata_evidence = EvidenceAssembler().assemble((metadata,), policy)

    empty = evaluator.evaluate(
        _analysis("unknown"),
        QueryKind.SIMPLE_FACT,
        (),
        (),
        ("DENSE_UNAVAILABLE",),
    )
    metadata_only = evaluator.evaluate(
        _analysis("metadata"),
        QueryKind.SIMPLE_FACT,
        (metadata,),
        metadata_evidence,
        (),
    )
    ambiguous = evaluator.evaluate(
        _analysis("它"),
        QueryKind.AMBIGUOUS,
        (lexical,),
        lexical_evidence,
        (),
    )

    assert empty.status is ConfidenceStatus.PROVIDER_UNAVAILABLE
    assert metadata_only.status is ConfidenceStatus.INSUFFICIENT_EVIDENCE
    assert ambiguous.status is ConfidenceStatus.AMBIGUOUS_NEEDS_CLARIFICATION
    assert {name for name, _ in ambiguous.feature_values} >= {
        "rank_stability",
        "rerank_margin",
        "evidence_count",
        "degraded_count",
    }


def test_cross_separator_quote_is_rejected() -> None:
    evidence = EvidenceAssembler().assemble(
        (make_ranked_chunk(1, "source"),), RetrievalPolicy()
    )
    separator = evidence[0].source_spans[0].model_copy(
        update={"span_type": SourceSpanKind.SEPARATOR}
    )
    invalid = evidence[0].model_copy(update={"source_spans": (separator,)})

    with pytest.raises(ValidationFailed):
        validate_extractive_draft(
            AnswerDraft(text="source", cited_evidence_ids=("S1",)),
            (invalid,),
        )
