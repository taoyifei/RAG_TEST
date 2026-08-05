"""逐条发布已校验 claim，并以 canonical final 收束的 NDJSON 流。"""

from __future__ import annotations

import json
import queue
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Final, TypeAlias

from rag_app.clients.resilience import StreamCancellation
from rag_app.observability import StructuredAuditLogger
from rag_app.query_executor import QueryExecutor
from rag_app.query_service import (
    AnswerStartEvent,
    QueryOutcome,
    QueryService,
    StageEvent,
    ValidatedClaimEvent,
)
from rag_app.tracing.models import TraceMode

__all__ = ["QueryStreamRequest", "stream_query"]

_END: Final = object()
_ERROR_EVENT: Final = {
    "type": "error",
    "code": "QUERY_FAILED",
}

_StreamMessage: TypeAlias = (
    StageEvent
    | AnswerStartEvent
    | ValidatedClaimEvent
    | QueryOutcome
    | dict[str, str]
    | object
)


@dataclass(frozen=True, slots=True)
class QueryStreamRequest:
    """一次已通过 HTTP schema 与鉴权的查询上下文。"""

    trace_id: str
    conversation_id: str
    question: str
    audit: StructuredAuditLogger | None = None
    trace_mode: TraceMode = TraceMode.SAFE


@dataclass(slots=True)
class _DeliveryState:
    """记录 NDJSON 消费端公开进度。"""

    answer_started: bool = False
    validated_claims: int = 0


@dataclass(slots=True)
class _QueryStream:
    """在查询工作线程与 HTTP 迭代器之间传递有界安全事件。"""

    executor: QueryExecutor
    service: QueryService
    request: QueryStreamRequest
    messages: queue.Queue[_StreamMessage] = field(
        default_factory=lambda: queue.Queue(maxsize=16)
    )
    cancellation: StreamCancellation = field(
        default_factory=StreamCancellation
    )
    started: float = field(default_factory=time.monotonic)

    def start(self) -> Iterator[bytes]:
        """提交查询工作并返回当前实例的 NDJSON 迭代器。"""
        self.executor.submit(self._run)
        return self._iterate()

    def _put(self, message: _StreamMessage) -> None:
        """在客户端仍消费时写入有界流队列。"""
        while not self.cancellation.is_cancelled():
            try:
                self.messages.put(message, timeout=0.1)
            except queue.Full:
                continue
            return

    def _emit_stage(self, event: StageEvent) -> None:
        """审计并转发一条非敏感阶段事件。"""
        if self.request.audit is not None:
            self.request.audit.query_stage(event)
        self._put(event)

    def _emit_answer(
        self,
        event: AnswerStartEvent | ValidatedClaimEvent,
    ) -> None:
        """转发回答开始或已通过门禁的完整 claim。"""
        self._put(event)

    def _run(self) -> None:
        """执行同步查询并把阶段或终态写入有界队列。"""
        try:
            stream_method = (
                self.service.ask_debug_stream
                if self.request.trace_mode is TraceMode.FULL
                else self.service.ask_stream
            )
            outcome = stream_method(
                trace_id=self.request.trace_id,
                conversation_id=self.request.conversation_id,
                question=self.request.question,
                now=datetime.now(UTC),
                emit=self._emit_stage,
                emit_answer=self._emit_answer,
                cancellation=self.cancellation,
            )
            if self.request.audit is not None:
                self.request.audit.query_outcome(outcome)
            self._put(outcome)
        except Exception:
            if self.request.audit is not None:
                self.request.audit.query_failed(
                    self.request.trace_id,
                    "QUERY_EXECUTION_FAILED",
                )
            self._put(_ERROR_EVENT)
        finally:
            self._put(_END)

    def _iterate(self) -> Iterator[bytes]:
        """消费有界队列并定期产生不含正文的回答进度。"""
        state = _DeliveryState()
        try:
            while True:
                try:
                    message = self.messages.get(
                        timeout=2.0 if state.answer_started else None
                    )
                except queue.Empty:
                    yield _json_line(self._progress_payload(state))
                    continue
                if message is _END:
                    return
                yield _json_line(self._message_payload(message, state))
        finally:
            self.cancellation.cancel()

    def _progress_payload(
        self,
        state: _DeliveryState,
    ) -> dict[str, object]:
        """构造无正文的两秒回答进度事件。"""
        return {
            "type": "answer_progress",
            "trace_id": self.request.trace_id,
            "elapsed_ms": max(
                0,
                round((time.monotonic() - self.started) * 1000),
            ),
            "validated_claims": state.validated_claims,
        }

    @staticmethod
    def _message_payload(
        message: _StreamMessage,
        state: _DeliveryState,
    ) -> object:
        """转换一条队列消息并维护公开进度状态。"""
        if isinstance(message, StageEvent):
            return _stage_payload(message)
        if isinstance(message, AnswerStartEvent):
            state.answer_started = True
            return _answer_start_payload(message)
        if isinstance(message, ValidatedClaimEvent):
            state.validated_claims = max(
                state.validated_claims,
                message.claim_index + 1,
            )
            return _claim_payload(message)
        if isinstance(message, QueryOutcome):
            return _final_payload(message)
        return message


def stream_query(
    *,
    executor: QueryExecutor,
    service: QueryService,
    request: QueryStreamRequest,
) -> Iterator[bytes]:
    """准入同步查询并返回逐行发布非敏感事件的迭代器。

    Args:
        executor: 进程级固定容量查询执行器。
        service: 完整查询链。
        request: 已校验的 trace、会话、问题和可选审计依赖。

    Returns:
        UTF-8 NDJSON 迭代器；最终回答只来自已校验的 QueryOutcome。

    """
    return _QueryStream(
        executor=executor,
        service=service,
        request=request,
    ).start()


def _stage_payload(event: StageEvent) -> dict[str, object]:
    """转换不含任何业务原文的阶段事件。"""
    return {
        "type": "stage",
        "trace_id": event.trace_id,
        "stage": event.stage.value,
        "elapsed_ms": event.elapsed_ms,
        "metrics": event.metrics,
    }


def _answer_start_payload(event: AnswerStartEvent) -> dict[str, object]:
    """转换不含模型正文的回答开始事件。"""
    return {
        "type": "answer_start",
        "trace_id": event.trace_id,
        "elapsed_ms": event.elapsed_ms,
    }


def _claim_payload(event: ValidatedClaimEvent) -> dict[str, object]:
    """转换已经通过全部引用门禁的一条完整 claim。"""
    return {
        "type": "claim",
        "trace_id": event.trace_id,
        "claim_index": event.claim_index,
        "text": event.claim.text,
        "supports": [asdict(support) for support in event.claim.supports],
    }


def _final_payload(outcome: QueryOutcome) -> dict[str, object]:
    """转换已通过发布门禁的最终回答或拒答。"""
    answer = outcome.answer
    return {
        "type": "final",
        "trace_id": outcome.trace_id,
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
        "rewritten": outcome.rewritten,
    }


def _json_line(payload: object) -> bytes:
    """编码一条无 ASCII 转义的 NDJSON。"""
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
