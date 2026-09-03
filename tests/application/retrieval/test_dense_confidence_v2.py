from __future__ import annotations

from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.confidence import ConfidenceEvaluator
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.models import (
    ConfidenceStatus,
    EvidenceSelectionContext,
    KnowledgeBaseScope,
    QueryAnalysis,
    QueryKind,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
)
from tests.application.retrieval.helpers import make_ranked_chunk

_SCOPE = KnowledgeBaseScope(
    project_id=f"prj_{'1' * 32}",
    knowledge_base_id=f"kb_{'2' * 32}",
)
_VECTOR_SPACE = "primary:fake-semantic:model:3:l2:1"


def _analysis() -> QueryAnalysis:
    return QueryAnalyzer().analyze(
        SearchRequest(scope=_SCOPE, text="设备在低温时如何保护")
    )


def _dense_candidate() -> RankedChunk:
    return make_ranked_chunk(
        1,
        "环境温度低于零度时应先预热设备",
        channel="dense:primary",
    ).model_copy(update={"rerank_rank": 1, "rerank_score": 0.9})


def test_dense_only_is_refused_without_calibration() -> None:
    candidate = _dense_candidate()
    policy = RetrievalPolicy()
    evidence = EvidenceAssembler().assemble(
        (candidate,),
        policy,
        context=EvidenceSelectionContext(
            analysis=_analysis(),
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="lexical_overlap",
            selected_slot="primary",
        ),
    )

    decision = ConfidenceEvaluator().evaluate(
        _analysis(),
        QueryKind.SIMPLE_FACT,
        (candidate,),
        evidence,
        (),
        policy=policy,
        rerank_mode="lexical_overlap",
        selected_vector_space=_VECTOR_SPACE,
    )

    assert decision.status is ConfidenceStatus.INSUFFICIENT_EVIDENCE


def test_controlled_fake_semantic_path_can_answer() -> None:
    candidate = _dense_candidate()
    policy = RetrievalPolicy(
        dense_semantic_enabled=True,
        dense_semantic_calibration_state="CONTROLLED_TEST_ONLY",
        dense_calibrated_vector_spaces=(_VECTOR_SPACE,),
    )
    evidence = EvidenceAssembler().assemble(
        (candidate,),
        policy,
        context=EvidenceSelectionContext(
            analysis=_analysis(),
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="lexical_overlap",
            selected_slot="primary",
        ),
    )

    decision = ConfidenceEvaluator().evaluate(
        _analysis(),
        QueryKind.SIMPLE_FACT,
        (candidate,),
        evidence,
        (),
        policy=policy,
        rerank_mode="lexical_overlap",
        selected_vector_space=_VECTOR_SPACE,
    )

    assert decision.status is ConfidenceStatus.ANSWERABLE
    assert "CONTROLLED_TEST_ONLY" in decision.reason_codes
