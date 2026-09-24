"""湾事通自然问答的公共增量流、会话与单次终态。"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Final, Literal

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
_QUEUE_CAPACITY = 16
_HEARTBEAT_SECONDS = 2.0
_TOTAL_SECONDS = 240.0
_Terminal = Literal["OPEN", "FINAL", "ERROR", "CANCELLED"]
_LOGGER = logging.getLogger(__name__)


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
        locator = next(
            (
                " / ".join(span.structural_path)
                for span in reference.source_spans
                if span.structural_path
            ),
            None,
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
        items.append(citation)
    return items


def public_natural_final(result: NaturalAnswerResult) -> dict[str, object]:
    """普通用户只能收到权威答案；管理员草稿和内部诊断留在 Trace。"""
    published = (
        result.answer is not None
        and result.citation_status == "valid"
        and result.finish_reason == "stop"
        and bool(result.references)
    )
    no_material = result.reason_code in {
        "NO_RETRIEVAL_MATERIAL",
        "NO_BOUNDED_SOURCE_PASSAGE",
    }
    generated = result.answer is not None or result.draft is not None
    status = (
        "ANSWERED"
        if published
        else "TRUNCATED"
        if generated and result.finish_reason != "stop"
        else "NO_MATERIAL"
        if no_material
        else result.reason_code
    )
    messages = {
        "CITATION_INVALID": "本次未形成可核对引用的答案。",
        "CITATION_MISSING": "本次未形成可核对引用的答案。",
        "NO_MATERIAL": "暂未找到可以回答该问题的资料。",
        "TRUNCATED": "回答未完整生成，请重新尝试。",
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
    messages: queue.Queue[tuple[str, dict[str, object]] | None] = field(
        default_factory=lambda: queue.Queue(maxsize=_QUEUE_CAPACITY)
    )
    cancellation: StreamCancellation = field(default_factory=StreamCancellation)
    _terminal: _Terminal = "OPEN"
    _terminal_lock: threading.Lock = field(default_factory=threading.Lock)
    _started: float = field(default_factory=time.monotonic)
    _sequence: int = 0
    _first_delta: float | None = None

    def start(self) -> Iterator[bytes]:
        """先取得查询容量，再交给 HTTP 同步迭代器。"""
        self.cancellation.deadline_monotonic = self._started + _TOTAL_SECONDS
        self.executor.submit(self._run)
        return self._iterate()

    def cancel(self) -> None:
        """在 HTTP 完成、断连或停止时释放上游调用和背压等待。"""
        with self._terminal_lock:
            if self._terminal == "OPEN":
                self._terminal = "CANCELLED"
        self.cancellation.cancel()

    def _put(self, name: str, payload: dict[str, object]) -> None:
        while not self.cancellation.is_cancelled():
            try:
                self.messages.put((name, payload), timeout=0.1)
            except queue.Full:
                continue
            return
        raise QueryCancelled("NATURAL_QUERY_CANCELLED")

    def _end(self) -> None:
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
            self._put("stage", {"stage": "generation"})
        self._put("answer_delta", {"text": delta, "provisional": True})

    def _run(self) -> None:  # noqa: PLR0912, PLR0915
        result: NaturalAnswerResult | None = None
        failure: RagError | None = None
        trace_started = False
        trace_finished = False
        try:
            with self.runtime.conversations.lease(
                self.scope, self.conversation_id, owner_id=self.owner_id
            ):
                history = self.runtime.conversations.context(
                    self.scope, self.conversation_id, owner_id=self.owner_id
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
                self._put("stage", {"stage": "retrieval"})
                request = SearchRequest(
                    scope=self.scope,
                    text=self.question,
                    limit=10,
                    conversation_context=history,
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
                        )
                if self.cancellation.is_cancelled():
                    raise QueryCancelled("NATURAL_QUERY_CANCELLED")
                self._put("stage", {"stage": "validation"})
                projection = public_natural_final(result)
                with self._terminal_lock:
                    if (
                        self._terminal != "OPEN"
                        or self.cancellation.is_cancelled()
                    ):
                        raise QueryCancelled("NATURAL_QUERY_CANCELLED")
                    should_commit = (
                        projection["published"]
                        or projection["status"] == "NO_MATERIAL"
                    )
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
                    self._terminal = "FINAL"
                if projection["published"]:
                    self._put("references", {"items": projection["citations"]})
                self._put("final", projection)
        except QueryCancelled:
            with self._terminal_lock:
                if self._terminal == "OPEN":
                    self._terminal = "CANCELLED"
        except RagError as error:
            failure = error
            with self._terminal_lock:
                if self._terminal == "OPEN":
                    self._terminal = "ERROR"
            if not self.cancellation.is_cancelled():
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
                    self._terminal = "ERROR"
            if not self.cancellation.is_cancelled():
                self._put(
                    "error",
                    {
                        "code": failure.code,
                        "user_message": failure.safe_message,
                    },
                )
        finally:
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
