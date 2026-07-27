"""只在回答校验完成后发布最终内容的 NDJSON 流。"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Final

from rag_app.observability import StructuredAuditLogger
from rag_app.query_service import (
    QueryOutcome,
    QueryService,
    StageEvent,
)

__all__ = ["stream_query"]

_END: Final = object()
_ERROR_EVENT: Final = {
    "type": "error",
    "code": "QUERY_FAILED",
}


def stream_query(
    *,
    service: QueryService,
    trace_id: str,
    conversation_id: str,
    question: str,
    audit: StructuredAuditLogger | None = None,
) -> Iterator[bytes]:
    """在后台执行同步查询并逐行发布非敏感事件。

    Args:
        service: 完整查询链。
        trace_id: 本次请求追踪标识。
        conversation_id: 有界 TTL 会话标识。
        question: 当前原始问题。
        audit: 可选固定字段结构化日志。

    Yields:
        UTF-8 NDJSON 行；最终回答只来自已校验的 QueryOutcome。

    """
    messages: queue.Queue[StageEvent | QueryOutcome | dict[str, str] | object]
    messages = queue.Queue(maxsize=16)

    def run() -> None:
        try:
            def emit(event: StageEvent) -> None:
                if audit is not None:
                    audit.query_stage(event)
                messages.put(event)

            outcome = service.ask(
                trace_id=trace_id,
                conversation_id=conversation_id,
                question=question,
                now=datetime.now(UTC),
                emit=emit,
            )
            if audit is not None:
                audit.query_outcome(outcome)
            messages.put(outcome)
        except Exception as error:
            if audit is not None:
                audit.query_failed(trace_id, type(error).__name__)
            messages.put(_ERROR_EVENT)
        finally:
            messages.put(_END)

    threading.Thread(
        target=run,
        name=f"rag-query-{trace_id[:8]}",
        daemon=True,
    ).start()

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
