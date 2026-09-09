"""默认 Product 回答的有界 SSE 协调与取消边界。"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from rag_app.clients.resilience import StreamCancellation
from rag_app.core.errors import QueryCancelled, RagError
from rag_app.core.models import (
    AnswerStreamCancelledEvent,
    AnswerStreamClaimEvent,
    AnswerStreamErrorEvent,
    AnswerStreamFinalEvent,
    AnswerStreamMetaEvent,
    AnswerStreamPublicEvent,
    AnswerStreamStageEvent,
    SearchAnswerResult,
)
from rag_app.query_executor import QueryExecutor
from rag_app.sdk import RagSdk

_END: Final = object()
_HEARTBEAT_SECONDS = 2.0
_FIRST_CONTENT_SECONDS = 30.0
_IDLE_SECONDS = 30.0
_TOTAL_SECONDS = 120.0
_QUEUE_CAPACITY = 16


@dataclass(frozen=True, slots=True)
class P09AnswerStreamRequest:
    """已经完成 HTTP schema、认证、CSRF 与容量前置检查的请求。"""

    project_id: str
    knowledge_base_id: str
    question: str
    trace_id: str
    limit: int
    include_related_content: bool
    history_mode: str
    owner_id: str


@dataclass(slots=True)
class _QueuedEvent:
    """worker 只在 HTTP 消费者确认取走事件后继续执行。"""

    event: AnswerStreamPublicEvent
    delivered: threading.Event = field(default_factory=threading.Event)


@dataclass(slots=True)
class P09AnswerStream:
    """在固定查询线程与 HTTP 消费者间传递有界类型化事件。"""

    executor: QueryExecutor
    sdk: RagSdk
    request: P09AnswerStreamRequest
    render_final: Callable[[SearchAnswerResult], dict[str, object]]
    authorization_guard: Callable[[], None] | None = None
    versioned_protocol: bool = False
    heartbeat_seconds: float = _HEARTBEAT_SECONDS
    first_content_seconds: float = _FIRST_CONTENT_SECONDS
    idle_seconds: float = _IDLE_SECONDS
    total_seconds: float = _TOTAL_SECONDS
    messages: queue.Queue[_QueuedEvent | object] = field(
        default_factory=lambda: queue.Queue(maxsize=_QUEUE_CAPACITY)
    )
    cancellation: StreamCancellation = field(default_factory=StreamCancellation)
    started: float = field(default_factory=time.monotonic)
    last_sequence: int = -1
    delivered_claims: int = 0
    terminal_delivered: threading.Event = field(default_factory=threading.Event)
    deadline_timer: threading.Timer | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """拒绝关闭时限或创建无界协调器。"""
        timeouts = (
            self.heartbeat_seconds,
            self.first_content_seconds,
            self.idle_seconds,
            self.total_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in timeouts):
            raise ValueError("流式时限必须为正有限秒数。")
        if (
            self.messages.maxsize <= 0
            or self.messages.maxsize > _QUEUE_CAPACITY
        ):
            raise ValueError("流式事件队列必须位于固定容量上限内。")

    def start(self) -> Iterator[bytes]:
        """在返回 StreamingResponse 前完成固定容量准入。"""
        remaining = max(
            0.001,
            self.total_seconds - (time.monotonic() - self.started),
        )
        self.deadline_timer = threading.Timer(
            remaining,
            self.cancellation.cancel,
        )
        self.deadline_timer.daemon = True
        self.deadline_timer.start()
        try:
            self.executor.submit(self._run)
        except BaseException:
            self.deadline_timer.cancel()
            raise
        return self._iterate()

    def cancel(self) -> None:
        """由 HTTP 响应生命周期显式传播完成、断连或发送失败。"""
        self.cancellation.cancel()

    def _put(self, event: AnswerStreamPublicEvent) -> None:
        """让慢消费者对 Provider 读取形成真实、有界背压。"""
        queued = _QueuedEvent(event)
        while not self.cancellation.is_cancelled():
            try:
                self.messages.put(queued, timeout=0.1)
            except queue.Full:
                continue
            while not queued.delivered.wait(timeout=0.1):
                if self.cancellation.is_cancelled():
                    raise QueryCancelled("QUERY_CANCELLED")
            return
        raise QueryCancelled("QUERY_CANCELLED")

    def _finish_queue(self) -> None:
        """自然结束时唤醒仍在等待下一事件的 HTTP 迭代器。"""
        while not self.cancellation.is_cancelled():
            try:
                self.messages.put(_END, timeout=0.1)
            except queue.Full:
                continue
            return

    def _run(self) -> None:
        """执行 SDK 共享查询链，并在异常后只发布一个安全终态。"""
        try:
            self.sdk.answer_stream(
                self.request.project_id,
                self.request.knowledge_base_id,
                self.request.question,
                emit=self._emit_authorized,
                cancellation=self.cancellation,
                limit=self.request.limit,
                include_related_content=self.request.include_related_content,
                history_mode=cast(
                    Literal["full", "metadata_only"],
                    self.request.history_mode,
                ),
                owner_id=self.request.owner_id,
                trace_id=self.request.trace_id,
            )
        except QueryCancelled:
            return
        except RagError as error:
            self._put_error(
                AnswerStreamErrorEvent(
                    trace_id=self.request.trace_id,
                    sequence=self.last_sequence + 1,
                    project_id=self.request.project_id,
                    knowledge_base_id=self.request.knowledge_base_id,
                    code=error.code,
                    message=error.safe_message,
                    stage=error.stage,
                    retryable=error.retryable,
                    partial=self.delivered_claims > 0,
                )
            )
        except Exception:
            self._put_error(
                AnswerStreamErrorEvent(
                    trace_id=self.request.trace_id,
                    sequence=self.last_sequence + 1,
                    project_id=self.request.project_id,
                    knowledge_base_id=self.request.knowledge_base_id,
                    code="INTERNAL_ERROR",
                    message="流式查询执行失败。",
                    stage="answer.stream",
                    partial=self.delivered_claims > 0,
                )
            )
        finally:
            self._finish_queue()
            if self.deadline_timer is not None:
                self.deadline_timer.cancel()

    def _emit_authorized(self, event: AnswerStreamPublicEvent) -> None:
        """在每个 SDK 发布边界重查在途请求的授权。"""
        if self.authorization_guard is not None:
            self.authorization_guard()
        self._put(event)

    def _put_error(self, event: AnswerStreamErrorEvent) -> None:
        """禁止在 Final 已交付后追加第二个冲突终态。"""
        if self.terminal_delivered.is_set():
            return
        try:
            self._put(event)
        except QueryCancelled:
            return

    def _iterate(self) -> Iterator[bytes]:
        """序列化 SSE；心跳不占协议 sequence，也不携带业务内容。"""
        last_activity = self.started
        content_delivered = False
        try:
            while True:
                now = time.monotonic()
                total_remaining = self.total_seconds - (now - self.started)
                first_remaining = self.first_content_seconds - (
                    now - self.started
                )
                idle_remaining = self.idle_seconds - (now - last_activity)
                deadline_remaining = min(
                    total_remaining,
                    idle_remaining,
                    first_remaining if not content_delivered else math.inf,
                )
                if deadline_remaining <= 0:
                    self.cancellation.cancel()
                    self.terminal_delivered.set()
                    yield self._timeout_frame(
                        now=now,
                        content_delivered=content_delivered,
                    )
                    return
                try:
                    message = self.messages.get(
                        timeout=min(self.heartbeat_seconds, deadline_remaining)
                    )
                except queue.Empty:
                    elapsed_ms = max(
                        0, round((time.monotonic() - self.started) * 1000)
                    )
                    yield f": heartbeat {elapsed_ms}\n\n".encode()
                    continue
                if message is _END:
                    return
                queued = cast(_QueuedEvent, message)
                event = queued.event
                frame = self._render_event(event)
                if frame is None:
                    self._acknowledge(queued)
                    last_activity = time.monotonic()
                    continue
                yield frame
                self._acknowledge(queued)
                last_activity = time.monotonic()
                if isinstance(
                    event,
                    (AnswerStreamClaimEvent, AnswerStreamFinalEvent),
                ):
                    content_delivered = True
        finally:
            self.cancellation.cancel()

    def _acknowledge(self, queued: _QueuedEvent) -> None:
        """原子更新已交付统计，再允许 worker 进入下一阶段。"""
        event = queued.event
        self.last_sequence = max(self.last_sequence, event.sequence)
        if isinstance(event, AnswerStreamClaimEvent):
            self.delivered_claims += 1
        if isinstance(
            event,
            (
                AnswerStreamFinalEvent,
                AnswerStreamErrorEvent,
                AnswerStreamCancelledEvent,
            ),
        ):
            self.terminal_delivered.set()
        queued.delivered.set()

    def _render_event(self, event: AnswerStreamPublicEvent) -> bytes | None:
        """新协议严格输出；旧协议只保留既有 meta/retrieval/final。"""
        if isinstance(event, AnswerStreamFinalEvent):
            payload = self.render_final(event.result)
            if self.versioned_protocol:
                payload.update(
                    {
                        "type": "final",
                        "protocol": event.protocol,
                        "sequence": event.sequence,
                    }
                )
            return _sse("final", payload)
        if self.versioned_protocol:
            return _sse(event.type, event.model_dump(mode="json"))
        if isinstance(event, AnswerStreamMetaEvent):
            return _sse("meta", {"trace_id": event.trace_id})
        if (
            isinstance(event, AnswerStreamStageEvent)
            and event.stage == "retrieval"
        ):
            return _sse(
                "retrieval",
                {
                    "trace_id": event.trace_id,
                    "evidence_count": dict(event.attributes).get(
                        "evidence_count", 0
                    ),
                    "diagnostics_summary": None,
                },
            )
        if isinstance(event, AnswerStreamErrorEvent):
            return _sse("error", event.model_dump(mode="json"))
        return None

    def _timeout_frame(
        self,
        *,
        now: float,
        content_delivered: bool,
    ) -> bytes:
        """按总时限、首内容与空闲优先级构造唯一安全终态。"""
        if now - self.started >= self.total_seconds:
            code = "STREAM_TOTAL_TIMEOUT"
            message = "流式回答超过总时限。"
        elif (
            not content_delivered
            and now - self.started >= self.first_content_seconds
        ):
            code = "STREAM_FIRST_CONTENT_TIMEOUT"
            message = "流式回答等待首个内容超过时限。"
        else:
            code = "STREAM_IDLE_TIMEOUT"
            message = "流式回答等待后续内容超过空闲时限。"
        event = AnswerStreamErrorEvent(
            trace_id=self.request.trace_id,
            sequence=self.last_sequence + 1,
            project_id=self.request.project_id,
            knowledge_base_id=self.request.knowledge_base_id,
            code=code,
            message=message,
            stage="answer.stream",
            retryable=True,
            partial=self.delivered_claims > 0,
        )
        return _sse("error", event.model_dump(mode="json"))


def _sse(event: str, payload: object) -> bytes:
    """编码单个严格 JSON data 的 SSE 帧。"""
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"event: {event}\ndata: {body}\n\n".encode()


__all__ = ["P09AnswerStream", "P09AnswerStreamRequest"]
