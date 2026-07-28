from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from rag_app.api.app import ApiServices, create_app
from rag_app.api.stream import QueryStreamRequest, stream_query
from rag_app.health import ComponentStatus, ReadinessService
from rag_app.query_executor import QueryExecutor
from rag_app.query_service import QueryOutcome, StageEvent, StageName
from rag_app.state.conversations import ConversationStore
from rag_app.state.feedback import FeedbackStore
from rag_app.state.jobs import JobStore


@dataclass(frozen=True, slots=True)
class _ReadyProbe:
    def check(self) -> ComponentStatus:
        return ComponentStatus("local", True, "ready", 1, 1)


class _NeverQuery:
    def ask(
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
    ) -> QueryOutcome:
        del trace_id, conversation_id, question, now, emit
        raise AssertionError("准入失败后不得执行查询。")


class _BlockingQuery:
    def __init__(self) -> None:
        self.release = threading.Event()

    def ask(
        self,
        *,
        trace_id: str,
        conversation_id: str,
        question: str,
        now: datetime,
        emit: Callable[[StageEvent], None],
    ) -> QueryOutcome:
        del conversation_id, question, now
        emit(
            StageEvent(
                trace_id=trace_id,
                stage=StageName.RETRIEVE,
                elapsed_ms=1,
                metrics={"candidate_count": 0},
            )
        )
        self.release.wait()
        raise RuntimeError("synthetic query stop")


def _wait_for_in_flight(
    executor: QueryExecutor,
    expected: int,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while executor.in_flight != expected:
        if time.monotonic() >= deadline:
            raise AssertionError("等待查询容量状态超时。")
        time.sleep(0.005)


def _blocking_admissions(
    executor: QueryExecutor,
    *,
    count: int,
    release: threading.Event,
) -> tuple[ThreadPoolExecutor, tuple[Future[None], ...]]:
    def work() -> None:
        release.wait()

    callers = ThreadPoolExecutor(max_workers=count)
    admissions = tuple(
        callers.submit(executor.submit, work) for _ in range(count)
    )
    _wait_for_in_flight(executor, count)
    return callers, admissions


def _client(
    tmp_path: Path,
    executor: QueryExecutor,
) -> tuple[TestClient, str]:
    state_path = tmp_path / "state.sqlite3"
    conversations = ConversationStore(
        state_path,
        ttl_seconds=300,
        max_rounds=3,
    )
    conversations.initialize()
    jobs = JobStore(state_path)
    jobs.initialize()
    feedback = FeedbackStore(state_path)
    feedback.initialize()
    readiness = ReadinessService((_ReadyProbe(),))
    readiness.refresh_once()
    query_token = uuid.uuid4().hex
    app = create_app(
        ApiServices(
            readiness=readiness,
            query_token=query_token,
            admin_token=uuid.uuid4().hex,
            query=_NeverQuery(),  # type: ignore[arg-type]
            query_executor=executor,
            conversations=conversations,
            jobs=jobs,
            feedback=feedback,
            pipeline_fingerprint="pipeline-1",
        )
    )
    return TestClient(app), query_token


def _chat_headers(query_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {query_token}"}


def test_chat_returns_429_before_stream_when_capacity_is_full(
    tmp_path: Path,
) -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    release = threading.Event()
    callers, admissions = _blocking_admissions(
        executor,
        count=12,
        release=release,
    )
    try:
        client, query_token = _client(tmp_path, executor)
        response = client.post(
            "/api/chat",
            headers=_chat_headers(query_token),
            json={"conversation_id": "c", "question": "问题"},
        )
    finally:
        release.set()
        for admission in admissions:
            admission.result(timeout=1)
        callers.shutdown()
        executor.close()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {"detail": "query capacity unavailable"}


def test_chat_returns_429_when_queue_wait_times_out(
    tmp_path: Path,
) -> None:
    executor = QueryExecutor(queue_wait_seconds=0.05)
    release = threading.Event()
    callers, admissions = _blocking_admissions(
        executor,
        count=4,
        release=release,
    )
    try:
        client, query_token = _client(tmp_path, executor)
        response = client.post(
            "/api/chat",
            headers=_chat_headers(query_token),
            json={"conversation_id": "c", "question": "问题"},
        )
    finally:
        release.set()
        for admission in admissions:
            admission.result(timeout=1)
        callers.shutdown()
        executor.close()

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {"detail": "query capacity unavailable"}


def test_stream_close_does_not_hold_capacity_after_query_finishes() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    query = _BlockingQuery()
    try:
        stream = cast(
            Generator[bytes, None, None],
            stream_query(
                executor=executor,
                service=query,  # type: ignore[arg-type]
                request=QueryStreamRequest(
                    trace_id="a" * 32,
                    conversation_id="conversation",
                    question="合成问题",
                ),
            ),
        )

        assert next(stream).startswith(b'{"type":"stage"')
        stream.close()
        query.release.set()
        _wait_for_in_flight(executor, 0)
    finally:
        query.release.set()
        executor.close()
