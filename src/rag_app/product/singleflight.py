"""Product 非流式查询的有界、可取消 singleflight 协调。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import perf_counter
from typing import Final

from rag_app.core.errors import QueryCancelled, RagError
from rag_app.core.models import (
    RetrievalDiagnostics,
    RetrievalDiagnosticsSummary,
    SearchAnswerResult,
    StageTiming,
)
from rag_app.core.ports import CancellationPort

_WAIT_POLL_SECONDS: Final = 0.05
_DEFAULT_MAX_FLIGHTS: Final = 256


@dataclass(frozen=True, slots=True)
class SingleflightMetrics:
    """不含正文与 scope 值的进程内安全计数。"""

    inflight: int
    leaders: int
    followers: int
    follower_cancellations: int
    upstream_cancellations: int
    capacity_bypasses: int
    total_wait_ms: int


@dataclass(frozen=True, slots=True)
class _Failure:
    """可为每个 Follower 重建独立异常的安全快照。"""

    kind: str
    message: str
    stage: str = "query.singleflight"
    code: str = "INTERNAL_ERROR"
    retryable: bool = False
    details: object = ()
    reason_code: str = "QUERY_CANCELLED"


@dataclass(slots=True)
class _Flight:
    """一个规范 key 的共享结果、订阅者与上游取消器。"""

    event: threading.Event = field(default_factory=threading.Event)
    subscribers: dict[int, CancellationPort | None] = field(
        default_factory=dict
    )
    subscriber_registrations: dict[int, int] = field(default_factory=dict)
    closers: dict[int, Callable[[], None]] = field(default_factory=dict)
    next_registration: int = 0
    cancellation_triggered: bool = False
    result: SearchAnswerResult | None = None
    failure: _Failure | None = None


class _NeverCancelled:
    """为无取消端口的同步调用提供最小协议实现。"""

    def is_cancelled(self) -> bool:
        """返回固定未取消状态。

        Args:
            无参数；读取当前空实现。

        Returns:
            固定返回 False。

        """
        return False

    def register(self, closer: Callable[[], None]) -> int:
        """接受关闭回调但不保留注册。

        Args:
            closer: 无取消源时不会调用的关闭回调。

        Returns:
            固定的空注册 ID。

        """
        del closer
        return 0

    def unregister(self, registration: int) -> None:
        """忽略空注册 ID。

        Args:
            registration: 由 ``register`` 返回的空注册 ID。

        Returns:
            无返回值。

        """
        del registration


class _GroupCancellation:
    """仅当 flight 全部订阅者取消时关闭真实上游。"""

    def __init__(
        self,
        coordinator: ProductQuerySingleflight,
        flight: _Flight,
    ) -> None:
        self._coordinator = coordinator
        self._flight = flight

    def is_cancelled(self) -> bool:
        """判断全部当前订阅者是否已经取消。

        Args:
            无参数；读取当前 flight。

        Returns:
            全部订阅者均取消时为 True。

        """
        return self._coordinator._trigger_if_all_cancelled(self._flight)

    def register(self, closer: Callable[[], None]) -> int:
        """为真实上游注册协作关闭回调。

        Args:
            closer: 全部订阅者取消后调用的关闭函数。

        Returns:
            可用于解除注册的整数 ID。

        """
        return self._coordinator._register_closer(self._flight, closer)

    def unregister(self, registration: int) -> None:
        """解除真实上游的关闭回调。

        Args:
            registration: 先前返回的注册 ID。

        Returns:
            无返回值。

        """
        self._coordinator._unregister_closer(self._flight, registration)


class ProductQuerySingleflight:
    """共享底层计算，但从不共享调用方 trace、History 或正文。"""

    def __init__(self, *, max_flights: int = _DEFAULT_MAX_FLIGHTS) -> None:
        """冻结活动 flight 数量上限。

        Args:
            max_flights: 进程内不同等价键的最大在途数量。

        Raises:
            ValueError: 上限不是正数。

        """
        if max_flights <= 0:
            raise ValueError("singleflight 容量必须为正数。")
        self._max_flights = max_flights
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}
        self._next_subscriber = 0
        self._leaders = 0
        self._followers = 0
        self._follower_cancellations = 0
        self._upstream_cancellations = 0
        self._capacity_bypasses = 0
        self._total_wait_ms = 0

    def execute(
        self,
        key_hash: str,
        *,
        request_trace_id: str,
        cancellation: CancellationPort | None,
        compute: Callable[[CancellationPort], SearchAnswerResult],
    ) -> SearchAnswerResult:
        """加入等价计算，并为当前请求返回独立结果投影。

        Args:
            key_hash: 覆盖 scope、owner、Revision、语义和回答行为的摘要。
            request_trace_id: 当前请求独立公开 Trace ID。
            cancellation: 当前请求自己的协作取消端口。
            compute: Leader 使用聚合取消端口执行的底层查询。

        Returns:
            带 leader/follower 观测字段的当前请求结果。

        Raises:
            QueryCancelled: 当前订阅者取消，或全部订阅者取消上游。
            RagError: Leader 的安全业务失败，为 Follower 独立重建。

        """
        started = perf_counter()
        with self._lock:
            flight = self._flights.get(key_hash)
            if flight is None and len(self._flights) >= self._max_flights:
                self._capacity_bypasses += 1
                bypass = True
                subscriber_id = -1
                leader = True
            else:
                bypass = False
                if flight is None:
                    flight = _Flight()
                    self._flights[key_hash] = flight
                    leader = True
                    self._leaders += 1
                else:
                    leader = False
                    self._followers += 1
                self._next_subscriber += 1
                subscriber_id = self._next_subscriber
                flight.subscribers[subscriber_id] = cancellation
        if bypass:
            result = compute(cancellation or _NeverCancelled())
            return result
        if flight is None:
            raise AssertionError("singleflight 状态未初始化。")
        if cancellation is not None:

            def cancel_subscription() -> None:
                """调用方取消后重新判断是否应关闭共享上游。

                Args:
                    无参数；响应已绑定的调用方取消事件。

                Returns:
                    无返回值。

                """
                self._trigger_if_all_cancelled(flight)

            registration = cancellation.register(cancel_subscription)
            with self._lock:
                completed = flight.event.is_set()
                if not completed:
                    flight.subscriber_registrations[subscriber_id] = (
                        registration
                    )
            if completed:
                cancellation.unregister(registration)
        if leader:
            return self._run_leader(
                key_hash,
                flight,
                subscriber_id,
                cancellation,
                compute,
            )
        return self._wait_follower(
            key_hash,
            flight,
            subscriber_id,
            cancellation,
            started,
            request_trace_id,
        )

    def metrics(self) -> SingleflightMetrics:
        """读取固定字段安全计数，不返回 key 或请求身份。

        Args:
            无参数；读取当前协调器计数。

        Returns:
            不含查询、scope 或 key 的指标快照。

        """
        with self._lock:
            return SingleflightMetrics(
                inflight=len(self._flights),
                leaders=self._leaders,
                followers=self._followers,
                follower_cancellations=self._follower_cancellations,
                upstream_cancellations=self._upstream_cancellations,
                capacity_bypasses=self._capacity_bypasses,
                total_wait_ms=self._total_wait_ms,
            )

    def _run_leader(
        self,
        key_hash: str,
        flight: _Flight,
        subscriber_id: int,
        cancellation: CancellationPort | None,
        compute: Callable[[CancellationPort], SearchAnswerResult],
    ) -> SearchAnswerResult:
        group = _GroupCancellation(self, flight)
        try:
            result = compute(group)
        except BaseException as error:
            self._publish_failure(key_hash, flight, error)
            raise
        self._publish_result(key_hash, flight, result)
        if cancellation is not None and cancellation.is_cancelled():
            raise QueryCancelled("QUERY_CANCELLED")
        del subscriber_id
        return result.model_copy(
            update={
                "singleflight_role": "leader",
                "singleflight_key_hash": key_hash,
                "singleflight_wait_ms": 0,
            }
        )

    def _wait_follower(  # noqa: PLR0913, PLR0917
        self,
        key_hash: str,
        flight: _Flight,
        subscriber_id: int,
        cancellation: CancellationPort | None,
        started: float,
        request_trace_id: str,
    ) -> SearchAnswerResult:
        while not flight.event.wait(_WAIT_POLL_SECONDS):
            if cancellation is not None and cancellation.is_cancelled():
                with self._lock:
                    self._follower_cancellations += 1
                self._trigger_if_all_cancelled(flight)
                raise QueryCancelled("QUERY_CANCELLED")
        waited_ms = max(0, round((perf_counter() - started) * 1000))
        with self._lock:
            self._total_wait_ms += waited_ms
        if cancellation is not None and cancellation.is_cancelled():
            with self._lock:
                self._follower_cancellations += 1
            raise QueryCancelled("QUERY_CANCELLED")
        del subscriber_id
        if flight.failure is not None:
            raise _restore_failure(flight.failure)
        if flight.result is None:
            raise RuntimeError("singleflight 完成但缺少结果或失败。")
        return _follower_result(
            flight.result,
            key_hash=key_hash,
            wait_ms=waited_ms,
            trace_id=request_trace_id,
        )

    def _publish_result(
        self,
        key_hash: str,
        flight: _Flight,
        result: SearchAnswerResult,
    ) -> None:
        with self._lock:
            flight.result = result
            self._flights.pop(key_hash, None)
            flight.event.set()
            registrations = self._clear_subscribers_locked(flight)
        _unregister_subscribers(registrations)

    def _publish_failure(
        self,
        key_hash: str,
        flight: _Flight,
        error: BaseException,
    ) -> None:
        with self._lock:
            flight.failure = _failure(error)
            self._flights.pop(key_hash, None)
            flight.event.set()
            registrations = self._clear_subscribers_locked(flight)
        _unregister_subscribers(registrations)

    @staticmethod
    def _clear_subscribers_locked(
        flight: _Flight,
    ) -> tuple[tuple[CancellationPort, int], ...]:
        """在 coordinator 锁内冻结待解除的调用方取消桥。"""
        registrations = tuple(
            (cancellation, registration)
            for subscriber_id, registration in (
                flight.subscriber_registrations.items()
            )
            if (cancellation := flight.subscribers.get(subscriber_id))
            is not None
        )
        flight.subscribers.clear()
        flight.subscriber_registrations.clear()
        flight.closers.clear()
        return registrations

    def _trigger_if_all_cancelled(self, flight: _Flight) -> bool:
        closers: tuple[Callable[[], None], ...] = ()
        with self._lock:
            cancelled = bool(flight.subscribers) and all(
                value is not None and value.is_cancelled()
                for value in flight.subscribers.values()
            )
            if cancelled and not flight.cancellation_triggered:
                flight.cancellation_triggered = True
                self._upstream_cancellations += 1
                closers = tuple(flight.closers.values())
        for closer in closers:
            closer()
        return cancelled

    def _register_closer(
        self, flight: _Flight, closer: Callable[[], None]
    ) -> int:
        with self._lock:
            flight.next_registration += 1
            registration = flight.next_registration
            if flight.cancellation_triggered:
                invoke = True
            else:
                flight.closers[registration] = closer
                invoke = False
        if invoke:
            closer()
        return registration

    def _unregister_closer(self, flight: _Flight, registration: int) -> None:
        with self._lock:
            flight.closers.pop(registration, None)


def _failure(error: BaseException) -> _Failure:
    if isinstance(error, QueryCancelled):
        return _Failure(
            kind="cancelled",
            message="查询已取消。",
            reason_code=error.reason_code,
        )
    if isinstance(error, RagError):
        return _Failure(
            kind="rag",
            message=error.safe_message,
            stage=error.stage,
            code=error.code,
            retryable=error.retryable,
            details=error.details,
        )
    return _Failure(kind="unexpected", message=type(error).__name__)


def _unregister_subscribers(
    registrations: tuple[tuple[CancellationPort, int], ...],
) -> None:
    """解除已经结束 flight 的取消桥，清理异常不改写查询结果。"""
    for cancellation, registration in registrations:
        with suppress(Exception):
            cancellation.unregister(registration)


def _restore_failure(failure: _Failure) -> BaseException:
    if failure.kind == "cancelled":
        return QueryCancelled(failure.reason_code)
    if failure.kind == "rag":
        return RagError(
            failure.message,
            stage=failure.stage,
            code=failure.code,
            retryable=failure.retryable,
            details=failure.details,
        )
    return RuntimeError("共享查询执行失败。")


def _follower_result(
    result: SearchAnswerResult,
    *,
    key_hash: str,
    wait_ms: int,
    trace_id: str,
) -> SearchAnswerResult:
    diagnostics = result.diagnostics
    follower_diagnostics: RetrievalDiagnostics | None = None
    if diagnostics is not None:
        follower_diagnostics = diagnostics.model_copy(
            update={
                "provider_calls": (),
                "provider_call_details": (),
                "cache_hit": False,
                "stage_timings": (
                    StageTiming(
                        stage="singleflight_wait",
                        elapsed_ms=float(wait_ms),
                    ),
                ),
            }
        )
    summary = result.diagnostics_summary
    follower_summary: RetrievalDiagnosticsSummary | None = None
    if summary is not None:
        follower_summary = summary.model_copy(
            update={
                "provider_call_count": 0,
                "provider_retry_count": 0,
                "cache_hit": False,
            }
        )
    return result.model_copy(
        update={
            "cache_hit": False,
            "trace_id": trace_id,
            "result_origin": "singleflight",
            "generation_called_this_request": False,
            "rewrite_called_this_request": False,
            "diagnostics": follower_diagnostics,
            "diagnostics_summary": follower_summary,
            "singleflight_role": "follower",
            "singleflight_key_hash": key_hash,
            "singleflight_wait_ms": wait_ms,
        }
    )


__all__ = ["ProductQuerySingleflight", "SingleflightMetrics"]
