"""从条件改写到发布门禁的同步查询编排。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from rag_app.clients.intent_classifier import IntentClassifier
from rag_app.clients.model_services import ExternalCallAudit
from rag_app.clients.resilience import (
    StreamCancellation,
    StreamCancelledError,
)
from rag_app.generation.answer import (
    AnswerClaim,
    AnswerGenerator,
    AnswerResult,
    AnswerStatus,
)
from rag_app.generation.evidence import (
    EvidenceAssembler,
    EvidenceBundle,
    EvidenceDecision,
    InvalidEvidencePayloadError,
)
from rag_app.generation.question_profile import (
    PrimaryOperation,
    QuestionProfile,
    RequestedSlot,
    RouteSource,
    extract_structural_signals,
    legacy_question_profile,
)
from rag_app.generation.semantic_router import (
    IntentRouterMode,
    SemanticQuestionRouter,
)
from rag_app.model_contracts import VerifiedClaimContext
from rag_app.retrieval.fusion import FusedHit
from rag_app.retrieval.hybrid import HybridRetriever
from rag_app.retrieval.neighbors import (
    NeighborDecision,
    NeighborExpander,
)
from rag_app.retrieval.rerank import RerankStage, RerankStageResult
from rag_app.retrieval.rewrite import (
    QueryRewriter,
    RewriteTokenLimitError,
)
from rag_app.state.answer_cache import AnswerCache, AnswerCacheKey
from rag_app.state.conversations import ConversationStore
from rag_app.tracing.models import (
    SpanKind,
    SpanStatus,
    TraceIdentity,
    TraceMode,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.recorder import (
    TraceRecorder,
    TraceSession,
    TraceSpanFinish,
    TraceSpanHandle,
    TraceSpanSpec,
)

__all__ = [
    "AnswerStartEvent",
    "QueryDependencies",
    "QueryOutcome",
    "QueryService",
    "StageEvent",
    "StageName",
    "ValidatedClaimEvent",
]


class StageName(StrEnum):
    """可流式展示但不含业务内容的查询阶段。"""

    REWRITE = "rewrite"
    RETRIEVE = "retrieve"
    RERANK = "rerank"
    ASSEMBLE = "assemble"
    VALIDATE = "validate"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class StageEvent:
    """只含 trace、阶段、耗时和计数的事件。"""

    trace_id: str
    stage: StageName
    elapsed_ms: int
    metrics: dict[str, int | bool | str]


@dataclass(frozen=True, slots=True)
class AnswerStartEvent:
    """回答模型开始前可安全发布的无正文事件。"""

    trace_id: str
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class ValidatedClaimEvent:
    """已经通过全部引用门禁且可立即展示的一条 claim。"""

    trace_id: str
    elapsed_ms: int
    claim_index: int
    claim: AnswerClaim


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """完整查询结束后的已验证结果。"""

    trace_id: str
    answer: AnswerResult
    rewritten: bool
    stage_count: int
    calls: tuple[ExternalCallAudit, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryDependencies:
    """查询链必需的有状态组件。"""

    conversations: ConversationStore
    rewriter: QueryRewriter
    retriever: HybridRetriever
    reranker: RerankStage
    neighbors: NeighborExpander
    assembler: EvidenceAssembler
    answerer: AnswerGenerator
    question_profile_router: SemanticQuestionRouter | None = None
    intent_classifier: IntentClassifier | None = None


@dataclass(frozen=True, slots=True)
class _StageEmitter:
    """绑定一次请求的非敏感阶段事件上下文。"""

    callback: Callable[[StageEvent], None]
    trace_id: str
    started: float
    clock: Callable[[], float]

    def emit(
        self,
        stage: StageName,
        metrics: dict[str, int | bool | str],
    ) -> None:
        """发送不含问题、证据或答案的阶段事件。

        Args:
            stage: 当前查询阶段。
            metrics: 仅含非敏感计数或状态的指标。

        Returns:
            无返回值。

        """
        self.callback(
            StageEvent(
                trace_id=self.trace_id,
                stage=stage,
                elapsed_ms=max(
                    0,
                    round((self.clock() - self.started) * 1000),
                ),
                metrics=metrics,
            )
        )


@dataclass(frozen=True, slots=True)
class _QueryRequest:
    """一次查询的原始执行参数。"""

    trace_id: str
    conversation_id: str
    question: str
    now: datetime
    emit: Callable[[StageEvent], None]
    emit_answer: (
        Callable[[AnswerStartEvent | ValidatedClaimEvent], None] | None
    ) = None
    cancellation: StreamCancellation | None = None


class QueryService:
    """顺序执行检索生成链，并通过 callback 发非敏感阶段事件。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        dependencies: QueryDependencies,
        clock: Callable[[], float] = time.monotonic,
        trace_recorder: TraceRecorder | None = None,
        trace_identity: (
            TraceIdentity | Callable[[], TraceIdentity] | None
        ) = None,
        default_trace_mode: TraceMode = TraceMode.SAFE,
        answer_cache: AnswerCache | None = None,
        access_mode: str = "source-only",
    ) -> None:
        """保存完整查询链依赖。

        Args:
            dependencies: 查询链必需的有状态组件。
            clock: 非敏感阶段耗时的单调时钟。
            trace_recorder: 可选的独立 Trace writer。
            trace_identity: 与 recorder 同时提供的运行身份或实时提供器。
            default_trace_mode: 普通 chat 的 SAFE/DIAGNOSTIC 边界。
            answer_cache: 可选的索引与回答协议绑定精确缓存。
            access_mode: 缓存键绑定的访问范围模式。

        """
        if trace_recorder is not None and trace_identity is None:
            raise ValueError("Trace recorder 和 identity 必须同时配置。")
        if (
            trace_recorder is None
            and trace_identity is not None
            and answer_cache is None
        ):
            raise ValueError("独立 Trace identity 只能用于回答缓存。")
        if answer_cache is not None and trace_identity is None:
            raise ValueError("回答缓存必须绑定实时 Trace identity。")
        self._dependencies = dependencies
        self._clock = clock
        self._trace_recorder = trace_recorder
        self._trace_identity = trace_identity
        if default_trace_mode is TraceMode.FULL:
            raise ValueError("普通 chat 不能配置为 FULL Trace。")
        self._default_trace_mode = default_trace_mode
        self._answer_cache = answer_cache
        self._access_mode = access_mode

    def ask(
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
    ) -> QueryOutcome:
        """执行一次查询，最终内容仅来自 AnswerResult。

        Args:
            trace_id: 本次请求稳定追踪标识。
            conversation_id: TTL 多轮会话标识。
            question: 当前原始问题，不进入阶段事件。
            now: 有效期判断和会话 TTL 的带时区时点。
            emit: 同步阶段事件 callback。

        Returns:
            已验证回答或拒答。

        """
        return self._ask(
            _QueryRequest(
                trace_id=trace_id,
                conversation_id=conversation_id,
                question=question,
                now=now,
                emit=emit,
            ),
            self._default_trace_mode,
        )

    def ask_debug(
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
    ) -> QueryOutcome:
        """执行管理员 FULL Debug 查询。

        Args:
            trace_id: 本次请求稳定追踪标识。
            conversation_id: TTL 多轮会话标识。
            question: 当前原始问题。
            now: 有效期判断和会话 TTL 的带时区时点。
            emit: 同步阶段事件 callback。

        Returns:
            已验证回答或拒答。

        Raises:
            RuntimeError: Trace recorder 未配置。

        """
        if self._trace_recorder is None:
            raise RuntimeError("FULL Debug Trace recorder 未配置。")
        return self._ask(
            _QueryRequest(
                trace_id=trace_id,
                conversation_id=conversation_id,
                question=question,
                now=now,
                emit=emit,
            ),
            TraceMode.FULL,
        )

    def ask_stream(  # noqa: PLR0913
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
        emit_answer: Callable[
            [AnswerStartEvent | ValidatedClaimEvent],
            None,
        ],
        cancellation: StreamCancellation,
    ) -> QueryOutcome:
        """执行普通查询并增量回调已验证 claim。

        Args:
            trace_id: 本次请求稳定追踪标识。
            conversation_id: TTL 多轮会话标识。
            question: 当前原始问题。
            now: 有效期判断和会话 TTL 的带时区时点。
            emit: 同步阶段事件 callback。
            emit_answer: 回答开始及已验证 claim callback。
            cancellation: 客户端断开时关闭上游模型流的令牌。

        Returns:
            与 ``ask`` 相同的 canonical 最终回答或拒答。

        """
        return self._ask(
            _QueryRequest(
                trace_id=trace_id,
                conversation_id=conversation_id,
                question=question,
                now=now,
                emit=emit,
                emit_answer=emit_answer,
                cancellation=cancellation,
            ),
            self._default_trace_mode,
        )

    def ask_debug_stream(  # noqa: PLR0913
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
        emit_answer: Callable[
            [AnswerStartEvent | ValidatedClaimEvent],
            None,
        ],
        cancellation: StreamCancellation,
    ) -> QueryOutcome:
        """执行 FULL Debug 查询并增量回调已验证 claim。

        Args:
            trace_id: 本次请求稳定追踪标识。
            conversation_id: TTL 多轮会话标识。
            question: 当前原始问题。
            now: 有效期判断和会话 TTL 的带时区时点。
            emit: 同步阶段事件 callback。
            emit_answer: 回答开始及已验证 claim callback。
            cancellation: 客户端断开时关闭上游模型流的令牌。

        Returns:
            与 ``ask_debug`` 相同的 canonical 最终结果。

        Raises:
            RuntimeError: Trace recorder 未配置。

        """
        if self._trace_recorder is None:
            raise RuntimeError("FULL Debug Trace recorder 未配置。")
        return self._ask(
            _QueryRequest(
                trace_id=trace_id,
                conversation_id=conversation_id,
                question=question,
                now=now,
                emit=emit,
                emit_answer=emit_answer,
                cancellation=cancellation,
            ),
            TraceMode.FULL,
        )

    def _ask(  # noqa: PLR0912, PLR0915
        self,
        request: _QueryRequest,
        mode: TraceMode,
    ) -> QueryOutcome:
        """执行带阶段事件和可选审计追踪的完整问答编排。

        成功路径依次完成上下文、改写、检索、重排、邻居扩展、证据组装、
        回答校验和发布；仅在回答完成后写入会话。任一阶段失败时会结束
        当前 span 和 trace，再原样传播异常。

        Args:
            request: 本次查询的身份、问题、时间和事件回调。
            mode: 控制审计追踪降级或强制成功建立的模式。

        Returns:
            包含回答、改写状态、阶段数和外部调用审计的查询结果。

        Raises:
            ValueError: ``request.trace_id`` 为空。

        """
        if not request.trace_id:
            raise ValueError("trace_id 不能为空。")
        started = self._clock()
        event_emitter = _StageEmitter(
            callback=request.emit,
            trace_id=request.trace_id,
            started=started,
            clock=self._clock,
        )
        event_count = 0
        session = self._begin_trace(request, mode)
        current_span: TraceSpanHandle | None = None
        cache_key: AnswerCacheKey | None = None
        singleflight_stack = ExitStack()
        failure_stage = "context.load"
        try:
            current_span = _start_span(
                session,
                "context.load",
                SpanKind.STORAGE,
            )
            context = self._dependencies.conversations.get_rewrite_context(
                request.conversation_id,
                now=request.now,
            )
            previous_questions = context.questions
            _finish_span(
                session,
                current_span,
                DecisionCode.ACCEPTED,
                {
                    "history_count": len(previous_questions),
                    "verified_claim_count": len(context.verified_claims),
                },
            )
            current_span = None
            _full_artifact(
                session,
                "context",
                {
                    "question": request.question,
                    "history": list(previous_questions),
                    "verified_claims": [
                        claim.as_payload() for claim in context.verified_claims
                    ],
                },
            )

            failure_stage = "rewrite.decide"
            current_span = _start_span(
                session,
                failure_stage,
                SpanKind.CHAIN,
            )
            variants = self._dependencies.rewriter.rewrite(
                request.question,
                previous_questions=previous_questions,
                verified_claims=context.verified_claims,
            )
            rewrite_reason = _reason_from_trace(
                variants.trace,
                DecisionCode.REWRITE_OK
                if variants.rewritten
                else DecisionCode.NO_CONTEXT_SIGNAL,
            )
            if (
                variants.call is not None
                and session is not None
                and current_span is not None
            ):
                _completed_span(
                    session,
                    TraceSpanSpec(
                        name="llm.rewrite",
                        kind=SpanKind.LLM,
                        parent_span_id=current_span.span_id,
                        reason_code=rewrite_reason,
                        attributes=_external_call_attributes(variants.call),
                        duration_ms=round(variants.call.elapsed_seconds * 1000),
                    ),
                )
            _finish_span(
                session,
                current_span,
                rewrite_reason,
                _rewrite_attributes(variants.trace),
            )
            current_span = None
            _full_artifact(session, "rewrite", variants.trace)
            event_count += 1
            event_emitter.emit(
                StageName.REWRITE,
                {
                    "rewritten": variants.rewritten,
                    "query_count": len(variants.queries),
                },
            )

            if self._answer_cache is not None:
                failure_stage = "cache.lookup"
                cache_key = self._answer_cache_key(
                    variants.resolved_query,
                    context_digest=_conversation_context_digest(
                        variants.rewritten,
                        previous_questions,
                        context.verified_claims,
                    ),
                )
                current_span = _start_span(
                    session,
                    failure_stage,
                    SpanKind.STORAGE,
                )
                cached = self._answer_cache.lookup(
                    cache_key,
                    now=request.now,
                )
                _finish_span(
                    session,
                    current_span,
                    DecisionCode.ACCEPTED,
                    {"cache_status": "hit" if cached else "miss"},
                )
                current_span = None
                cache_outcome = "cache.hit" if cached else "cache.miss"
                outcome_span = _start_span(
                    session,
                    cache_outcome,
                    SpanKind.STORAGE,
                )
                _finish_span(
                    session,
                    outcome_span,
                    DecisionCode.ACCEPTED,
                    {"cache_status": cache_outcome.removeprefix("cache.")},
                )
                if cached is not None:
                    return self._publish_cached(
                        request=request,
                        answer=cached,
                        rewritten=variants.rewritten,
                        rewrite_call=variants.call,
                        event_count=event_count,
                        emitter=event_emitter,
                        session=session,
                    )

                wait_span = _start_span(
                    session,
                    "cache.wait",
                    SpanKind.STORAGE,
                )
                flight = singleflight_stack.enter_context(
                    self._answer_cache.singleflight(cache_key)
                )
                cached = flight.result
                if flight.waited and cached is None:
                    cached = self._answer_cache.lookup(
                        cache_key,
                        now=request.now,
                    )
                _finish_span(
                    session,
                    wait_span,
                    DecisionCode.ACCEPTED,
                    {
                        "waited": flight.waited,
                        "cache_status": (
                            "hit_after_wait"
                            if cached is not None
                            else (
                                "miss_after_wait" if flight.waited else "leader"
                            )
                        ),
                    },
                )
                if cached is not None:
                    hit_span = _start_span(
                        session,
                        "cache.hit",
                        SpanKind.STORAGE,
                    )
                    _finish_span(
                        session,
                        hit_span,
                        DecisionCode.ACCEPTED,
                        {
                            "cache_status": (
                                "singleflight"
                                if flight.result is not None
                                else "persistent_after_wait"
                            )
                        },
                    )
                    return self._publish_cached(
                        request=request,
                        answer=cached,
                        rewritten=variants.rewritten,
                        rewrite_call=variants.call,
                        event_count=event_count,
                        emitter=event_emitter,
                        session=session,
                    )

            failure_stage = "retrieve"
            current_span = _start_span(
                session,
                failure_stage,
                SpanKind.RETRIEVER,
            )
            retrieval = self._dependencies.retriever.retrieve(
                variants,
                as_of=request.now,
            )
            _record_retrieval(session, current_span, retrieval.trace)
            _finish_span(
                session,
                current_span,
                (
                    DecisionCode.RETRIEVAL_OK
                    if retrieval.candidates
                    else DecisionCode.RETRIEVAL_EMPTY
                ),
                {
                    "candidate_count": len(retrieval.candidates),
                    "query_count": retrieval.query_count,
                },
            )
            current_span = None
            _full_artifact(
                session,
                "retrieval",
                {
                    "queries": list(variants.queries),
                    "resolved_query": variants.resolved_query,
                    "trace": retrieval.trace,
                },
            )
            event_count += 1
            event_emitter.emit(
                StageName.RETRIEVE,
                {
                    "candidate_count": len(retrieval.candidates),
                    "embedding_calls": retrieval.embedding_calls,
                    "route_id": retrieval.route_id or "",
                    "route_confidence_milli": round(
                        retrieval.route_confidence * 1000
                    ),
                    "route_fallback": retrieval.route_fallback,
                },
            )

            failure_stage = "intent.route"
            current_span = _start_span(
                session,
                failure_stage,
                SpanKind.CHAIN,
            )
            route_started = self._clock()
            structural_signals = extract_structural_signals(
                variants.resolved_query
            )
            legacy_profile = legacy_question_profile(request.question)
            semantic_router = self._dependencies.question_profile_router
            fallback_call: ExternalCallAudit | None = None
            if semantic_router is None:
                active_mode = IntentRouterMode.LEGACY
                semantic_profile = _general_question_profile(
                    structural_signals.requested_slots,
                    reason_code="ROUTER_UNAVAILABLE",
                )
            else:
                active_mode = semantic_router.config.mode
                semantic_profile = semantic_router.route(
                    variants.resolved_query,
                    (
                        None
                        if retrieval.query_embedding is None
                        else retrieval.query_embedding.vector
                    ),
                    structural_signals,
                )
            if (
                active_mode is IntentRouterMode.HYBRID
                and semantic_router is not None
                and semantic_router.config.llm_fallback_enabled
                and semantic_profile.reason_code == "SEMANTIC_UNCERTAIN"
                and self._dependencies.intent_classifier is not None
            ):
                fallback = self._dependencies.intent_classifier.classify(
                    variants.resolved_query,
                    semantic_profile=semantic_profile,
                    structural_signals=structural_signals,
                )
                semantic_profile = fallback.profile
                fallback_call = fallback.call
            active_profile = (
                legacy_profile
                if active_mode
                in {IntentRouterMode.LEGACY, IntentRouterMode.SHADOW}
                else semantic_profile
            )
            if (
                fallback_call is not None
                and session is not None
                and current_span is not None
            ):
                _completed_span(
                    session,
                    TraceSpanSpec(
                        name="llm.intent_classifier",
                        kind=SpanKind.LLM,
                        parent_span_id=current_span.span_id,
                        reason_code=DecisionCode.ACCEPTED,
                        attributes=_external_call_attributes(fallback_call),
                        duration_ms=round(fallback_call.elapsed_seconds * 1000),
                    ),
                )
            _finish_span(
                session,
                current_span,
                DecisionCode.ACCEPTED,
                _intent_route_attributes(
                    active_mode=active_mode,
                    legacy_profile=legacy_profile,
                    semantic_profile=semantic_profile,
                    selected_profile=active_profile,
                    prototype_cache_ready=(
                        False
                        if semantic_router is None
                        else semantic_router.prototype_cache_ready
                    ),
                    llm_fallback_enabled=(
                        False
                        if semantic_router is None
                        else semantic_router.config.llm_fallback_enabled
                    ),
                    llm_fallback_used=fallback_call is not None
                    or (
                        active_mode is IntentRouterMode.HYBRID
                        and semantic_router is not None
                        and semantic_router.config.llm_fallback_enabled
                        and semantic_profile.reason_code
                        in {
                            "LLM_FALLBACK_CONFIDENT",
                            "LLM_FALLBACK_UNAVAILABLE",
                        }
                    ),
                    elapsed_ms=_elapsed_ms(route_started, self._clock),
                ),
            )
            current_span = None

            failure_stage = "rerank"
            current_span = _start_span(
                session,
                failure_stage,
                SpanKind.RERANKER,
            )
            reranked = self._dependencies.reranker.rerank(
                variants.resolved_query,
                retrieval.candidates,
            )
            _record_rerank(
                session,
                retrieval.candidates,
                reranked,
            )
            _finish_span(
                session,
                current_span,
                (
                    DecisionCode.SELECTED
                    if reranked.hits
                    else DecisionCode.RERANK_DROP
                ),
                {
                    "input_candidate_count": (reranked.input_candidate_count),
                    "output_scored_count": len(reranked.scored_hits),
                    "final_selected_count": len(reranked.hits),
                },
            )
            current_span = None
            _full_artifact(
                session,
                "rerank",
                _rerank_artifact(
                    variants.resolved_query,
                    retrieval.candidates,
                    reranked,
                ),
            )

            failure_stage = "neighbor.expand"
            current_span = _start_span(
                session,
                failure_stage,
                SpanKind.RETRIEVER,
            )
            neighbor_result = self._dependencies.neighbors.expand_with_trace(
                reranked.hits
            )
            expanded_hits = neighbor_result.hits
            _record_neighbors(session, neighbor_result.decisions)
            _finish_span(
                session,
                current_span,
                DecisionCode.ACCEPTED,
                {
                    "seed_count": len(reranked.hits),
                    "expanded_count": len(expanded_hits),
                    "decision_count": len(neighbor_result.decisions),
                },
            )
            current_span = None
            event_count += 1
            event_emitter.emit(
                StageName.RERANK,
                {
                    "candidate_count": (reranked.input_candidate_count),
                    "scored_count": len(reranked.scored_hits),
                    "final_count": len(reranked.hits),
                    "expanded_count": len(expanded_hits),
                    "external_calls": reranked.call_count,
                },
            )

            failure_stage = "evidence.assemble"
            current_span = _start_span(
                session,
                failure_stage,
                SpanKind.CHAIN,
            )
            evidence = self._dependencies.assembler.assemble(expanded_hits)
            _record_evidence(session, evidence.decisions)
            evidence_reason = _evidence_reason(evidence.decisions)
            _finish_span(
                session,
                current_span,
                evidence_reason,
                {
                    "evidence_count": len(evidence.items),
                    "evidence_tokens": evidence.token_count,
                    "quarantined": len(evidence.quarantined_chunk_ids),
                },
            )
            current_span = None
            _full_artifact(
                session,
                "evidence",
                {
                    "rendered": json.loads(evidence.rendered_json),
                    "decisions": [
                        asdict(decision) for decision in evidence.decisions
                    ],
                },
            )
            event_count += 1
            event_emitter.emit(
                StageName.ASSEMBLE,
                {
                    "evidence_count": len(evidence.items),
                    "evidence_tokens": evidence.token_count,
                    "quarantined": len(evidence.quarantined_chunk_ids),
                },
            )

            failure_stage = "llm.answer"
            current_span = _start_span(
                session,
                failure_stage,
                SpanKind.LLM,
            )
            rerank_scores = tuple(
                hit.rerank_score for hit in reranked.scored_hits
            )
            emit_answer = request.emit_answer
            if emit_answer is None:
                answer = self._dependencies.answerer.answer(
                    request.question,
                    evidence,
                    question_profile=active_profile,
                    rerank_scores=rerank_scores,
                )
            else:
                cancellation = request.cancellation
                if cancellation is None:
                    raise RuntimeError("流式查询缺少 cancellation。")
                emit_answer(
                    AnswerStartEvent(
                        trace_id=request.trace_id,
                        elapsed_ms=_elapsed_ms(started, self._clock),
                    )
                )
                streamed_claim_count = 0

                def emit_claim(claim: AnswerClaim) -> None:
                    """向当前请求回调一条已经完整验证的 claim。"""
                    nonlocal streamed_claim_count
                    if cancellation.is_cancelled():
                        raise StreamCancelledError("LLM_STREAM_CANCELLED")
                    emit_answer(
                        ValidatedClaimEvent(
                            trace_id=request.trace_id,
                            elapsed_ms=_elapsed_ms(
                                started,
                                self._clock,
                            ),
                            claim_index=streamed_claim_count,
                            claim=claim,
                        )
                    )
                    streamed_claim_count += 1

                answer = self._dependencies.answerer.answer_stream(
                    request.question,
                    evidence,
                    question_profile=active_profile,
                    rerank_scores=rerank_scores,
                    on_claim=emit_claim,
                    cancellation=cancellation,
                )
            answer_span_id = (
                None if current_span is None else current_span.span_id
            )
            _record_answer_children(session, answer_span_id, answer.trace)
            _finish_span(
                session,
                current_span,
                _answer_reason(answer),
                _answer_attributes(answer.trace),
            )
            current_span = None
            _record_citations(session, evidence, answer)
            _full_artifact(session, "answer", answer.trace)
            _full_artifact(
                session,
                "final",
                {
                    "status": answer.status.value,
                    "answer": answer.answer,
                    "answer_mode": answer.answer_mode.value,
                    "user_message": answer.user_message,
                    "refusal_code": (
                        None
                        if answer.refusal_code is None
                        else answer.refusal_code.value
                    ),
                    "claims": [asdict(claim) for claim in answer.claims],
                },
            )
            _raise_if_cancelled(request.cancellation)
            if self._answer_cache is not None and cache_key is not None:
                self._answer_cache.publish_singleflight(cache_key, answer)
                cache_span = _start_span(
                    session,
                    "cache.store",
                    SpanKind.STORAGE,
                )
                cache_status = self._answer_cache.store(
                    cache_key,
                    answer,
                    now=request.now,
                )
                _finish_span(
                    session,
                    cache_span,
                    DecisionCode.ACCEPTED,
                    {"cache_store_status": cache_status.value},
                )
            event_count += 1
            event_emitter.emit(
                StageName.VALIDATE,
                {
                    "status": answer.status.value,
                    "model_calls": answer.model_calls,
                    "refusal_code": (
                        ""
                        if answer.refusal_code is None
                        else answer.refusal_code.value
                    ),
                },
            )

            failure_stage = "answer.publish"
            current_span = _start_span(
                session,
                failure_stage,
                SpanKind.GUARDRAIL,
            )
            _raise_if_cancelled(request.cancellation)
            self._dependencies.conversations.append_turn(
                request.conversation_id,
                request.question,
                answer=answer,
                now=request.now,
                turn_id=request.trace_id,
            )
            _finish_span(
                session,
                current_span,
                _answer_reason(answer),
                {"status": answer.status.value},
            )
            current_span = None
            event_count += 1
            event_emitter.emit(
                StageName.COMPLETE,
                {"status": answer.status.value},
            )
            outcome = QueryOutcome(
                trace_id=request.trace_id,
                answer=answer,
                rewritten=variants.rewritten,
                stage_count=event_count,
                calls=tuple(
                    call
                    for call in (
                        variants.call,
                        *retrieval.calls,
                        reranked.call,
                        *answer.calls,
                    )
                    if call is not None
                ),
            )
            _finish_trace(session, answer)
            return outcome
        except Exception as error:
            if session is not None:
                if isinstance(error, InvalidEvidencePayloadError):
                    _record_evidence(session, (error.decision,))
                if current_span is not None:
                    reason_code = (
                        DecisionCode.REWRITE_TOKEN_LIMIT
                        if isinstance(error, RewriteTokenLimitError)
                        else DecisionCode.ERROR
                    )
                    if isinstance(error, RewriteTokenLimitError):
                        _full_artifact(
                            session,
                            "rewrite",
                            error.trace,
                        )
                    session.finish_span(
                        current_span,
                        TraceSpanFinish(
                            status=SpanStatus.ERROR,
                            reason_code=reason_code,
                            attributes={
                                "failure_stage": failure_stage,
                                "stream_cancelled": isinstance(
                                    error,
                                    StreamCancelledError,
                                ),
                            },
                        ),
                    )
                session.finish(
                    status=TraceStatus.FAILED,
                    reason_code=DecisionCode.ERROR,
                    error_code=_failure_code(failure_stage),
                    attributes={
                        "failure_stage": failure_stage,
                        "stream_cancelled": isinstance(
                            error,
                            StreamCancelledError,
                        ),
                    },
                )
            raise
        finally:
            singleflight_stack.close()

    def _answer_cache_key(
        self,
        resolved_query: str,
        *,
        context_digest: str,
    ) -> AnswerCacheKey:
        """用活动索引、服务指纹和回答协议构造精确缓存键。"""
        identity_source = self._trace_identity
        if identity_source is None:
            raise RuntimeError("回答缓存缺少 Trace identity。")
        identity = (
            identity_source() if callable(identity_source) else identity_source
        )
        return AnswerCacheKey.from_inputs(
            resolved_query=resolved_query,
            conversation_context_digest=context_digest,
            index_manifest_sha256=identity.index_manifest_sha256,
            serving_fingerprint=identity.serving_fingerprint,
            access_mode=self._access_mode,
            answer_revision=self._dependencies.answerer.revision(),
        )

    def _publish_cached(  # noqa: PLR0913
        self,
        *,
        request: _QueryRequest,
        answer: AnswerResult,
        rewritten: bool,
        rewrite_call: ExternalCallAudit | None,
        event_count: int,
        emitter: _StageEmitter,
        session: TraceSession | None,
    ) -> QueryOutcome:
        """跳过检索与模型调用并发布已绑定当前版本的精确命中。"""
        cached = replace(
            answer,
            trace={**answer.trace, "cache_status": "hit"},
        )
        if request.emit_answer is not None:
            request.emit_answer(
                AnswerStartEvent(
                    trace_id=request.trace_id,
                    elapsed_ms=_elapsed_ms(emitter.started, self._clock),
                )
            )
            for claim_index, claim in enumerate(cached.claims):
                _raise_if_cancelled(request.cancellation)
                request.emit_answer(
                    ValidatedClaimEvent(
                        trace_id=request.trace_id,
                        elapsed_ms=_elapsed_ms(
                            emitter.started,
                            self._clock,
                        ),
                        claim_index=claim_index,
                        claim=claim,
                    )
                )
        _full_artifact(
            session,
            "final",
            {
                "status": cached.status.value,
                "answer": cached.answer,
                "answer_mode": cached.answer_mode.value,
                "user_message": cached.user_message,
                "refusal_code": (
                    None
                    if cached.refusal_code is None
                    else cached.refusal_code.value
                ),
                "claims": [asdict(claim) for claim in cached.claims],
                "cache_status": "hit",
            },
        )
        emitter.emit(
            StageName.VALIDATE,
            {
                "status": cached.status.value,
                "model_calls": 0,
                "refusal_code": (
                    ""
                    if cached.refusal_code is None
                    else cached.refusal_code.value
                ),
                "cache_status": "hit",
            },
        )
        _raise_if_cancelled(request.cancellation)
        self._dependencies.conversations.append_turn(
            request.conversation_id,
            request.question,
            answer=cached,
            now=request.now,
            turn_id=request.trace_id,
        )
        emitter.emit(
            StageName.COMPLETE,
            {"status": cached.status.value, "cache_status": "hit"},
        )
        outcome = QueryOutcome(
            trace_id=request.trace_id,
            answer=cached,
            rewritten=rewritten,
            stage_count=event_count + 2,
            calls=() if rewrite_call is None else (rewrite_call,),
        )
        _finish_trace(session, cached)
        return outcome

    def _begin_trace(
        self,
        request: _QueryRequest,
        mode: TraceMode,
    ) -> TraceSession | None:
        """按追踪模式建立会话，并为非 FULL 请求提供审计降级。

        Args:
            request: 提供 trace 身份和查询时点的请求。
            mode: 决定追踪建立失败是否阻断查询的模式。

        Returns:
            已建立的追踪会话；追踪未配置或允许降级失败时返回 ``None``。

        Raises:
            Exception: FULL 模式下生成身份或建立追踪会话失败。

        """
        if self._trace_recorder is None or self._trace_identity is None:
            return None
        try:
            identity = (
                self._trace_identity()
                if callable(self._trace_identity)
                else self._trace_identity
            )
            return self._trace_recorder.begin_query(
                request.trace_id,
                mode,
                request.now,
                identity,
                question_sha256=hashlib.sha256(
                    request.question.encode("utf-8")
                ).hexdigest(),
            )
        except Exception:
            if mode is TraceMode.FULL:
                raise
            self._trace_recorder.capture_failed(request.trace_id)
            return None


def _start_span(
    session: TraceSession | None,
    name: str,
    kind: SpanKind,
) -> TraceSpanHandle | None:
    if session is None:
        return None
    return session.start_span(
        name,
        kind,
        parent_span_id=session.root.span_id,
    )


def _conversation_context_digest(
    rewritten: bool,
    previous_questions: tuple[str, ...],
    verified_claims: tuple[VerifiedClaimContext, ...],
) -> str:
    """独立问题使用空摘要，依赖上下文的问题绑定有限会话内容。"""
    if not rewritten:
        return ""
    canonical = json.dumps(
        {
            "questions": list(previous_questions),
            "verified_claims": [
                claim.as_payload() for claim in verified_claims
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float, clock: Callable[[], float]) -> int:
    """计算不小于零的请求累计毫秒数。"""
    return max(0, round((clock() - started) * 1000))


def _general_question_profile(
    requested_slots: tuple[RequestedSlot, ...],
    *,
    reason_code: str,
) -> QuestionProfile:
    """构造无法路由时不改变回答可用性的 GENERAL profile。"""
    return QuestionProfile(
        primary_operation=PrimaryOperation.GENERAL,
        secondary_operations=(),
        requested_slots=requested_slots,
        confidence=0.0,
        margin=0.0,
        route_source=RouteSource.GENERAL,
        scores=(),
        fallback_used=True,
        reason_code=reason_code,
    )


def _intent_route_attributes(  # noqa: PLR0913
    *,
    active_mode: IntentRouterMode,
    legacy_profile: QuestionProfile,
    semantic_profile: QuestionProfile,
    selected_profile: QuestionProfile,
    prototype_cache_ready: bool,
    llm_fallback_enabled: bool,
    llm_fallback_used: bool,
    elapsed_ms: int,
) -> dict[str, object]:
    """构造不含问题、样本或向量的 intent.route SAFE 属性。"""
    return {
        "active_mode": active_mode.value,
        "legacy_primary": legacy_profile.primary_operation.value,
        "semantic_primary": semantic_profile.primary_operation.value,
        "selected_primary": selected_profile.primary_operation.value,
        "secondary_operations": [
            operation.value
            for operation in selected_profile.secondary_operations
        ],
        "requested_slots": [
            slot.value for slot in selected_profile.requested_slots
        ],
        "semantic_confidence_milli": round(semantic_profile.confidence * 1000),
        "semantic_margin_milli": round(semantic_profile.margin * 1000),
        "route_source": selected_profile.route_source.value,
        "route_reason": selected_profile.reason_code,
        "disagreement": (
            legacy_profile.primary_operation
            is not semantic_profile.primary_operation
        ),
        "prototype_cache_ready": prototype_cache_ready,
        "llm_fallback_enabled": llm_fallback_enabled,
        "llm_fallback_used": llm_fallback_used,
        "route_elapsed_ms": elapsed_ms,
    }


def _raise_if_cancelled(
    cancellation: StreamCancellation | None,
) -> None:
    """在任何持久化写入前阻止已取消查询继续发布。"""
    if cancellation is not None and cancellation.is_cancelled():
        raise StreamCancelledError("LLM_STREAM_CANCELLED")


def _finish_span(
    session: TraceSession | None,
    span: TraceSpanHandle | None,
    reason: DecisionCode,
    attributes: dict[str, object],
) -> None:
    if session is None or span is None:
        return
    session.finish_span(
        span,
        TraceSpanFinish(
            status=SpanStatus.OK,
            reason_code=reason,
            attributes=attributes,
        ),
    )


def _completed_span(
    session: TraceSession | None,
    spec: TraceSpanSpec,
) -> None:
    if session is not None:
        session.completed_span(spec)


def _full_artifact(
    session: TraceSession | None,
    kind: str,
    payload: object,
) -> None:
    if session is not None:
        session.artifact(kind, _sanitize_artifact_payload(payload))


def _reason_from_trace(
    trace: Mapping[str, object],
    fallback: DecisionCode,
) -> DecisionCode:
    raw_reason = trace.get("reason_code")
    if not isinstance(raw_reason, str):
        return fallback
    try:
        return DecisionCode(raw_reason)
    except ValueError:
        return fallback


def _rewrite_attributes(
    trace: Mapping[str, object],
) -> dict[str, object]:
    allowed = (
        "question_sha256",
        "history_sha256",
        "resolved_query_sha256",
        "question_tokens",
        "history_tokens",
        "selected_history_tokens",
        "resolved_query_tokens",
        "rewrite_result_code",
        "trigger_reason_code",
        "max_output_tokens",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "retry_count",
    )
    return {key: trace[key] for key in allowed if key in trace}


def _record_retrieval(
    session: TraceSession | None,
    retrieve_span: TraceSpanHandle | None,
    trace: Mapping[str, object],
) -> None:
    """把检索内部决策投影为父子 spans 和逐候选审计记录。

    路由决策挂在根 span 下，embedding、各检索通道和 RRF 融合挂在
    检索 span 下；缺少追踪上下文时安全跳过。

    Args:
        session: 当前追踪会话，未启用追踪时为 ``None``。
        retrieve_span: 检索阶段父 span，未建立时为 ``None``。
        trace: 检索器产生的结构化追踪载荷。

    Returns:
        无返回值；有效会话会新增 spans 和候选决策记录。

    """
    if session is None or retrieve_span is None:
        return
    route = trace.get("route")
    if isinstance(route, dict):
        route_reason = _reason_from_trace(
            route,
            DecisionCode.FALLBACK_FULL_CORPUS,
        )
        session.completed_span(
            TraceSpanSpec(
                name="route.decide",
                kind=SpanKind.CHAIN,
                parent_span_id=session.root.span_id,
                reason_code=route_reason,
                attributes=_route_attributes(route),
            )
        )
        if not bool(route.get("routed", False)):
            session.completed_span(
                TraceSpanSpec(
                    name="route.fallback",
                    kind=SpanKind.CHAIN,
                    parent_span_id=session.root.span_id,
                    reason_code=DecisionCode.FALLBACK_FULL_CORPUS,
                    attributes={"full_corpus": True},
                )
            )
    embedding_duration = trace.get("embedding_duration_ms", 0)
    session.completed_span(
        TraceSpanSpec(
            name="embedding.query",
            kind=SpanKind.EMBEDDING,
            parent_span_id=retrieve_span.span_id,
            reason_code=DecisionCode.RETRIEVAL_OK,
            attributes={
                "query_count": trace.get("embedding_query_count", 0),
            },
            duration_ms=(
                embedding_duration if isinstance(embedding_duration, int) else 0
            ),
        )
    )
    raw_channels = trace.get("channels")
    if isinstance(raw_channels, list):
        for channel in raw_channels:
            if not isinstance(channel, dict):
                continue
            _record_channel(session, retrieve_span, channel)
    session.completed_span(
        TraceSpanSpec(
            name="rrf.fuse",
            kind=SpanKind.RETRIEVER,
            parent_span_id=retrieve_span.span_id,
            reason_code=DecisionCode.RETRIEVAL_OK,
            attributes={
                "rank_constant": trace.get("rrf_rank_constant", 0),
                "candidate_limit": trace.get("candidate_limit", 0),
                "fused_count": len(_dict_list(trace.get("fused"))),
            },
        )
    )
    for fused in _dict_list(trace.get("fused")):
        chunk_id = fused.get("chunk_id")
        if isinstance(chunk_id, str) and chunk_id:
            session.decision(
                stage="rrf.fuse",
                chunk_id=chunk_id,
                selected=True,
                reason_code=DecisionCode.SELECTED,
                details=fused,
            )


def _route_attributes(
    route: Mapping[object, object],
) -> dict[str, object]:
    return {
        "route_id": route.get("route_id"),
        "source_ids": route.get("source_ids", []),
        "confidence": route.get("confidence", 0.0),
        "routed": route.get("routed", False),
        "threshold": route.get("threshold", 0.0),
        "rule_scores": route.get("rule_scores", []),
    }


def _record_channel(
    session: TraceSession,
    retrieve_span: TraceSpanHandle,
    channel: dict[object, object],
) -> None:
    """记录单个检索通道及其返回候选。

    Args:
        session: 接收通道 span 和候选决策的追踪会话。
        retrieve_span: 通道所属的检索阶段父 span。
        channel: 检索器提供的单通道结构化追踪载荷。

    Returns:
        无返回值；通道名称无效时不写入任何记录。

    """
    name = channel.get("name")
    if not isinstance(name, str) or not name:
        return
    duration = channel.get("duration_ms", 0)
    session.completed_span(
        TraceSpanSpec(
            name=f"qdrant.{name.replace(':', '.')}",
            kind=SpanKind.RETRIEVER,
            parent_span_id=retrieve_span.span_id,
            reason_code=DecisionCode.RETRIEVAL_OK,
            attributes={
                "channel": name,
                "query_variant_index": channel.get(
                    "query_variant_index",
                    0,
                ),
                "channel_type": channel.get("channel_type", ""),
                "limit": channel.get("limit", 0),
                "returned_count": channel.get("returned_count", 0),
            },
            duration_ms=duration if isinstance(duration, int) else 0,
        )
    )
    for candidate in _dict_list(channel.get("candidates")):
        chunk_id = candidate.get("chunk_id")
        if isinstance(chunk_id, str) and chunk_id:
            session.decision(
                stage=f"retrieve.{name}",
                chunk_id=chunk_id,
                selected=True,
                reason_code=DecisionCode.RETRIEVAL_OK,
                details=candidate,
            )


def _record_rerank(
    session: TraceSession | None,
    candidates: tuple[FusedHit, ...],
    result: RerankStageResult,
) -> None:
    """记录重排前后名次以及最终截断决策。

    Args:
        session: 当前追踪会话，未启用追踪时为 ``None``。
        candidates: RRF 融合后按名次排列的输入候选。
        result: 包含评分全集和最终入选集合的重排结果。

    Returns:
        无返回值；有效会话会为每个已评分候选写入一条决策。

    """
    if session is None:
        return
    scored = result.scored_hits or result.hits
    selected_ids = {item.hit.chunk_id for item in result.hits}
    fused_ranks = {
        item.chunk_id: rank for rank, item in enumerate(candidates, start=1)
    }
    for item in scored:
        selected = item.hit.chunk_id in selected_ids
        session.decision(
            stage="rerank",
            chunk_id=item.hit.chunk_id,
            selected=selected,
            reason_code=(
                DecisionCode.SELECTED
                if selected
                else DecisionCode.DROPPED_FINAL_LIMIT
            ),
            details={
                "fused_rank": fused_ranks.get(item.hit.chunk_id),
                "rrf_score": item.hit.rrf_score,
                "rerank_score": item.rerank_score,
                "rerank_rank": item.rank,
                **_candidate_metadata(item.hit.payload),
            },
        )


def _record_neighbors(
    session: TraceSession | None,
    decisions: tuple[NeighborDecision, ...],
) -> None:
    if session is None:
        return
    for decision in decisions:
        session.decision(
            stage="neighbor.expand",
            chunk_id=decision.candidate_chunk_id,
            selected=decision.selected,
            reason_code=decision.reason_code,
            details={
                "seed_chunk_id": decision.seed_chunk_id,
                "direction": decision.direction,
            },
        )


def _record_evidence(
    session: TraceSession | None,
    decisions: tuple[EvidenceDecision, ...],
) -> None:
    if session is None:
        return
    for decision in decisions:
        session.decision(
            stage="evidence.assemble",
            chunk_id=decision.chunk_id,
            selected=decision.selected,
            reason_code=decision.reason_code,
            details={
                "evidence_id": decision.evidence_id,
                "estimated_total_tokens": (decision.estimated_total_tokens),
                "actual_candidate_tokens": (decision.actual_candidate_tokens),
                "contains_ocr": decision.contains_ocr,
                "minimum_ocr_confidence": (decision.minimum_ocr_confidence),
                "source_span_count": decision.source_span_count,
            },
        )


def _evidence_reason(
    decisions: tuple[EvidenceDecision, ...],
) -> DecisionCode:
    if any(decision.selected for decision in decisions):
        return DecisionCode.SELECTED
    if any(
        decision.reason_code is DecisionCode.PROMPT_INJECTION
        for decision in decisions
    ):
        return DecisionCode.PROMPT_INJECTION_ONLY
    if any(
        decision.reason_code is DecisionCode.TOKEN_BUDGET
        for decision in decisions
    ):
        return DecisionCode.EVIDENCE_BUDGET_DROP
    return DecisionCode.RETRIEVAL_EMPTY


def _record_answer_children(
    session: TraceSession | None,
    answer_span_id: str | None,
    trace: Mapping[str, object],
) -> None:
    """记录回答首次校验和可选修复调用的子 spans。

    Args:
        session: 当前追踪会话，未启用追踪时为 ``None``。
        answer_span_id: 回答生成阶段的父 span 标识。
        trace: 回答器产生的校验、修复和生成追踪载荷。

    Returns:
        无返回值；缺少会话或父 span 时安全跳过。

    """
    if session is None or answer_span_id is None:
        return
    first_code = _decision_code(
        trace.get("first_validation_code"),
        DecisionCode.VALIDATION_OK,
    )
    generations = _dict_list(trace.get("generations"))
    first_generation = _generation_for_phase(generations, "first")
    validation_attributes = _safe_answer_context(trace)
    if "first_validation_code" in trace:
        validation_attributes["validation_code"] = trace[
            "first_validation_code"
        ]
    if first_generation is not None:
        validation_attributes.update(
            _safe_generation_attributes(first_generation)
        )
    session.completed_span(
        TraceSpanSpec(
            name="answer.validate",
            kind=SpanKind.GUARDRAIL,
            parent_span_id=answer_span_id,
            reason_code=first_code,
            attributes=validation_attributes,
        )
    )
    if bool(trace.get("review_triggered", False)):
        session.completed_span(
            TraceSpanSpec(
                name="answer.abstention_review",
                kind=SpanKind.GUARDRAIL,
                parent_span_id=answer_span_id,
                reason_code=DecisionCode.ABSTENTION_REVIEW_TRIGGERED,
                attributes=_safe_answer_context(trace),
            )
        )
        review_generation = _generation_for_phase(
            generations,
            "abstention_review",
        )
        review_attributes = _safe_answer_context(trace)
        if "review_validation_code" in trace:
            review_attributes["validation_code"] = trace[
                "review_validation_code"
            ]
        if review_generation is not None:
            review_attributes.update(
                _safe_generation_attributes(review_generation)
            )
        session.completed_span(
            TraceSpanSpec(
                name="llm.abstention_review",
                kind=SpanKind.LLM,
                parent_span_id=answer_span_id,
                reason_code=_decision_code(
                    trace.get("review_reason_code"),
                    DecisionCode.ABSTENTION_REVIEW_INVALID,
                ),
                attributes=review_attributes,
                duration_ms=_generation_duration(
                    trace,
                    "abstention_review",
                ),
            )
        )
    if bool(trace.get("repair_triggered", False)):
        repair_code = _decision_code(
            trace.get("repair_validation_code"),
            DecisionCode.REPAIR_FAILED,
        )
        repair_attributes: dict[str, object] = {
            "validation_code": trace.get(
                "repair_validation_code",
                "",
            )
        }
        repair_generation = _generation_for_phase(generations, "repair")
        if repair_generation is not None:
            repair_attributes.update(
                _safe_generation_attributes(repair_generation)
            )
        session.completed_span(
            TraceSpanSpec(
                name="llm.repair",
                kind=SpanKind.LLM,
                parent_span_id=answer_span_id,
                reason_code=(
                    DecisionCode.REPAIR_OK
                    if repair_code is DecisionCode.VALIDATION_OK
                    else DecisionCode.REPAIR_FAILED
                ),
                attributes=repair_attributes,
                duration_ms=_repair_duration(trace),
            )
        )


def _record_citations(
    session: TraceSession | None,
    evidence: EvidenceBundle,
    answer: AnswerResult,
) -> None:
    if session is None:
        return
    cited_ids = {
        support.evidence_id
        for claim in answer.claims
        for support in claim.supports
    }
    for item in evidence.items:
        cited = item.evidence_id in cited_ids
        session.decision(
            stage="citation",
            chunk_id=item.chunk_id,
            selected=cited,
            reason_code=(
                DecisionCode.SELECTED if cited else DecisionCode.SKIPPED
            ),
            details={
                "evidence_id": item.evidence_id,
                "cited": cited,
                "source_span_count": len(item.source_spans),
            },
        )


def _answer_reason(answer: AnswerResult) -> DecisionCode:
    if answer.status is AnswerStatus.ANSWERED:
        return DecisionCode.ANSWERED
    if answer.refusal_code is not None:
        return _decision_code(
            answer.refusal_code.value,
            DecisionCode.REFUSED,
        )
    return DecisionCode.REFUSED


def _answer_attributes(
    trace: Mapping[str, object],
) -> dict[str, object]:
    attributes = _safe_answer_context(trace)
    if "first_validation_code" in trace:
        attributes["validation_code"] = trace["first_validation_code"]
    generations = _dict_list(trace.get("generations"))
    first_generation = _generation_for_phase(generations, "first")
    if first_generation is not None:
        attributes.update(_safe_generation_attributes(first_generation))
    return attributes


def _safe_answer_context(
    trace: Mapping[str, object],
) -> dict[str, object]:
    return {
        key: trace[key]
        for key in (
            "intent",
            "evidence_count",
            "non_low_ocr_evidence_count",
            "answerability_decision",
            "answerability_top_score",
            "strong_anchor_count",
            "covered_anchor_count",
            "answerability_non_low_ocr_count",
            "dropped_claim_count",
            "dropped_claim_codes",
            "selected_support_ranks",
            "min_selected_support_score",
            "low_rank_support_count",
            "first_validated_claim_ms",
            "validated_claim_count",
            "stream_dropped_claim_count",
            "stream_parser_error",
            "llm_stream",
            "first_delta_ms",
            "delta_count",
            "stream_cancelled",
            "stream_finish_reason",
            "retry_count",
            "extractive_fallback",
            "review_triggered",
        )
        if key in trace
    }


def _safe_generation_attributes(
    generation: Mapping[str, object],
) -> dict[str, object]:
    attributes: dict[str, object] = {}
    for key in (
        "phase",
        "endpoint",
        "selected_endpoint",
        "retry_count",
        "elapsed_ms",
        "queue_ms",
        "ttft_ms",
        "llm_stream",
        "first_delta_ms",
        "delta_count",
        "stream_cancelled",
        "stream_finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "completion_tokens_per_second",
        "claims_count",
        "validation_code",
    ):
        if key in generation:
            attributes[key] = (
                _sanitize_endpoint(str(generation[key]))
                if key in {"endpoint", "selected_endpoint"}
                else generation[key]
            )
    return attributes


def _generation_for_phase(
    generations: list[dict[str, object]],
    phase: str,
) -> dict[str, object] | None:
    return next(
        (
            generation
            for generation in generations
            if generation.get("phase") == phase
        ),
        None,
    )


def _rerank_artifact(
    query: str,
    candidates: tuple[FusedHit, ...],
    result: RerankStageResult,
) -> dict[str, object]:
    return {
        "query": query,
        "input_candidates": [
            {
                "chunk_id": candidate.chunk_id,
                "rrf_score": candidate.rrf_score,
                "channel_ranks": list(candidate.channel_ranks),
                "payload": _scrub_payload(candidate.payload),
            }
            for candidate in candidates
        ],
        "scored": [
            {
                "chunk_id": item.hit.chunk_id,
                "rerank_score": item.rerank_score,
                "rerank_rank": item.rank,
                "selected": item in result.hits,
            }
            for item in (result.scored_hits or result.hits)
        ],
    }


def _scrub_payload(payload: dict[str, object]) -> dict[str, object]:
    forbidden = {
        "vector",
        "vectors",
        "embedding_vector",
        "image_base64",
        "ocr_base64",
        "binary",
    }
    return {
        key: value
        for key, value in payload.items()
        if key.casefold() not in forbidden
        and not isinstance(value, (bytes, bytearray, memoryview))
    }


def _candidate_metadata(
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        key: payload.get(key)
        for key in (
            "source_id",
            "doc_version",
            "section_id",
            "section_path",
            "chunk_role",
            "neighbor_group_id",
        )
    }


def _external_call_attributes(
    call: ExternalCallAudit,
) -> dict[str, object]:
    return {
        "endpoint": _sanitize_endpoint(call.endpoint),
        "retry_count": call.retry_count,
    }


def _sanitize_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if not parsed.scheme or parsed.hostname is None:
        return "invalid-endpoint"
    port = "" if parsed.port is None else f":{parsed.port}"
    return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path}"


def _decision_code(
    value: object,
    fallback: DecisionCode,
) -> DecisionCode:
    if not isinstance(value, str):
        return fallback
    aliases = {
        "INVALID_CITATION_ID": "INVALID_EVIDENCE_ID",
        "QUOTE_NOT_IN_EVIDENCE": "QUOTE_NOT_FOUND",
        "QUOTE_CROSSES_SOURCE_SPAN": "CROSS_SPAN_QUOTE",
    }
    try:
        return DecisionCode(aliases.get(value, value))
    except ValueError:
        return fallback


def _repair_duration(trace: Mapping[str, object]) -> int:
    return _generation_duration(trace, "repair")


def _generation_duration(
    trace: Mapping[str, object],
    phase: str,
) -> int:
    generations = _dict_list(trace.get("generations"))
    generation = _generation_for_phase(generations, phase)
    if generation is None:
        return 0
    duration = generation.get("elapsed_ms")
    return duration if isinstance(duration, int) else 0


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _finish_trace(
    session: TraceSession | None,
    answer: AnswerResult,
) -> None:
    if session is None:
        return
    refusal_code = (
        None if answer.refusal_code is None else answer.refusal_code.value
    )
    session.finish(
        status=(
            TraceStatus.ANSWERED
            if answer.status is AnswerStatus.ANSWERED
            else TraceStatus.REFUSED
        ),
        reason_code=_answer_reason(answer),
        refusal_code=refusal_code,
        attributes={"status": answer.status.value},
    )


def _failure_code(stage: str) -> str:
    normalized = "".join(
        character if character.isalnum() else "_" for character in stage.upper()
    )
    return f"{normalized}_FAILED"


def _sanitize_artifact_payload(payload: object) -> object:
    """递归清理进入 FULL Trace artifact 的不安全内容。

    映射中的密钥、鉴权信息、向量和 Base64 字段会被移除，endpoint
    只保留安全组成，普通字符串中的常见凭据模式会被遮蔽。

    Args:
        payload: 待持久化的任意嵌套 artifact 载荷。

    Returns:
        保持原有容器语义且已完成敏感信息清理的 JSON 兼容值。

    Raises:
        ValueError: 任意嵌套层级包含二进制内容。

    """
    if isinstance(payload, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, value in payload.items():
            key = str(raw_key)
            normalized = key.casefold()
            if (
                normalized
                in {
                    "authorization",
                    "api_key",
                    "apikey",
                    "cookie",
                    "set-cookie",
                    "secret",
                    "password",
                }
                or normalized in {"vector", "vectors"}
                or normalized.endswith(("_base64", "_vector", "_vectors"))
            ):
                continue
            sanitized[key] = (
                _sanitize_endpoint(value)
                if normalized == "endpoint" and isinstance(value, str)
                else _sanitize_artifact_payload(value)
            )
        return sanitized
    if isinstance(payload, (list, tuple)):
        return [_sanitize_artifact_payload(item) for item in payload]
    if isinstance(payload, str):
        return _redact_secret_text(payload)
    if isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValueError("Trace artifact 禁止二进制内容。")
    return payload


def _redact_secret_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        value,
    )
    return re.sub(
        r"\bsk-[A-Za-z0-9_-]{16,}\b",
        "sk-[REDACTED]",
        redacted,
    )
