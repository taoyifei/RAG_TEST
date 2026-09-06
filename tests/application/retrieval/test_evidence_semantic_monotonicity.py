"""已校准 dense 路径增加少量词面重合时不得丢掉合法证据。"""

from __future__ import annotations

import pytest

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.evidence import (
    EvidenceAssembler,
    semantic_candidate_allowed,
)
from rag_app.core.models import (
    EvidenceSelectionContext,
    KnowledgeBaseScope,
    QueryKind,
    RetrievalPolicy,
    SearchRequest,
)
from tests.application.retrieval.helpers import make_ranked_chunk

_QUERY = "amber beacon cedar delta ember frost grove harbor ivory juniper"
_SPACE = "primary:fake-semantic:independent:3:l2:1"


def _context(text: str) -> EvidenceSelectionContext:
    scope = KnowledgeBaseScope(
        project_id=f"prj_{'1' * 32}",
        knowledge_base_id=f"kb_{'2' * 32}",
    )
    return EvidenceSelectionContext(
        analysis=QueryAnalyzer().analyze(SearchRequest(scope=scope, text=text)),
        query_kind=QueryKind.SIMPLE_FACT,
        rerank_mode="provider",
        selected_slot="primary",
    )


@pytest.mark.parametrize("quote", ["quiet ocean", "amber ocean"])
def test_calibrated_dense_evidence_is_monotonic_in_lexical_overlap(
    quote: str,
) -> None:
    candidate = make_ranked_chunk(1, quote, channel="dense:primary").model_copy(
        update={"rerank_rank": 1, "rerank_score": 0.91}
    )
    policy = RetrievalPolicy(
        dense_semantic_enabled=True,
        dense_semantic_calibration_state="CONTROLLED_TEST_ONLY",
        dense_calibrated_vector_spaces=(_SPACE,),
    )
    context = _context(_QUERY).model_copy(update={"selected_slot": "primary"})

    evidence = EvidenceAssembler().assemble(
        (candidate,), policy, context=context
    )

    assert policy.minimum_span_overlap == 0.2
    # unit_synthetic：少量字符重叠不改变候选资格，但无关原文不支持事实。
    assert semantic_candidate_allowed(candidate, policy, context)
    assert not evidence


@pytest.mark.parametrize(
    "boundary",
    ["uncalibrated", "slot", "rerank", "failed", "lexical", "uncitable"],
)
@pytest.mark.parametrize("quote", ["quiet ocean", "amber ocean"])
def test_semantic_fallback_retains_its_preconditions(
    boundary: str,
    quote: str,
) -> None:
    candidate = make_ranked_chunk(
        1,
        quote,
        channel="lexical:fts5" if boundary == "lexical" else "dense:primary",
    ).model_copy(update={"rerank_rank": None if boundary == "rerank" else 1})
    if boundary == "uncitable":
        chunk = candidate.hydrated.chunk
        chunk = chunk.model_copy(
            update={
                "source_spans": tuple(
                    span.model_copy(update={"is_citable": False})
                    for span in chunk.source_spans
                )
            }
        )
        candidate = candidate.model_copy(
            update={
                "hydrated": candidate.hydrated.model_copy(
                    update={"chunk": chunk}
                )
            }
        )
    policy = (
        RetrievalPolicy()
        if boundary == "uncalibrated"
        else RetrievalPolicy(
            dense_semantic_enabled=True,
            dense_semantic_calibration_state="CONTROLLED_TEST_ONLY",
            dense_calibrated_vector_spaces=(_SPACE,),
        )
    )
    context = _context(_QUERY).model_copy(
        update={
            "selected_slot": None if boundary == "slot" else "primary",
            "rerank_mode": "RERANK_FAILED"
            if boundary == "failed"
            else "provider",
        }
    )

    assert not EvidenceAssembler().assemble(
        (candidate,), policy, context=context
    )
