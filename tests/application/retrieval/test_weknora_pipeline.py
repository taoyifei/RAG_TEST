"""候选自然问答路径的来源与旧编排隔离测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rag_app.adapters.providers.aliyun_chat import (
    ChatMessage,
    message_token_estimate,
)
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatConfig,
    openai_compatible_chat_payload,
)
from rag_app.application.answering.natural_answer import (
    NaturalCompletion,
    NaturalMessage,
    NaturalReference,
)
from rag_app.application.retrieval.analyzer import QueryAnalyzer
from rag_app.application.retrieval.context_reader import ContextReadResult
from rag_app.application.retrieval.expansion import RuleBasedNormalizer
from rag_app.application.retrieval.natural_context import (
    NaturalBudget,
    estimate_natural_messages,
)
from rag_app.application.retrieval.reranking import (
    RerankingOutcome,
    _natural_bounded_text,
)
from rag_app.application.retrieval.weknora_pipeline import (
    WeKnoraStandardPipeline,
    _Passage,
)
from rag_app.clients.resilience import StreamCancellation
from rag_app.core.models import (
    ChannelHit,
    KnowledgeBaseScope,
    RetrievalPolicy,
    SearchRequest,
    SourceSpan,
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
    assert metadata["pipeline_revision"] == "weknora-natural-v3-02"
    assert metadata["citation_status"] == "valid"
    assert metadata["policy_fingerprint"] == NaturalBudget().identity


@pytest.mark.parametrize("citation", ("[S9]", "[S100]", "[S0]", "[Sx]"))
def test_unbound_citation_is_recorded_without_public_answer(
    citation: str,
) -> None:
    service, model = _scenario(f"甲方负责核对记录。[S1]{citation}")

    result = WeKnoraStandardPipeline(service).run(
        _request(),
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    assert result.answer is None
    assert result.draft == f"甲方负责核对记录。[S1]{citation}"
    assert result.reason_code == "CITATION_INVALID"
    assert result.citation_status == "invalid"
    assert result.invalid_citations
    assert result.references == ()
    model.validate_natural_sources.assert_called_once()


def test_missing_citation_is_a_diagnostic_draft() -> None:
    service, _model = _scenario("甲方负责核对记录。")

    result = WeKnoraStandardPipeline(service).run(
        _request(),
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    assert result.answer is None
    assert result.draft == "甲方负责核对记录。"
    assert result.citation_status == "missing"
    assert result.reason_code == "CITATION_MISSING"


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
    assert result.reason_code == "NO_RETRIEVAL_MATERIAL"
    model.complete_natural.assert_not_called()


def test_history_adds_one_rewrite_but_keeps_original_retrieval() -> None:
    service, model = _scenario()
    model.complete_query_understanding.return_value = NaturalCompletion(
        text='{"query":"甲方在流程中由谁负责核对记录？"}',
        model="fixture-qwen",
        provider_calls=(),
    )
    request = _request().model_copy(
        update={"conversation_context": ("刚才谈到甲方的流程。",)}
    )

    result = WeKnoraStandardPipeline(service).run(
        request,
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    assert result.reason_code == "ANSWERED"
    assert service._lexical.search.call_count == 2
    assert model.complete_query_understanding.call_count == 1
    assert model.complete_natural.call_count == 1


def test_invalid_rewrite_falls_back_to_original_query() -> None:
    service, model = _scenario()
    model.complete_query_understanding.return_value = NaturalCompletion(
        text="not-json", model="fixture-qwen", provider_calls=()
    )
    request = _request().model_copy(
        update={"conversation_context": ("刚才谈到甲方的流程。",)}
    )

    result = WeKnoraStandardPipeline(service).run(
        request,
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    assert result.reason_code == "ANSWERED"
    assert service._lexical.search.call_count == 1
    assert any(
        call.args[1] == "weknora_query_understand"
        and call.args[2]["reason_code"] == "REWRITE_FORMAT_INVALID"
        for call in service._record.call_args_list
    )


def test_rewrite_cannot_drop_explicit_number_or_negation() -> None:
    service, model = _scenario()
    model.complete_query_understanding.return_value = NaturalCompletion(
        text='{"query":"甲方在30分钟内完成什么？"}',
        model="fixture-qwen",
        provider_calls=(),
    )
    request = _request().model_copy(
        update={
            "text": "甲方在30分钟内不能做什么？",
            "conversation_context": ("之前讨论甲方。",),
        }
    )

    WeKnoraStandardPipeline(service).run(
        request,
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    assert service._lexical.search.call_count == 1
    assert any(
        call.args[1] == "weknora_query_understand"
        and call.args[2]["reason_code"] == "REWRITE_CONSTRAINT_CHANGED"
        for call in service._record.call_args_list
    )


def test_rewrite_cannot_drop_explicit_document_title() -> None:
    service, model = _scenario()
    model.complete_query_understanding.return_value = NaturalCompletion(
        text='{"query":"由谁负责审批？"}',
        model="fixture-qwen",
        provider_calls=(),
    )
    request = _request().model_copy(
        update={
            "text": "只根据《电子资源管理办法》说明由谁审批？",
            "conversation_context": ("之前讨论了另一个办法。",),
        }
    )

    WeKnoraStandardPipeline(service).run(
        request,
        engine_id="wk-standard-v1",
        cancellation=StreamCancellation(),
    )

    assert service._lexical.search.call_count == 1
    assert any(
        call.args[1] == "weknora_query_understand"
        and call.args[2]["reason_code"] == "REWRITE_CONSTRAINT_CHANGED"
        for call in service._record.call_args_list
    )


def test_oversized_parent_uses_hit_source_range() -> None:
    service, _model = _scenario()
    pipeline = WeKnoraStandardPipeline(service)
    ranked = make_ranked_chunk(1, "原文")
    chunk = ranked.hydrated.chunk
    original_span = chunk.source_spans[0]
    text = "甲" * 6000 + "目标片段" + "乙" * 6000
    parent_span = SourceSpan.model_validate(
        {
            **original_span.model_dump(mode="python"),
            "chunk_start_char": 0,
            "chunk_end_char": len(text),
            "source_start_char": 0,
            "source_end_char": len(text),
        }
    )
    hit_span = SourceSpan.model_validate(
        {
            **original_span.model_dump(mode="python"),
            "chunk_start_char": 0,
            "chunk_end_char": 4,
            "source_start_char": 6000,
            "source_end_char": 6004,
        }
    )
    passage = _Passage(
        text=text,
        reference=NaturalReference(
            alias="S1",
            document_id=chunk.version.document_id,
            document_version_id=chunk.version.document_version_id,
            document_title="测试原件",
            chunk_ids=(chunk.chunk_id,),
            source_spans=(parent_span,),
            parent_ranges=((0, len(text)),),
            citation_basis="original",
            source_complete=True,
        ),
        hit_sources=((chunk.chunk_id, hit_span),),
    )

    messages, sent, decisions = pipeline._fit_messages(
        _request(), (passage,), NaturalBudget()
    )

    assert "目标片段" in sent[0].text
    assert sent[0].reference.source_complete is False
    assert sent[0].reference.parent_ranges[0][0] > 0
    assert (
        sent[0].reference.source_spans[0].source_start_char
        == (sent[0].reference.parent_ranges[0][0])
    )
    assert decisions == ((chunk.chunk_id, "STRUCTURALLY_PARTIAL"),)
    assert pipeline._message_tokens(messages) <= NaturalBudget().input_limit


def test_later_candidate_can_fill_budget_after_oversized_prefix() -> None:
    service, _model = _scenario()
    pipeline = WeKnoraStandardPipeline(service)
    ranked = make_ranked_chunk(1, "可用材料")
    chunk = ranked.hydrated.chunk
    reference = NaturalReference(
        alias="S9",
        document_id=chunk.version.document_id,
        document_version_id=chunk.version.document_version_id,
        document_title="测试原件",
        chunk_ids=(chunk.chunk_id,),
        source_spans=chunk.source_spans,
        citation_basis="original",
        source_complete=False,
    )
    large = _Passage(text="长" * 6000, reference=reference)
    small = _Passage(text="可用材料", reference=reference)

    _messages, sent, decisions = pipeline._fit_messages(
        _request(), (*((large,) * 8), small), NaturalBudget()
    )

    assert len(sent) == 1
    assert sent[0].reference.alias == "S1"
    assert sent[0].text == "可用材料"
    assert decisions[-1][1] == "INCLUDED_FULL"


def test_partial_parent_only_reports_children_in_sent_ranges() -> None:
    service, _model = _scenario()
    pipeline = WeKnoraStandardPipeline(service)
    ranked = make_ranked_chunk(1, "原文")
    chunk = ranked.hydrated.chunk
    original = chunk.source_spans[0]
    first = SourceSpan.model_validate(
        {
            **original.model_dump(mode="python"),
            "chunk_start_char": 0,
            "chunk_end_char": 4000,
            "source_start_char": 0,
            "source_end_char": 4000,
        }
    )
    second = SourceSpan.model_validate(
        {
            **original.model_dump(mode="python"),
            "chunk_start_char": 4000,
            "chunk_end_char": 8000,
            "source_start_char": 4000,
            "source_end_char": 8000,
        }
    )
    first_hit = first.model_copy(
        update={
            "chunk_start_char": 0,
            "chunk_end_char": 8,
            "source_start_char": 0,
            "source_end_char": 8,
        }
    )
    second_hit = second.model_copy(
        update={
            "chunk_start_char": 0,
            "chunk_end_char": 8,
            "source_start_char": 4000,
            "source_end_char": 4008,
        }
    )
    passage = _Passage(
        text="甲" * 4000 + "乙" * 4000,
        reference=NaturalReference(
            alias="S1",
            document_id=chunk.version.document_id,
            document_version_id=chunk.version.document_version_id,
            document_title="测试原件",
            chunk_ids=("hit-first", "hit-second"),
            source_spans=(first, second),
            parent_ranges=((0, 8000),),
            citation_basis="original",
            source_complete=True,
        ),
        hit_sources=(("hit-first", first_hit), ("hit-second", second_hit)),
    )

    _messages, sent, decisions = pipeline._fit_messages(
        _request(), (passage,), NaturalBudget()
    )

    assert decisions == (("hit-first", "STRUCTURALLY_PARTIAL"),)
    assert sent[0].reference.chunk_ids == ("hit-first",)
    assert sent[0].reference.parent_ranges == ((0, 4000),)
    assert sent[0].text == "甲" * 4000


def test_natural_budget_matches_final_provider_message_gate() -> None:
    budget = NaturalBudget()
    messages = (
        NaturalMessage(role="system", content="按来源回答。"),
        NaturalMessage(role="user", content="引用材料 [S1] 回答。"),
    )
    wire = tuple(ChatMessage(role=m.role, content=m.content) for m in messages)
    config = OpenAICompatibleChatConfig(
        model="fixture-qwen",
        egress_allowed=True,
        max_input_tokens=budget.input_limit,
        max_output_tokens=budget.output_tokens,
    )

    payload = openai_compatible_chat_payload(wire, config, stream=True)

    assert estimate_natural_messages(messages) == message_token_estimate(wire)
    assert payload["max_tokens"] == budget.output_tokens
    assert budget.input_limit == 5000


def test_candidate_rerank_view_keeps_middle_fact_and_original_text() -> None:
    ranked = make_ranked_chunk(1, "前" * 1800 + "中间关键内容" + "后" * 1800)
    canonical = ranked.hydrated.chunk.citation_text

    view, window = _natural_bounded_text(ranked, "中间关键内容", 1200)

    assert "中间关键内容" in view
    assert len(view) <= 1200
    assert window[0] > 0
    assert ranked.hydrated.chunk.citation_text == canonical
