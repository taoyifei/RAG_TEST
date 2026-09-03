from __future__ import annotations

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import (
    EvidenceSelectionContext,
    KnowledgeBaseScope,
    QueryKind,
    RetrievalPolicy,
    SearchRequest,
)
from tests.application.retrieval.helpers import make_ranked_chunk

_SCOPE = KnowledgeBaseScope(
    project_id=f"prj_{'1' * 32}",
    knowledge_base_id=f"kb_{'2' * 32}",
)


def test_query_aware_selection_rejects_higher_ranked_noise() -> None:
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="液压压力是多少")
    )
    noise = make_ranked_chunk(1, "市场部门本周召开例会", document_number=1)
    relevant = make_ranked_chunk(
        2, "液压系统额定压力为 16 MPa", document_number=2
    )

    evidence = EvidenceAssembler().assemble(
        (noise, relevant),
        RetrievalPolicy(),
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="lexical_overlap",
            selected_slot=None,
        ),
    )

    assert [item.chunk_id for item in evidence] == [
        relevant.hydrated.chunk.chunk_id
    ]
    assert evidence[0].citation_text == "液压系统额定压力为 16 MPa"


def test_evidence_total_and_per_chunk_caps_are_explicit() -> None:
    candidates = tuple(
        make_ranked_chunk(
            index,
            f"压力参数 {index}",
            document_number=index,
        )
        for index in range(1, 6)
    )

    evidence = EvidenceAssembler().assemble(
        candidates,
        RetrievalPolicy(max_evidence_items=2, max_evidence_items_per_chunk=1),
    )

    assert len(evidence) == 2
    assert len({item.chunk_id for item in evidence}) == 2
