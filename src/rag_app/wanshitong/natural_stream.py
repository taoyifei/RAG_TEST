"""湾事通自然问答的公共增量流、会话与单次终态。"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from rag_app.application.answering.natural_answer import (
    NaturalAnswerResult,
    NaturalReference,
)
from rag_app.clients.resilience import StreamCancellation
from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import QueryCancelled, RagError
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from rag_app.core.models.usage_audit import QueryAuditContext
from rag_app.product.conversations import natural_reference_id
from rag_app.query_executor import QueryExecutor

NATURAL_PUBLIC_PROTOCOL: Final[Literal["wanshitong-natural-sse-v1"]] = (
    "wanshitong-natural-sse-v1"
)
NATURAL_PUBLIC_PROTOCOL_V2: Final[Literal["wanshitong-natural-sse-v2"]] = (
    "wanshitong-natural-sse-v2"
)
_QUEUE_CAPACITY = 16
_EVENT_RETENTION = 256
_RUN_RETENTION_SECONDS = 600.0
_REGISTRY_CAPACITY = 128
_HEARTBEAT_SECONDS = 2.0
_TOTAL_SECONDS = 240.0
_Terminal = Literal["OPEN", "FINAL", "ERROR", "CANCELLED"]
_LOGGER = logging.getLogger(__name__)


class NaturalStreamExpiredError(Exception):
    """请求的事件序号已超出有界重放窗口。"""


def _frame(name: str, payload: dict[str, object]) -> bytes:
    """只编码本方公共白名单 DTO。"""
    return (
        f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode()


def public_natural_references(
    trace_id: str, references: tuple[NaturalReference, ...]
) -> list[dict[str, object]]:
    """仅把实际引用的本次材料投影为可见来源。"""
    items: list[dict[str, object]] = []
    for reference in references:
        paths = tuple(
            dict.fromkeys(
                " / ".join(span.structural_path)
                for span in reference.source_spans
                if span.is_citable and span.structural_path
            )
        )
        locator = (
            "；".join(paths)
            if reference.answer_unit_id is not None
            else next(iter(paths), None)
        )
        citation: dict[str, object] = {
            "reference_id": natural_reference_id(trace_id, reference.alias),
            "alias": reference.alias,
            "document_name": reference.document_title,
            "document_title": reference.document_title,
            "source_kind": reference.citation_basis,
        }
        if locator is not None:
            citation["locator"] = locator
        if reference.excerpt is not None:
            citation["quote"] = reference.excerpt
        if reference.answer_unit_id is not None:
            citation["answer_unit_id"] = reference.answer_unit_id
        items.append(citation)
    return items


def public_natural_final(result: NaturalAnswerResult) -> dict[str, object]:
    """普通用户只能收到权威答案；管理员草稿和内部诊断留在 Trace。"""
    reviewed = result.publication_status is not None
    grounded_valid = (
        result.publication_status in {"GROUNDED_ANSWER", "GROUNDED_PARTIAL"}
        and result.validation_level
        == "source_binding_and_automated_support_review"
        and all(
            reference.answer_unit_id is not None
            and reference.excerpt is not None
            and bool(reference.source_spans)
            for reference in result.references
        )
    )
    published = (
        result.answer is not None
        and result.citation_status == "valid"
        and result.finish_reason == "stop"
        and bool(result.references)
        and (not reviewed or grounded_valid)
    )
    no_material = result.reason_code in {
        "NO_RETRIEVAL_MATERIAL",
        "NO_BOUNDED_SOURCE_PASSAGE",
    }
    generated = result.answer is not None or result.draft is not None
    status: str
    if result.publication_status is not None:
        status = (
            result.publication_status
            if result.publication_status
            not in {"GROUNDED_ANSWER", "GROUNDED_PARTIAL"}
            or published
            else "EXECUTION_ERROR"
        )
    elif published:
        status = "ANSWERED"
    elif generated and result.finish_reason != "stop":
        status = "TRUNCATED"
    elif no_material:
        status = "NO_MATERIAL"
    else:
        status = result.reason_code
    messages = {
        "CITATION_INVALID": "本次未形成可核对引用的答案。",
        "CITATION_MISSING": "本次未形成可核对引用的答案。",
        "NO_MATERIAL": "暂未找到可以回答该问题的资料。",
        "TRUNCATED": "回答未完整生成，请重新尝试。",
        "INSUFFICIENT_EVIDENCE": "当前资料不足以形成可核对的答复。",
        "SOURCE_CLARIFICATION": "请明确要查询的资料或对象后重试。",
        "EXECUTION_ERROR": "核对答案时出现异常，请稍后重试。",
        "CANCELLED": "已停止回答。",
    }
    projection: dict[str, object] = {
        "status": status,
        "published": published,
        "answer": result.answer if published else None,
        "citation_status": result.citation_status,
        "validation_level": result.validation_level,
        "citations": (
            public_natural_references(result.trace_id, result.references)
            if published
            else []
        ),
    }
    if not published:
        projection["user_message"] = messages.get(
            status, "暂未获得可核对的答复。"
        )
    return projection


@dataclass(slots=True)
class NaturalPublicStream:
    """在固定查询执行器和 HTTP 消费者间传递有界真实模型增量。"""

    runtime: ProductRuntime
    executor: QueryExecutor
    scope: KnowledgeBaseScope
    question: str
    conversation_id: str
    owner_id: str
    trace_id: str
    engine_id: Literal["wk-standard-v1", "wk-standard-pc-v1"]
    audit_context: QueryAuditContext
    authorization_guard: Callable[[], None]
    ignore_mentioned_sources: bool = False
    protocol: Literal[
        "wanshitong-natural-sse-v1", "wanshitong-natural-sse-v2"
    ] = NATURAL_PUBLIC_PROTOCOL
    messages: queue.Queue[tuple[str, dict[str, object]] | None] = field(
        default_factory=lambda: queue.Queue(maxsize=_QUEUE_CAPACITY)
    )
    cancellation: StreamCancellation = field(default_factory=StreamCancellation)
    _terminal: _Terminal = "OPEN"
    _terminal_lock: threading.Lock = field(default_factory=threading.Lock)
    _started: float = field(default_factory=time.monotonic)
    _sequence: int = 0
    _first_delta: float | None = None
    _events: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=_EVENT_RETENTION)
    )
    _event_condition: threading.Condition = field(
        default_factory=threading.Condition
    )
    _deadline_timer: threading.Timer | None = None
    _finished_at: float | None = None

    def start(self) -> Iterator[bytes]:
        """先取得查询容量，再交给 HTTP 同步迭代器。"""
        self.cancellation.deadline_monotonic = self._started + _TOTAL_SECONDS
        if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
            self._append_event(
                "meta",
                {
                    "run_id": self.trace_id,
                    "turn_id": self.trace_id,
                    "conversation_id": self.conversation_id,
                    "validation_level": (
                        "source_binding_and_automated_support_review"
                    ),
                },
            )
        timer: threading.Timer | None = None
        if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
            timer = threading.Timer(_TOTAL_SECONDS, self._timeout)
            timer.daemon = True
            self._deadline_timer = timer
        self.executor.submit(self._run)
        if timer is not None:
            if self.is_open:
                timer.start()
            return self.iterate_after(-1)
        return self._iterate()

    def cancel(self) -> bool:
        """仅显式停止当前开放的运行；v2 断连不调用此方法。"""
        with self._terminal_lock:
            if self._terminal != "OPEN":
                return False
            if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
                self._append_event("cancelled", {"reason": "user_requested"})
            self._terminal = "CANCELLED"
            self._finished_at = time.monotonic()
        self.cancellation.cancel()
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
        return True

    def _timeout(self) -> None:
        """独立于 HTTP 消费者执行总截止时间。"""
        with self._terminal_lock:
            if self._terminal != "OPEN":
                return
            self._append_event(
                "error",
                {
                    "code": "NATURAL_TIMEOUT",
                    "user_message": "回答超时，请重试。",
                },
            )
            self._terminal = "ERROR"
            self._finished_at = time.monotonic()
        self.cancellation.cancel()

    def _append_event(self, name: str, payload: dict[str, object]) -> None:
        """由生产端分配稳定序号并保存有限白名单事件。"""
        with self._event_condition:
            event: dict[str, object] = {
                "protocol": self.protocol,
                "type": name,
                "trace_id": self.trace_id,
                "turn_id": self.trace_id,
                "run_id": self.trace_id,
                "sequence": self._sequence,
                **payload,
            }
            self._sequence += 1
            self._events.append(event)
            self._event_condition.notify_all()

    def assert_replayable(self, last_sequence: int) -> None:
        """在 HTTP 响应头发出前判定事件窗口是否仍可重放。"""
        with self._event_condition:
            if (
                self._events
                and last_sequence < cast(int, self._events[0]["sequence"]) - 1
            ):
                raise NaturalStreamExpiredError()

    def iterate_after(self, last_sequence: int) -> Iterator[bytes]:
        """重放同一 run 的后续事件，再等待新增或唯一终态。"""
        self.assert_replayable(last_sequence)
        cursor = last_sequence
        while True:
            with self._event_condition:
                if (
                    self._events
                    and cursor < cast(int, self._events[0]["sequence"]) - 1
                ):
                    return
                available = tuple(
                    item
                    for item in self._events
                    if cast(int, item["sequence"]) > cursor
                )
                if not available:
                    if self._terminal != "OPEN":
                        return
                    self._event_condition.wait(timeout=_HEARTBEAT_SECONDS)
                    continue
            for item in available:
                self.authorization_guard()
                yield _frame(str(item["type"]), item)
                cursor = cast(int, item["sequence"])
                if item["type"] in {"final", "error", "cancelled"}:
                    return
            yield b": heartbeat\n\n"

    @property
    def retained_until(self) -> float:
        """终态有限保留，活跃运行受总截止时间约束。"""
        return (self._finished_at or self._started + _TOTAL_SECONDS) + (
            _RUN_RETENTION_SECONDS if self._finished_at else 0.0
        )

    @property
    def is_open(self) -> bool:
        """返回该运行是否仍允许继续执行。"""
        with self._terminal_lock:
            return self._terminal == "OPEN"

    @property
    def last_sequence(self) -> int:
        """返回生产端最新事件序号。"""
        with self._event_condition:
            return self._sequence - 1

    def _put(self, name: str, payload: dict[str, object]) -> None:
        if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
            with self._terminal_lock:
                if self._terminal != "OPEN" or self.cancellation.is_cancelled():
                    raise QueryCancelled("NATURAL_QUERY_CANCELLED")
                self._append_event(name, payload)
            return
        while not self.cancellation.is_cancelled():
            try:
                self.messages.put((name, payload), timeout=0.1)
            except queue.Full:
                continue
            return
        raise QueryCancelled("NATURAL_QUERY_CANCELLED")

    def _end(self) -> None:
        if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
            return
        while not self.cancellation.is_cancelled():
            try:
                self.messages.put(None, timeout=0.1)
            except queue.Full:
                continue
            return

    def _on_delta(self, delta: str) -> None:
        if not delta:
            return
        if self._first_delta is None:
            self._first_delta = time.monotonic()
            if self.protocol == NATURAL_PUBLIC_PROTOCOL:
                self._put("stage", {"stage": "generation"})
        if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
            return
        self._put("answer_delta", {"text": delta, "provisional": True})

    def _on_stage(self, stage: str) -> None:
        """仅发布真实发生的 CQ1 阶段，不包含任何草稿。"""
        if stage in {
            "retrieval",
            "evidence_organization",
            "support_review",
            "publication",
        }:
            self._put("stage", {"stage": stage})

    def _run(self) -> None:  # noqa: PLR0912, PLR0915
        result: NaturalAnswerResult | None = None
        failure: RagError | None = None
        trace_started = False
        trace_finished = False
        try:
            with self.runtime.conversations.lease(
                self.scope, self.conversation_id, owner_id=self.owner_id
            ):
                history = (
                    self.runtime.conversations.context(
                        self.scope, self.conversation_id, owner_id=self.owner_id
                    )
                    if self.protocol == NATURAL_PUBLIC_PROTOCOL
                    else ()
                )
                natural_history = (
                    self.runtime.conversations.natural_history(
                        self.scope,
                        self.conversation_id,
                        owner_id=self.owner_id,
                    )
                    if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2
                    else ()
                )
                self.runtime.traces.start(
                    self.trace_id,
                    self.scope,
                    self.question,
                    owner_id=self.owner_id,
                    save_body=False,
                    audit_context=self.audit_context,
                )
                trace_started = True
                if self.protocol == NATURAL_PUBLIC_PROTOCOL:
                    self._put("stage", {"stage": "retrieval"})
                request = SearchRequest(
                    scope=self.scope,
                    text=self.question,
                    limit=10,
                    conversation_context=history,
                    natural_history=natural_history,
                    ignore_mentioned_sources=self.ignore_mentioned_sources,
                    owner_identity=self.owner_id,
                    trace_id=self.trace_id,
                    singleflight_enabled=False,
                )
                with self.runtime.profiles.retrieval_service_lease(
                    self.scope.knowledge_base_id,
                    self.runtime.retrieval_runtime.retrieval,
                ) as service:
                    snapshot = service._query_snapshot(request)
                    frozen = request.model_copy(
                        update={
                            "expected_active_revision_id": (
                                snapshot.revision.index_revision_id
                            ),
                            "expected_serving_fingerprint": (
                                snapshot.serving_fingerprint
                            ),
                        }
                    )
                    with self.runtime.profiles.query_retrieval_scope(
                        self.scope.knowledge_base_id,
                        snapshot.revision.index_revision_id,
                    ):
                        result = service.search_natural(
                            frozen,
                            engine_id=self.engine_id,
                            cancellation=self.cancellation,
                            on_delta=self._on_delta,
                            grounded=(
                                self.protocol == NATURAL_PUBLIC_PROTOCOL_V2
                            ),
                            on_stage=(
                                self._on_stage
                                if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2
                                else None
                            ),
                        )
                if self.cancellation.is_cancelled():
                    raise QueryCancelled("NATURAL_QUERY_CANCELLED")
                if self.protocol == NATURAL_PUBLIC_PROTOCOL:
                    self._put("stage", {"stage": "validation"})
                projection = public_natural_final(result)
                with self._terminal_lock:
                    if (
                        self._terminal != "OPEN"
                        or self.cancellation.is_cancelled()
                    ):
                        raise QueryCancelled("NATURAL_QUERY_CANCELLED")
                    should_commit = projection["published"] or projection[
                        "status"
                    ] in {
                        "NO_MATERIAL",
                        "INSUFFICIENT_EVIDENCE",
                        "SOURCE_CLARIFICATION",
                    }
                    committed = (
                        self.runtime.conversations.commit_natural(
                            self.scope,
                            self.conversation_id,
                            self.question,
                            result,
                            owner_id=self.owner_id,
                        )
                        if should_commit
                        else True
                    )
                    if not committed:
                        raise RagError(
                            "自然会话资料版本已变化，请重试。",
                            stage="conversation.commit_natural",
                            code="NATURAL_TURN_NOT_COMMITTED",
                            trace_id=self.trace_id,
                        )
                    self.runtime.traces.finish(
                        self.trace_id,
                        result=result,
                        error=None,
                        cancelled=False,
                    )
                    trace_finished = True
                    if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
                        if projection["published"]:
                            self._append_event(
                                "references", {"items": projection["citations"]}
                            )
                        self._append_event("final", projection)
                    self._terminal = "FINAL"
                    self._finished_at = time.monotonic()
                if self.protocol == NATURAL_PUBLIC_PROTOCOL:
                    if projection["published"]:
                        self._put(
                            "references", {"items": projection["citations"]}
                        )
                    self._put("final", projection)
        except QueryCancelled:
            with self._terminal_lock:
                if self._terminal == "OPEN":
                    if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
                        self._append_event(
                            "cancelled", {"reason": "upstream_cancelled"}
                        )
                    self._terminal = "CANCELLED"
                    self._finished_at = time.monotonic()
        except RagError as error:
            failure = error
            with self._terminal_lock:
                if self._terminal == "OPEN":
                    if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
                        self._append_event(
                            "error",
                            {
                                "code": error.code,
                                "user_message": error.safe_message,
                            },
                        )
                    self._terminal = "ERROR"
                    self._finished_at = time.monotonic()
            if (
                self.protocol == NATURAL_PUBLIC_PROTOCOL
                and not self.cancellation.is_cancelled()
            ):
                self._put(
                    "error",
                    {"code": error.code, "user_message": error.safe_message},
                )
        except Exception:
            failure = RagError(
                "自然问答暂时不可用，请稍后重试。",
                stage="wanshitong.public.natural",
                code="NATURAL_INTERNAL_ERROR",
                trace_id=self.trace_id,
            )
            with self._terminal_lock:
                if self._terminal == "OPEN":
                    if self.protocol == NATURAL_PUBLIC_PROTOCOL_V2:
                        self._append_event(
                            "error",
                            {
                                "code": failure.code,
                                "user_message": failure.safe_message,
                            },
                        )
                    self._terminal = "ERROR"
                    self._finished_at = time.monotonic()
            if (
                self.protocol == NATURAL_PUBLIC_PROTOCOL
                and not self.cancellation.is_cancelled()
            ):
                self._put(
                    "error",
                    {
                        "code": failure.code,
                        "user_message": failure.safe_message,
                    },
                )
        finally:
            if self._deadline_timer is not None:
                self._deadline_timer.cancel()
            if trace_started and not trace_finished:
                try:
                    self.runtime.traces.finish(
                        self.trace_id,
                        result=result if failure is None else None,
                        error=failure,
                        cancelled=self._terminal == "CANCELLED",
                    )
                except Exception:
                    _LOGGER.exception(
                        "自然问答 Trace 收尾失败: %s", self.trace_id
                    )
            self._end()

    def _iterate(self) -> Iterator[bytes]:
        """终态只出现一次；心跳不占 sequence。"""
        try:
            yield self._event(
                "meta",
                {
                    "turn_id": self.trace_id,
                    "conversation_id": self.conversation_id,
                    "validation_level": "citation_binding_only",
                },
            )
            while True:
                remaining = _TOTAL_SECONDS - (time.monotonic() - self._started)
                if remaining <= 0:
                    self.cancel()
                    yield self._event(
                        "error",
                        {
                            "code": "NATURAL_TIMEOUT",
                            "user_message": "回答超时，请重试。",
                        },
                    )
                    return
                try:
                    queued = self.messages.get(
                        timeout=min(_HEARTBEAT_SECONDS, remaining)
                    )
                except queue.Empty:
                    yield b": heartbeat\n\n"
                    continue
                if queued is None:
                    return
                name, payload = queued
                self.authorization_guard()
                yield self._event(name, payload)
                if name in {"final", "error", "cancelled"}:
                    return
        finally:
            self.cancel()

    def _event(self, name: str, payload: dict[str, object]) -> bytes:
        event = {
            "protocol": NATURAL_PUBLIC_PROTOCOL,
            "type": name,
            "trace_id": self.trace_id,
            "turn_id": self.trace_id,
            "sequence": self._sequence,
            **payload,
        }
        self._sequence += 1
        return _frame(name, event)


class NaturalStreamRegistry:
    """按 Trace 保存运行与有界事件，供续接和显式停止。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: dict[str, NaturalPublicStream] = {}

    def register(self, stream: NaturalPublicStream) -> None:
        """登记已取得查询容量的自然流。"""
        with self._lock:
            self._prune()
            if len(self._streams) >= _REGISTRY_CAPACITY:
                expired = next(
                    (
                        key
                        for key, value in self._streams.items()
                        if not value.is_open
                    ),
                    None,
                )
                if expired is not None:
                    del self._streams[expired]
                else:
                    raise RuntimeError("自然流登记表容量已满。")
            self._streams[stream.trace_id] = stream

    def discard(self, stream: NaturalPublicStream) -> None:
        """仅移除同一对象，避免响应收尾误删其他流。"""
        with self._lock:
            if self._streams.get(stream.trace_id) is stream:
                del self._streams[stream.trace_id]

    def cancel_owned(
        self,
        trace_id: str,
        *,
        owner_id: str,
        conversation_id: str,
        scope: KnowledgeBaseScope | None = None,
    ) -> bool:
        """只允许原 owner 与原会话停止该轮；重复停止返回 false。"""
        with self._lock:
            self._prune()
            stream = self._streams.get(trace_id)
            if (
                stream is None
                or stream.owner_id != owner_id
                or stream.conversation_id != conversation_id
                or (scope is not None and stream.scope != scope)
            ):
                return False
            if stream.protocol == NATURAL_PUBLIC_PROTOCOL:
                del self._streams[trace_id]
        return stream.cancel()

    def get_owned(
        self,
        trace_id: str,
        *,
        owner_id: str,
        conversation_id: str,
        scope: KnowledgeBaseScope,
    ) -> NaturalPublicStream | None:
        """仅向原 owner、会话和知识范围返回同一运行。"""
        with self._lock:
            self._prune()
            stream = self._streams.get(trace_id)
            if (
                stream is None
                or stream.protocol != NATURAL_PUBLIC_PROTOCOL_V2
                or stream.owner_id != owner_id
                or stream.conversation_id != conversation_id
                or stream.scope != scope
            ):
                return None
            return stream

    def active_owned(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        scope: KnowledgeBaseScope,
    ) -> NaturalPublicStream | None:
        """供刷新后的会话历史发现保留期内的最新运行。"""
        with self._lock:
            self._prune()
            return next(
                (
                    stream
                    for stream in reversed(tuple(self._streams.values()))
                    if stream.protocol == NATURAL_PUBLIC_PROTOCOL_V2
                    and stream.owner_id == owner_id
                    and stream.conversation_id == conversation_id
                    and stream.scope == scope
                ),
                None,
            )

    def _prune(self) -> None:
        """调用方持锁；只清理完成后过期的记录。"""
        now = time.monotonic()
        for key, stream in tuple(self._streams.items()):
            if stream.retained_until <= now:
                del self._streams[key]
