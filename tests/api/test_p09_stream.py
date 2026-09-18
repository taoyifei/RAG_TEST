"""P09 流式 HTTP 协调器的背压、时限与安全终态。"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator

from rag_app.api.p09_stream import P09AnswerStream, P09AnswerStreamRequest
from rag_app.core.errors import QueryCancelled, RagError
from rag_app.core.models import (
    AnswerClaim,
    AnswerStreamClaimEvent,
    AnswerStreamFinalEvent,
    AnswerStreamMetaEvent,
    AnswerStreamStageEvent,
    ClaimSupport,
    ConfidenceDecision,
    ConfidenceStatus,
    QueryKind,
    SearchAnswerResult,
)
from rag_app.core.ports import CancellationPort
from rag_app.query_executor import QueryExecutor

_TRACE = "trace_" + "1" * 32
_PROJECT = "prj_" + "2" * 32
_KB = "kb_" + "3" * 32
_REVISION = "irev_" + "4" * 32


def _request() -> P09AnswerStreamRequest:
    return P09AnswerStreamRequest(
        project_id=_PROJECT,
        knowledge_base_id=_KB,
        question="合成问题",
        trace_id=_TRACE,
        limit=5,
        include_related_content=False,
        history_mode="metadata_only",
        owner_id="synthetic-owner",
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等待流式协调状态超时。")
        time.sleep(0.005)


def _event_payload(frame: bytes) -> tuple[str, dict[str, object]] | None:
    if frame.startswith(b":"):
        return None
    lines = frame.decode().splitlines()
    return lines[0].removeprefix("event: "), json.loads(lines[1][6:])


def _next_event(stream: Iterator[bytes]) -> tuple[str, dict[str, object]]:
    while True:
        parsed = _event_payload(next(stream))
        if parsed is not None:
            return parsed


class _BackpressureSdk:
    """每次 emit 返回后记录，证明 worker 没有越过 HTTP 交付确认。"""

    def __init__(self) -> None:
        self.meta_returned = False
        self.stage_returned = False

    def answer_stream(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        emit: Callable[[object], None],
        cancellation: CancellationPort,
        trace_id: str,
        **kwargs: object,
    ) -> None:
        del text, cancellation, kwargs
        emit(
            AnswerStreamMetaEvent(
                trace_id=trace_id,
                sequence=0,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
        )
        self.meta_returned = True
        emit(
            AnswerStreamStageEvent.create(
                trace_id=trace_id,
                sequence=1,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                stage="accepted",
            )
        )
        self.stage_returned = True
        raise QueryCancelled("synthetic stop")


class _WaitingSdk:
    """发布可选 claim 后等待协调器取消，不生成 final。"""

    def __init__(self, *, claim: bool) -> None:
        self.claim = claim

    def answer_stream(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        emit: Callable[[object], None],
        cancellation: CancellationPort,
        trace_id: str,
        **kwargs: object,
    ) -> None:
        del text, kwargs
        emit(
            AnswerStreamMetaEvent(
                trace_id=trace_id,
                sequence=0,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
        )
        if self.claim:
            emit(
                AnswerStreamClaimEvent(
                    trace_id=trace_id,
                    sequence=1,
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                    claim_index=0,
                    claim=AnswerClaim(
                        text="资料员每周核对清单。",
                        supports=(
                            ClaimSupport(
                                support_id="S1",
                                quote="资料员每周核对清单。",
                            ),
                        ),
                    ),
                    active_index_revision_id=_REVISION,
                )
            )
        while not cancellation.is_cancelled():
            time.sleep(0.002)
        raise QueryCancelled("synthetic timeout")


class _SilentSdk:
    """模拟连首个协议事件都未产生的上游等待。"""

    def answer_stream(
        self,
        *_args: object,
        cancellation: CancellationPort,
        **_kwargs: object,
    ) -> None:
        while not cancellation.is_cancelled():
            time.sleep(0.002)
        raise QueryCancelled("synthetic timeout")


class _PartialFailureSdk(_WaitingSdk):
    """首条合法 claim 后返回只含安全字段的应用错误。"""

    def answer_stream(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        emit: Callable[[object], None],
        cancellation: CancellationPort,
        trace_id: str,
        **kwargs: object,
    ) -> None:
        del text, cancellation, kwargs
        emit(
            AnswerStreamMetaEvent(
                trace_id=trace_id,
                sequence=0,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
        )
        emit(
            AnswerStreamClaimEvent(
                trace_id=trace_id,
                sequence=1,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                claim_index=0,
                claim=AnswerClaim(
                    text="已核验事实。",
                    supports=(ClaimSupport(support_id="S1", quote="事实"),),
                ),
                active_index_revision_id=_REVISION,
            )
        )
        raise RagError(
            "流式回答未能安全收束。",
            stage="answer.stream",
            code="STREAM_PARTIAL_FAILED",
        )


class _UninterruptibleSdk:
    """模拟取消后仍须等待真实同步调用返回的上游。"""

    def __init__(self) -> None:
        self.cancel_observed = threading.Event()
        self.release = threading.Event()

    def answer_stream(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        emit: Callable[[object], None],
        cancellation: CancellationPort,
        trace_id: str,
        **kwargs: object,
    ) -> None:
        del text, cancellation, kwargs
        try:
            emit(
                AnswerStreamMetaEvent(
                    trace_id=trace_id,
                    sequence=0,
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                )
            )
        except QueryCancelled:
            self.cancel_observed.set()
            self.release.wait(timeout=2)
            raise


class _FinalThenFailureSdk:
    """模拟 Final 已交付后缓存或历史结算才发生故障。"""

    def answer_stream(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        emit: Callable[[object], None],
        cancellation: CancellationPort,
        trace_id: str,
        **kwargs: object,
    ) -> None:
        del text, cancellation, kwargs
        emit(
            AnswerStreamFinalEvent(
                trace_id=trace_id,
                sequence=0,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                result=SearchAnswerResult(
                    trace_id=trace_id,
                    status=ConfidenceStatus.INSUFFICIENT_EVIDENCE,
                    reason_code="INSUFFICIENT_SUPPORT",
                    confidence=ConfidenceDecision(
                        status=ConfidenceStatus.INSUFFICIENT_EVIDENCE,
                        score=0.0,
                    ),
                    query_kind=QueryKind.SIMPLE_FACT,
                    active_index_revision_id=_REVISION,
                    index_fingerprint="sha256:" + "5" * 64,
                    serving_fingerprint="sha256:" + "6" * 64,
                    route_reason_code="LEXICAL_ONLY",
                    rerank_execution_mode="bypass",
                    generation_mode="none",
                    cache_key="sha256:" + "7" * 64,
                ),
            )
        )
        raise RagError(
            "Final 后的合成结算故障。",
            stage="history.finish",
            code="HISTORY_UNAVAILABLE",
        )


class _StagedFinalSdk:
    """先交付协议进度，超过旧首内容时限后才发送 Final。"""

    def answer_stream(  # noqa: PLR0913
        self,
        project_id: str,
        knowledge_base_id: str,
        text: str,
        *,
        emit: Callable[[object], None],
        cancellation: CancellationPort,
        trace_id: str,
        **kwargs: object,
    ) -> None:
        del text, kwargs
        emit(
            AnswerStreamMetaEvent(
                trace_id=trace_id,
                sequence=0,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
            )
        )
        emit(
            AnswerStreamStageEvent.create(
                trace_id=trace_id,
                sequence=1,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                stage="generation",
            )
        )
        time.sleep(0.07)
        if cancellation.is_cancelled():
            raise QueryCancelled("synthetic cancellation")
        emit(
            AnswerStreamFinalEvent(
                trace_id=trace_id,
                sequence=2,
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                result=SearchAnswerResult(
                    trace_id=trace_id,
                    status=ConfidenceStatus.INSUFFICIENT_EVIDENCE,
                    reason_code="INSUFFICIENT_SUPPORT",
                    confidence=ConfidenceDecision(
                        status=ConfidenceStatus.INSUFFICIENT_EVIDENCE,
                        score=0.0,
                    ),
                    query_kind=QueryKind.SIMPLE_FACT,
                    active_index_revision_id=_REVISION,
                    index_fingerprint="sha256:" + "5" * 64,
                    serving_fingerprint="sha256:" + "6" * 64,
                    route_reason_code="LEXICAL_ONLY",
                    rerank_execution_mode="bypass",
                    generation_mode="none",
                    cache_key="sha256:" + "7" * 64,
                ),
            )
        )


class _LateFinalSdk(_StagedFinalSdk):
    """模拟超时后仍迟到的 Final。"""

    def answer_stream(self, *args: object, **kwargs: object) -> None:
        time.sleep(0.07)
        super().answer_stream(*args, **kwargs)


def _stream(
    executor: QueryExecutor,
    sdk: object,
    *,
    first: float = 1.0,
    idle: float = 1.0,
    total: float = 2.0,
) -> P09AnswerStream:
    return P09AnswerStream(
        executor=executor,
        sdk=sdk,  # type: ignore[arg-type]
        request=_request(),
        render_final=lambda _result: {},
        versioned_protocol=True,
        heartbeat_seconds=0.01,
        first_content_seconds=first,
        idle_seconds=idle,
        total_seconds=total,
    )


def test_worker_waits_for_each_http_delivery_acknowledgement() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    sdk = _BackpressureSdk()
    stream = _stream(executor, sdk).start()
    try:
        assert _next_event(stream)[0] == "meta"
        assert sdk.meta_returned is False
        assert _next_event(stream)[0] == "stage"
        assert sdk.meta_returned is True
        assert sdk.stage_returned is False
    finally:
        stream.close()
        _wait_for(lambda: executor.in_flight == 0)
        executor.close()


def test_first_content_total_and_idle_timeouts_are_distinct() -> None:
    cases = (
        (_SilentSdk(), 0.04, 0.5, 1.0, "STREAM_FIRST_CONTENT_TIMEOUT", False),
        (
            _WaitingSdk(claim=False),
            0.5,
            0.5,
            0.04,
            "STREAM_TOTAL_TIMEOUT",
            False,
        ),
        (_WaitingSdk(claim=True), 0.5, 0.04, 1.0, "STREAM_IDLE_TIMEOUT", True),
    )
    for sdk, first, idle, total, expected, partial in cases:
        executor = QueryExecutor(queue_wait_seconds=1.0)
        stream = _stream(
            executor,
            sdk,
            first=first,
            idle=idle,
            total=total,
        ).start()
        try:
            events = []
            while not events or events[-1][0] != "error":
                events.append(_next_event(stream))
            assert events[-1][1]["code"] == expected
            assert events[-1][1]["partial"] is partial
        finally:
            stream.close()
            _wait_for(lambda current=executor: current.in_flight == 0)
            executor.close()


def test_error_after_claim_is_partial_and_has_no_unvalidated_body() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    stream = _stream(executor, _PartialFailureSdk(claim=True)).start()
    try:
        events = [_next_event(stream) for _ in range(3)]
        assert [event for event, _ in events] == ["meta", "claim", "error"]
        assert events[-1][1]["partial"] is True
        assert events[-1][1]["code"] == "STREAM_PARTIAL_FAILED"
        assert "未核验" not in json.dumps(events[-1][1], ensure_ascii=False)
    finally:
        stream.close()
        _wait_for(lambda: executor.in_flight == 0)
        executor.close()


def test_disconnect_keeps_slot_until_sync_upstream_returns() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    sdk = _UninterruptibleSdk()
    stream = _stream(executor, sdk).start()
    assert _next_event(stream)[0] == "meta"
    stream.close()
    try:
        assert sdk.cancel_observed.wait(timeout=1)
        assert executor.in_flight == 1
        sdk.release.set()
        _wait_for(lambda: executor.in_flight == 0)
    finally:
        sdk.release.set()
        executor.close()


def test_total_deadline_releases_worker_even_when_consumer_stops_reading() -> (
    None
):
    executor = QueryExecutor(queue_wait_seconds=1.0)
    coordinator = _stream(
        executor,
        _WaitingSdk(claim=False),
        first=1.0,
        idle=1.0,
        total=0.04,
    )
    stream = coordinator.start()
    try:
        # 刻意不拉取首个 meta；独立总时限仍须取消 worker，不能依赖客户
        # 端继续调用迭代器才释放真实查询槽。
        _wait_for(lambda: executor.in_flight == 0)
        assert coordinator.cancellation.is_cancelled()
    finally:
        stream.close()
        executor.close()


def test_failure_after_final_does_not_append_second_terminal() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    stream = _stream(executor, _FinalThenFailureSdk()).start()
    try:
        assert _next_event(stream)[0] == "final"
        try:
            next(stream)
        except StopIteration:
            pass
        else:
            raise AssertionError("Final 后不应再出现第二个事件。")
    finally:
        stream.close()
        _wait_for(lambda: executor.in_flight == 0)
        executor.close()


def test_final_only_route_stage_prevents_first_event_timeout() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    coordinator = _stream(
        executor, _StagedFinalSdk(), first=0.04, idle=0.3, total=1.0
    )
    stream = coordinator.start()
    try:
        events = [_event_payload(frame) for frame in stream]
        names = [event[0] for event in events if event is not None]
        assert names == ["meta", "stage", "final"]
        assert coordinator.first_protocol_event_delivered is True
        assert coordinator.answer_content_delivered is True
        assert coordinator.terminal_state == "FINAL"
        assert coordinator.final_acknowledged.is_set()
        assert not coordinator.cancellation.is_cancelled()
        coordinator.cancel()
        assert not coordinator.cancellation.is_cancelled()
    finally:
        _wait_for(lambda: executor.in_flight == 0)
        executor.close()


def test_unacknowledged_final_disconnect_cancels_worker() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    coordinator = _stream(executor, _StagedFinalSdk())
    stream = coordinator.start()
    try:
        assert _next_event(stream)[0] == "meta"
        assert _next_event(stream)[0] == "stage"
        assert _next_event(stream)[0] == "final"
        assert not coordinator.final_acknowledged.is_set()
        stream.close()
        assert coordinator.cancellation.is_cancelled()
    finally:
        _wait_for(lambda: executor.in_flight == 0)
        executor.close()


def test_timeout_emits_exactly_one_error_and_ignores_late_final() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    coordinator = _stream(
        executor, _LateFinalSdk(), first=0.03, idle=0.3, total=1.0
    )
    stream = coordinator.start()
    try:
        events = [_event_payload(frame) for frame in stream]
        names = [event[0] for event in events if event is not None]
        assert names == ["error"]
        assert coordinator.terminal_state == "ERROR"
        _wait_for(lambda: executor.in_flight == 0)
        assert coordinator.terminal_state == "ERROR"
    finally:
        executor.close()


def test_worker_cancel_without_prior_terminal_emits_cancelled() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    coordinator = _stream(executor, _BackpressureSdk())
    stream = coordinator.start()
    try:
        events = [_event_payload(frame) for frame in stream]
        names = [event[0] for event in events if event is not None]
        assert names == ["meta", "stage", "cancelled"]
        assert coordinator.terminal_state == "CANCELLED"
    finally:
        _wait_for(lambda: executor.in_flight == 0)
        executor.close()


def test_provider_failure_emits_exactly_one_error() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)
    coordinator = _stream(executor, _PartialFailureSdk(claim=True))
    stream = coordinator.start()
    try:
        events = [_event_payload(frame) for frame in stream]
        names = [event[0] for event in events if event is not None]
        assert names == ["meta", "claim", "error"]
        assert coordinator.terminal_state == "ERROR"
    finally:
        _wait_for(lambda: executor.in_flight == 0)
        executor.close()


def test_final_render_failure_emits_exactly_one_error() -> None:
    executor = QueryExecutor(queue_wait_seconds=1.0)

    def _broken_render(_result: SearchAnswerResult) -> dict[str, object]:
        raise ValueError("synthetic render failure")

    coordinator = _stream(executor, _FinalThenFailureSdk())
    coordinator.render_final = _broken_render
    stream = coordinator.start()
    try:
        events = [_event_payload(frame) for frame in stream]
        names = [event[0] for event in events if event is not None]
        assert names == ["error"]
        assert coordinator.terminal_state == "ERROR"
    finally:
        _wait_for(lambda: executor.in_flight == 0)
        executor.close()
