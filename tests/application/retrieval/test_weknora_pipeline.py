"""候选自然问答路径的来源与旧编排隔离测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rag_app.application.answering.natural_answer import NaturalCompletion
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.context_reader import ContextReadResult
from rag_app.application.retrieval.expansion import RuleBasedNormalizer
from rag_app.application.retrieval.reranking import RerankingOutcome
from rag_app.application.retrieval.weknora_pipeline import (
    WeKnoraStandardPipeline,
)
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import ProviderInvalidResponse
from rag_app.core.models import (
    ChannelHit,
    KnowledgeBaseScope,
    RetrievalPolicy,
    SearchRequest,
)
from rag_app.product.query_history import _completion
from tests.application.retrieval.helpers import make_ranked_chunk


def _scenario(answer: str = "甲方负责核对记录。[S1]") -> tuple[object, Mock]:
    ranked = make_ranked_chunk(1, "甲方负责核对记录。")
    chunk = ranked.hydrated.chunk
    hit = ChannelHit(
        revision_id=chunk.index_revision_id,
        chunk_id=chunk.chunk_id,
        document_id=chunk.version.document_id,
        document_version_id=chunk.version.document_version_id,
        role=chunk.role.value,
        section_id=chunk.section_id,
        content_sha256=chunk.content_sha256,
        channel="lexical:fts5",
        rank=1,
        raw_score=1.0,
    )
    snapshot = SimpleNamespace(
        revision=SimpleNamespace(
            index_revision_id=chunk.index_revision_id,
            index_fingerprint=f"sha256:{'1' * 64}",
        ),
        serving_fingerprint=f"sha256:{'2' * 64}",
        excluded_document_ids=(),
    )
    model = Mock()
    model.complete_natural.return_value = NaturalCompletion(
        text=answer, model="fixture-qwen", provider_calls=()
    )
    service = SimpleNamespace(
        _natural_model=model,
        _query_snapshot=Mock(return_value=snapshot),
        _source_catalog_context=Mock(
            return_value=((), True, f"sha256:{'3' * 64}")
        ),
        _analyzer=QueryAnalyzer(),
        _expander=RuleBasedNormalizer(),
        _policy=RetrievalPolicy(enabled_channels=("lexical",)),
        _lexical=SimpleNamespace(search=Mock(return_value=(hit,))),
        _hydrator=SimpleNamespace(
            hydrate=Mock(
                side_effect=lambda _snapshot, candidates: (
                    (ranked,) if candidates else ()
                )
            )
        ),
        _reranker=SimpleNamespace(
            rerank=Mock(
                return_value=RerankingOutcome(
                    candidates=(ranked,),
                    mode="rerank",
                    reason_code="RERANK_EXECUTED",
                )
            )
        ),
        _context_reader=SimpleNamespace(
            read=Mock(return_value=ContextReadResult(groups=()))
        ),
        _egress=object(),
        _record=Mock(),
        _planner=Mock(),
        _grounded=Mock(),
    )
    return service, model


def _request() -> SearchRequest:
    return SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=f"prj_{'a' * 32}",
            knowledge_base_id=f"kb_{'b' * 32}",
        ),
        text="谁负责核对记录？",
    )


def test_natural_path_uses_real_candidate_without_atom_chain() -> None:
    service, model = _scenario()

    result = WeKnoraStandardPipeline(service).run(
        _request(),
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    assert result.answer == "甲方负责核对记录。[S1]"
    assert result.validation_level == "citation_binding_only"
    assert result.references[0].chunk_ids
    assert result.references[0].document_id
    assert result.cited_aliases == ("S1",)
    assert model.complete_natural.call_count == 1
    assert model.validate_natural_sources.call_count == 1
    service._planner.plan.assert_not_called()
    service._grounded.answer.assert_not_called()
    status, metadata = _completion(result, None, False)
    assert status == "ANSWERED"
    assert metadata["engine_id"] == "wk-standard-v1"


@pytest.mark.parametrize("citation", ("[S9]", "[S100]", "[S0]", "[Sx]"))
def test_unbound_citation_is_rejected_before_publication(citation: str) -> None:
    service, model = _scenario(f"甲方负责核对记录。[S1]{citation}")

    with pytest.raises(ProviderInvalidResponse):
        WeKnoraStandardPipeline(service).run(
            _request(),
            engine_id="wk-standard-v1",
            cancellation=StreamCancellation(),
        )

    model.validate_natural_sources.assert_not_called()


def test_empty_retrieval_never_calls_generation() -> None:
    service, model = _scenario()
    service._lexical.search.return_value = ()
    service._reranker.rerank.return_value = RerankingOutcome(
        candidates=(), mode="bypass", reason_code="NO_CANDIDATES"
    )

    result = WeKnoraStandardPipeline(service).run(
        _request(),
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    assert result.answer is None
    assert result.reason_code == "NO_BOUNDED_SOURCE_PASSAGE"
    model.complete_natural.assert_not_called()
