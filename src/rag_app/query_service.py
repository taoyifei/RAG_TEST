"""从条件改写到发布门禁的同步查询编排。"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from rag_app.clients.model_services import ExternalCallAudit
from rag_app.generation.answer import (
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
    "QueryDependencies",
    "QueryOutcome",
    "QueryService",
    "StageEvent",
    "StageName",
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


class QueryService:
    """顺序执行检索生成链，并通过 callback 发非敏感阶段事件。"""

    def __init__(
        self,
        *,
        dependencies: QueryDependencies,
        clock: Callable[[], float] = time.monotonic,
        trace_recorder: TraceRecorder | None = None,
        trace_identity: (
            TraceIdentity | Callable[[], TraceIdentity] | None
        ) = None,
        default_trace_mode: TraceMode = TraceMode.SAFE,
    ) -> None:
        """保存完整查询链依赖。

        Args:
            dependencies: 查询链必需的有状态组件。
            clock: 非敏感阶段耗时的单调时钟。
            trace_recorder: 可选的独立 Trace writer。
            trace_identity: 与 recorder 同时提供的运行身份或实时提供器。
            default_trace_mode: 普通 chat 的 SAFE/DIAGNOSTIC 边界。

        """
        if (trace_recorder is None) != (trace_identity is None):
            raise ValueError("Trace recorder 和 identity 必须同时配置。")
        self._dependencies = dependencies
        self._clock = clock
        self._trace_recorder = trace_recorder
        self._trace_identity = trace_identity
        if default_trace_mode is TraceMode.FULL:
            raise ValueError("普通 chat 不能配置为 FULL Trace。")
        self._default_trace_mode = default_trace_mode

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

    def _ask(  # noqa: PLR0915
        self,
        request: _QueryRequest,
        mode: TraceMode,
    ) -> QueryOutcome:
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
        failure_stage = "context.load"
        try:
            current_span = _start_span(
                session,
                "context.load",
                SpanKind.STORAGE,
            )
            previous_questions = self._dependencies.conversations.get_questions(
                request.conversation_id,
                now=request.now,
            )
            _finish_span(
                session,
                current_span,
                DecisionCode.ACCEPTED,
                {"history_count": len(previous_questions)},
            )
            current_span = None
            _full_artifact(
                session,
                "context",
                {
                    "question": request.question,
                    "history": list(previous_questions),
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
            answer = self._dependencies.answerer.answer(
                request.question,
                evidence,
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
                    "refusal_code": (
                        None
                        if answer.refusal_code is None
                        else answer.refusal_code.value
                    ),
                    "claims": [asdict(claim) for claim in answer.claims],
                },
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
            self._dependencies.conversations.append_question(
                request.conversation_id,
                request.question,
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
                            },
                        ),
                    )
                session.finish(
                    status=TraceStatus.FAILED,
                    reason_code=DecisionCode.ERROR,
                    error_code=_failure_code(failure_stage),
                    attributes={"failure_stage": failure_stage},
                )
            raise

    def _begin_trace(
        self,
        request: _QueryRequest,
        mode: TraceMode,
    ) -> TraceSession | None:
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
    if session is None or answer_span_id is None:
        return
    first_code = _decision_code(
        trace.get("first_validation_code"),
        DecisionCode.VALIDATION_OK,
    )
    session.completed_span(
        TraceSpanSpec(
            name="answer.validate",
            kind=SpanKind.GUARDRAIL,
            parent_span_id=answer_span_id,
            reason_code=first_code,
            attributes={
                "validation_code": trace.get(
                    "first_validation_code",
                    "VALIDATION_OK",
                ),
                "repair_triggered": trace.get(
                    "repair_triggered",
                    False,
                ),
            },
        )
    )
    if bool(trace.get("repair_triggered", False)):
        repair_code = _decision_code(
            trace.get("repair_validation_code"),
            DecisionCode.REPAIR_FAILED,
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
                attributes={
                    "validation_code": trace.get(
                        "repair_validation_code",
                        "",
                    )
                },
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
    attributes: dict[str, object] = {
        "first_validation_code": trace.get(
            "first_validation_code",
            "",
        ),
        "repair_triggered": trace.get("repair_triggered", False),
        "repair_validation_code": trace.get(
            "repair_validation_code",
            "",
        ),
    }
    generations = _dict_list(trace.get("generations"))
    if generations:
        latest = generations[-1]
        for key in (
            "model",
            "endpoint",
            "retry_count",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "max_output_tokens",
        ):
            if key in latest:
                attributes[key] = (
                    _sanitize_endpoint(str(latest[key]))
                    if key == "endpoint"
                    else latest[key]
                )
    return attributes


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
    generations = _dict_list(trace.get("generations"))
    if not generations:
        return 0
    repair = generations[-1]
    duration = repair.get("elapsed_ms")
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
                or normalized.endswith(
                    ("_base64", "_vector", "_vectors")
                )
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
