from datetime import UTC, datetime
from pathlib import Path

from rag_app.generation.answer import (
    AnswerResult,
    AnswerStatus,
    RefusalCode,
)
from rag_app.generation.evidence import EvidenceBundle
from rag_app.query_service import (
    QueryDependencies,
    QueryService,
    StageEvent,
    StageName,
)
from rag_app.retrieval.hybrid import HybridRetrievalResult
from rag_app.retrieval.rerank import RerankStageResult
from rag_app.retrieval.rewrite import QueryVariants
from rag_app.state.conversations import ConversationStore


class _Rewriter:
    def rewrite(
        self,
        question: str,
        *,
        previous_questions: tuple[str, ...],
    ) -> QueryVariants:
        assert question == "当前问题"
        assert previous_questions == ("历史问题",)
        return QueryVariants(
            queries=(question, "独立问题"),
            resolved_query="独立问题",
            rewritten=True,
            call=None,
        )


class _Retriever:
    def retrieve(
        self,
        variants: QueryVariants,
        *,
        as_of: datetime,
    ) -> HybridRetrievalResult:
        assert variants.queries == ("当前问题", "独立问题")
        assert variants.resolved_query == "独立问题"
        assert as_of.tzinfo is not None
        return HybridRetrievalResult(
            candidates=(),
            query_count=1,
            embedding_calls=1,
        )


class _Reranker:
    def rerank(
        self,
        query: str,
        candidates: tuple[object, ...],
    ) -> RerankStageResult:
        assert query == "独立问题"
        assert candidates == ()
        return RerankStageResult(hits=(), call=None)


class _Assembler:
    def assemble(self, ranked_hits: tuple[object, ...]) -> EvidenceBundle:
        assert ranked_hits == ()
        return EvidenceBundle(
            items=(),
            rendered_json='{"evidence":[]}',
            token_count=4,
            quarantined_chunk_ids=(),
        )


class _Neighbors:
    def expand(self, ranked_hits: tuple[object, ...]) -> tuple[object, ...]:
        return ranked_hits


class _Answerer:
    def answer(
        self,
        question: str,
        evidence: EvidenceBundle,
    ) -> AnswerResult:
        assert question == "当前问题"
        assert evidence.items == ()
        return AnswerResult(
            status=AnswerStatus.REFUSED,
            answer=None,
            claims=(),
            refusal_code=RefusalCode.NO_EVIDENCE,
            model_calls=0,
            calls=(),
        )


def test_query_service_emits_only_stage_metadata_and_appends_question(
    tmp_path: Path,
) -> None:
    conversations = ConversationStore(
        tmp_path / "state.sqlite3",
        ttl_seconds=300,
        max_rounds=3,
    )
    conversations.initialize()
    now = datetime(2026, 7, 27, 5, 0, tzinfo=UTC)
    conversations.append_question("conversation", "历史问题", now=now)
    events: list[StageEvent] = []
    service = QueryService(
        dependencies=QueryDependencies(
            conversations=conversations,
            rewriter=_Rewriter(),  # type: ignore[arg-type]
            retriever=_Retriever(),  # type: ignore[arg-type]
            reranker=_Reranker(),  # type: ignore[arg-type]
            neighbors=_Neighbors(),  # type: ignore[arg-type]
            assembler=_Assembler(),  # type: ignore[arg-type]
            answerer=_Answerer(),  # type: ignore[arg-type]
        )
    )

    outcome = service.ask(
        trace_id="trace-1",
        conversation_id="conversation",
        question="当前问题",
        now=now,
        emit=events.append,
    )

    assert outcome.answer.refusal_code == RefusalCode.NO_EVIDENCE
    assert outcome.rewritten is True
    assert [event.stage for event in events] == [
        StageName.REWRITE,
        StageName.RETRIEVE,
        StageName.RERANK,
        StageName.ASSEMBLE,
        StageName.VALIDATE,
        StageName.COMPLETE,
    ]
    assert all("当前问题" not in str(event) for event in events)
    assert conversations.get_questions(
        "conversation",
        now=now,
    ) == ("历史问题", "当前问题")
