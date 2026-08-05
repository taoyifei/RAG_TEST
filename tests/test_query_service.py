import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from rag_app.generation.answer import (
    AnswerClaim,
    AnswerMode,
    AnswerResult,
    AnswerStatus,
    ClaimSupport,
    RefusalCode,
)
from rag_app.generation.evidence import EvidenceBundle
from rag_app.model_contracts import VerifiedClaimContext
from rag_app.query_service import (
    QueryDependencies,
    QueryService,
    StageEvent,
    StageName,
)
from rag_app.retrieval.hybrid import HybridRetrievalResult
from rag_app.retrieval.neighbors import NeighborExpansionResult
from rag_app.retrieval.rerank import RerankedHit, RerankStageResult
from rag_app.retrieval.rewrite import QueryVariants
from rag_app.state.answer_cache import AnswerCache, AnswerCacheKey
from rag_app.state.conversations import ConversationStore
from rag_app.tracing.models import TraceIdentity


class _Rewriter:
    def rewrite(
        self,
        question: str,
        *,
        previous_questions: tuple[str, ...],
        verified_claims: tuple[VerifiedClaimContext, ...],
    ) -> QueryVariants:
        assert question == "当前问题"
        assert previous_questions == ("历史问题",)
        assert verified_claims == ()
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
    def expand_with_trace(
        self,
        ranked_hits: tuple[RerankedHit, ...],
    ) -> NeighborExpansionResult:
        return NeighborExpansionResult(
            hits=ranked_hits,
            decisions=(),
        )


class _Answerer:
    def answer(
        self,
        question: str,
        evidence: EvidenceBundle,
        *,
        rerank_scores: tuple[float, ...] = (),
    ) -> AnswerResult:
        del rerank_scores
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


class _CachedRewriter:
    def rewrite(
        self,
        question: str,
        *,
        previous_questions: tuple[str, ...],
        verified_claims: tuple[VerifiedClaimContext, ...],
    ) -> QueryVariants:
        assert previous_questions == ()
        assert verified_claims == ()
        return QueryVariants(
            queries=(question,),
            resolved_query=question,
            rewritten=False,
            call=None,
        )


class _MustNotRun:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"缓存命中不应访问 {name}。")


class _CachedAnswerer:
    def revision(self) -> str:
        return "sha256:" + "c" * 64

    def answer(self, *args: object, **kwargs: object) -> AnswerResult:
        del args, kwargs
        raise AssertionError("缓存命中不应调用回答模型。")


class _CountingPipeline:
    """记录同键并发请求是否重复执行昂贵阶段。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts = {
            "retrieve": 0,
            "rerank": 0,
            "assemble": 0,
            "answer": 0,
        }

    def _increment(self, stage: str) -> None:
        with self._lock:
            self.counts[stage] += 1

    def revision(self) -> str:
        return "sha256:" + "c" * 64

    def retrieve(
        self,
        variants: QueryVariants,
        *,
        as_of: datetime,
    ) -> HybridRetrievalResult:
        del variants, as_of
        self._increment("retrieve")
        time.sleep(0.05)
        return HybridRetrievalResult(
            candidates=(),
            query_count=1,
            embedding_calls=1,
        )

    def rerank(
        self,
        query: str,
        candidates: tuple[object, ...],
    ) -> RerankStageResult:
        del query, candidates
        self._increment("rerank")
        return RerankStageResult(hits=(), call=None)

    def expand_with_trace(
        self,
        ranked_hits: tuple[RerankedHit, ...],
    ) -> NeighborExpansionResult:
        return NeighborExpansionResult(hits=ranked_hits, decisions=())

    def assemble(self, ranked_hits: tuple[object, ...]) -> EvidenceBundle:
        del ranked_hits
        self._increment("assemble")
        return EvidenceBundle(
            items=(),
            rendered_json='{"evidence":[]}',
            token_count=4,
            quarantined_chunk_ids=(),
        )

    def answer(
        self,
        question: str,
        evidence: EvidenceBundle,
        *,
        rerank_scores: tuple[float, ...] = (),
    ) -> AnswerResult:
        del question, evidence, rerank_scores
        self._increment("answer")
        time.sleep(0.05)
        support = ClaimSupport(
            evidence_id="E1",
            chunk_id="chunk-1",
            quote="验收测试包括功能验收。",
            locator="规范.docx > 验收",
        )
        return AnswerResult(
            status=AnswerStatus.ANSWERED,
            answer="验收测试包括功能验收。",
            claims=(
                AnswerClaim(
                    text="验收测试包括功能验收。",
                    supports=(support,),
                ),
            ),
            refusal_code=None,
            model_calls=1,
            calls=(),
            answer_mode=AnswerMode.ANSWERED,
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
    assert conversations.get_rewrite_context(
        "conversation",
        now=now,
    ).verified_claims == ()


def test_exact_cache_hit_skips_retrieval_rerank_and_llm(
    tmp_path: Path,
) -> None:
    conversations = ConversationStore(
        tmp_path / "state.sqlite3",
        ttl_seconds=300,
        max_rounds=3,
    )
    conversations.initialize()
    cache = AnswerCache(tmp_path / "answer-cache.sqlite3")
    cache.initialize()
    now = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
    identity = TraceIdentity(
        pipeline_fingerprint="sha256:" + "a" * 64,
        serving_fingerprint="sha256:" + "b" * 64,
        release_revision="release",
        active_collection="active",
        index_manifest_sha256="d" * 64,
        payload_schema_version=2,
    )
    support = ClaimSupport(
        evidence_id="E1",
        chunk_id="chunk-1",
        quote="验收测试包括功能验收。",
        locator="规范.docx > 验收",
    )
    answer = AnswerResult(
        status=AnswerStatus.ANSWERED,
        answer="验收测试包括功能验收。",
        claims=(
            AnswerClaim(
                text="验收测试包括功能验收。",
                supports=(support,),
            ),
        ),
        refusal_code=None,
        model_calls=1,
        calls=(),
        answer_mode=AnswerMode.ANSWERED,
    )
    key = AnswerCacheKey.from_inputs(
        resolved_query="验收测试包括哪些内容？",
        conversation_context_digest="",
        index_manifest_sha256=identity.index_manifest_sha256,
        serving_fingerprint=identity.serving_fingerprint,
        access_mode="source-only",
        answer_revision="sha256:" + "c" * 64,
    )
    cache.store(key, answer, now=now)
    events: list[StageEvent] = []
    service = QueryService(
        dependencies=QueryDependencies(
            conversations=conversations,
            rewriter=_CachedRewriter(),  # type: ignore[arg-type]
            retriever=_MustNotRun(),  # type: ignore[arg-type]
            reranker=_MustNotRun(),  # type: ignore[arg-type]
            neighbors=_MustNotRun(),  # type: ignore[arg-type]
            assembler=_MustNotRun(),  # type: ignore[arg-type]
            answerer=_CachedAnswerer(),  # type: ignore[arg-type]
        ),
        trace_identity=identity,
        answer_cache=cache,
    )

    started = time.perf_counter()
    outcome = service.ask(
        trace_id="cache-hit",
        conversation_id="conversation",
        question="验收测试包括哪些内容？",
        now=now,
        emit=events.append,
    )

    assert time.perf_counter() - started < 0.2
    assert outcome.answer.model_calls == 0
    assert outcome.answer.answer == "验收测试包括功能验收。"
    assert [event.stage for event in events] == [
        StageName.REWRITE,
        StageName.VALIDATE,
        StageName.COMPLETE,
    ]


def test_same_key_concurrency_runs_retrieval_and_llm_once(
    tmp_path: Path,
) -> None:
    conversations = ConversationStore(
        tmp_path / "state.sqlite3",
        ttl_seconds=300,
        max_rounds=3,
    )
    conversations.initialize()
    cache = AnswerCache(tmp_path / "answer-cache.sqlite3")
    cache.initialize()
    identity = TraceIdentity(
        pipeline_fingerprint="sha256:" + "a" * 64,
        serving_fingerprint="sha256:" + "b" * 64,
        release_revision="release",
        active_collection="active",
        index_manifest_sha256="d" * 64,
        payload_schema_version=2,
    )
    pipeline = _CountingPipeline()
    service = QueryService(
        dependencies=QueryDependencies(
            conversations=conversations,
            rewriter=_CachedRewriter(),  # type: ignore[arg-type]
            retriever=pipeline,  # type: ignore[arg-type]
            reranker=pipeline,  # type: ignore[arg-type]
            neighbors=pipeline,  # type: ignore[arg-type]
            assembler=pipeline,  # type: ignore[arg-type]
            answerer=pipeline,  # type: ignore[arg-type]
        ),
        trace_identity=identity,
        answer_cache=cache,
    )
    start = threading.Barrier(4)
    results: list[AnswerResult] = []
    result_lock = threading.Lock()
    now = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)

    def ask(index: int) -> None:
        start.wait()
        outcome = service.ask(
            trace_id=f"concurrent-{index}",
            conversation_id=f"conversation-{index}",
            question="验收测试包括哪些内容？",
            now=now,
            emit=lambda _: None,
        )
        with result_lock:
            results.append(outcome.answer)

    threads = [
        threading.Thread(target=ask, args=(index,))
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert pipeline.counts == {
        "retrieve": 1,
        "rerank": 1,
        "assemble": 1,
        "answer": 1,
    }
    assert len(results) == 4
    assert sorted(result.model_calls for result in results) == [0, 0, 0, 1]
