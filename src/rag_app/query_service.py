"""从条件改写到发布门禁的同步查询编排。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rag_app.clients.model_services import ExternalCallAudit
from rag_app.generation.answer import AnswerGenerator, AnswerResult
from rag_app.generation.evidence import EvidenceAssembler
from rag_app.retrieval.hybrid import HybridRetriever
from rag_app.retrieval.neighbors import NeighborExpander
from rag_app.retrieval.rerank import RerankStage
from rag_app.retrieval.rewrite import QueryRewriter
from rag_app.state.conversations import ConversationStore

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
        """发送不含问题、证据或答案的阶段事件。"""
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


class QueryService:
    """顺序执行检索生成链，并通过 callback 发非敏感阶段事件。"""

    def __init__(
        self,
        *,
        dependencies: QueryDependencies,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """保存完整查询链依赖。

        Args:
            dependencies: 查询链必需的有状态组件。
            clock: 非敏感阶段耗时的单调时钟。

        """
        self._dependencies = dependencies
        self._clock = clock

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
        if not trace_id:
            raise ValueError("trace_id 不能为空。")
        started = self._clock()
        event_emitter = _StageEmitter(
            callback=emit,
            trace_id=trace_id,
            started=started,
            clock=self._clock,
        )
        event_count = 0

        previous_questions = self._dependencies.conversations.get_questions(
            conversation_id,
            now=now,
        )
        variants = self._dependencies.rewriter.rewrite(
            question,
            previous_questions=previous_questions,
        )
        event_count += 1
        event_emitter.emit(
            StageName.REWRITE,
            {
                "rewritten": variants.rewritten,
                "query_count": len(variants.queries),
            },
        )

        retrieval = self._dependencies.retriever.retrieve(variants, as_of=now)
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

        reranked = self._dependencies.reranker.rerank(
            question,
            retrieval.candidates,
        )
        expanded_hits = self._dependencies.neighbors.expand(reranked.hits)
        event_count += 1
        event_emitter.emit(
            StageName.RERANK,
            {
                "candidate_count": len(reranked.hits),
                "expanded_count": len(expanded_hits),
                "external_calls": reranked.call_count,
            },
        )

        evidence = self._dependencies.assembler.assemble(expanded_hits)
        event_count += 1
        event_emitter.emit(
            StageName.ASSEMBLE,
            {
                "evidence_count": len(evidence.items),
                "evidence_tokens": evidence.token_count,
                "quarantined": len(evidence.quarantined_chunk_ids),
            },
        )

        answer = self._dependencies.answerer.answer(question, evidence)
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
        self._dependencies.conversations.append_question(
            conversation_id,
            question,
            now=now,
            turn_id=trace_id,
        )
        event_count += 1
        event_emitter.emit(
            StageName.COMPLETE,
            {"status": answer.status.value},
        )
        return QueryOutcome(
            trace_id=trace_id,
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
