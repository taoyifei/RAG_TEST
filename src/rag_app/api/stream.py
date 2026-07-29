"""只在回答校验完成后发布最终内容的 NDJSON 流。"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Final

from rag_app.observability import StructuredAuditLogger
from rag_app.query_executor import QueryExecutor
from rag_app.query_service import (
    QueryOutcome,
    QueryService,
    StageEvent,
)
from rag_app.tracing.models import TraceMode

__all__ = ["QueryStreamRequest", "stream_query"]

_END: Final = object()
_ERROR_EVENT: Final = {
    "type": "error",
    "code": "QUERY_FAILED",
}


@dataclass(frozen=True, slots=True)
class QueryStreamRequest:
    """一次已通过 HTTP schema 与鉴权的查询上下文。"""

    trace_id: str
    conversation_id: str
    question: str
    audit: StructuredAuditLogger | None = None
    trace_mode: TraceMode = TraceMode.SAFE


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
    messages: queue.Queue[StageEvent | QueryOutcome | dict[str, str] | object]
    messages = queue.Queue(maxsize=16)
    cancelled = threading.Event()

    def put_message(
        message: StageEvent | QueryOutcome | dict[str, str] | object,
    ) -> None:
        """在客户端仍消费时写入有界流队列。

        Args:
            message: 待发布的阶段、终态或内部结束信号。

        Returns:
            无返回值；流关闭后直接丢弃后续消息。

        """
        while not cancelled.is_set():
            try:
                messages.put(message, timeout=0.1)
            except queue.Full:
                continue
            return

    def run() -> None:
        """执行同步查询并把阶段或终态写入有界队列。

        Args:
            无参数；使用外层请求上下文。

        Returns:
            无返回值。

        """
        try:
            def emit(event: StageEvent) -> None:
                """记录并转发一条非敏感阶段事件。

                Args:
                    event: 当前查询阶段事件。

                Returns:
                    无返回值。

                """
                if request.audit is not None:
                    request.audit.query_stage(event)
                put_message(event)

            query_method = (
                service.ask_debug
                if request.trace_mode is TraceMode.FULL
                else service.ask
            )
            outcome = query_method(
                trace_id=request.trace_id,
                conversation_id=request.conversation_id,
                question=request.question,
                now=datetime.now(UTC),
                emit=emit,
            )
            if request.audit is not None:
                request.audit.query_outcome(outcome)
            put_message(outcome)
        except Exception:
            if request.audit is not None:
                request.audit.query_failed(
                    request.trace_id,
                    "QUERY_EXECUTION_FAILED",
                )
            put_message(_ERROR_EVENT)
        finally:
            put_message(_END)

    executor.submit(run)

    def iterate() -> Iterator[bytes]:
        """消费当前查询的有界消息队列。

        Args:
            无参数；消费外层查询队列。

        Yields:
            已编码的 UTF-8 NDJSON 行。

        Returns:
            查询结束或客户端关闭流后无额外返回值。

        """
        try:
            while True:
                message = messages.get()
                if message is _END:
                    return
                if isinstance(message, StageEvent):
                    yield _json_line(_stage_payload(message))
                elif isinstance(message, QueryOutcome):
                    yield _json_line(_final_payload(message))
                else:
                    yield _json_line(message)
        finally:
            cancelled.set()

    return iterate()


def _stage_payload(event: StageEvent) -> dict[str, object]:
    """转换不含任何业务原文的阶段事件。"""
    return {
        "type": "stage",
        "trace_id": event.trace_id,
        "stage": event.stage.value,
        "elapsed_ms": event.elapsed_ms,
        "metrics": event.metrics,
    }


def _final_payload(outcome: QueryOutcome) -> dict[str, object]:
    """转换已通过发布门禁的最终回答或拒答。"""
    answer = outcome.answer
    return {
        "type": "final",
        "trace_id": outcome.trace_id,
        "status": answer.status.value,
        "answer": answer.answer,
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
