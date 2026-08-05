"""有限重试、并发闸门、健康剔除与熔断。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar
from urllib.parse import urlparse

import httpx

__all__ = [
    "ExternalRequestRejectedError",
    "ExternalServiceUnavailableError",
    "ExternalStreamInterruptedError",
    "HttpJsonResponse",
    "HttpStreamAttempt",
    "HttpStreamResponse",
    "ResiliencePolicy",
    "ResilientHttpPool",
    "StreamCancellation",
    "StreamCancelledError",
]

_TRANSIENT_STATUSES = frozenset({408, 425, 429})
_SERVER_ERROR_MIN = 500
_SERVER_ERROR_MAX = 599
_StreamValue = TypeVar("_StreamValue")


class ExternalServiceUnavailableError(RuntimeError):
    """所有健康端点均未在有限尝试内成功。"""


class ExternalRequestRejectedError(RuntimeError):
    """端点明确拒绝不可重试请求。"""


class ExternalStreamInterruptedError(RuntimeError):
    """流已发布 content delta 后中断，禁止切换端点重放。"""


class StreamCancelledError(RuntimeError):
    """调用方主动取消了仍在进行的外部流。"""


class _RetryableStreamError(RuntimeError):
    """首个 content delta 前可切换一次端点的瞬时失败。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StreamCancellation:
    """在线程间传播取消并立即关闭已登记的上游响应。"""

    def __init__(self) -> None:
        """创建尚未取消且没有上游关闭器的令牌。

        Args:
            无参数；创建独立取消状态。

        Returns:
            无返回值。

        """
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._next_registration = 0
        self._closers: dict[int, Callable[[], None]] = {}

    def cancel(self) -> None:
        """标记取消并同步关闭当前所有上游响应。

        Args:
            无参数；作用于当前令牌。

        Returns:
            无返回值；重复调用保持幂等。

        """
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            closers = tuple(self._closers.values())
            self._closers.clear()
        for closer in closers:
            _close_quietly(closer)

    def is_cancelled(self) -> bool:
        """返回调用方是否已经取消。

        Args:
            无参数；读取当前令牌。

        Returns:
            已取消时为 ``True``。

        """
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """等待取消信号，供同步工作线程停止阻塞。

        Args:
            timeout: 最长等待秒数；``None`` 表示无限等待。

        Returns:
            超时前收到取消信号时为 ``True``。

        """
        return self._event.wait(timeout)

    def register(self, closer: Callable[[], None]) -> int:
        """登记一个取消时必须立即执行的上游关闭器。

        Args:
            closer: 不接收参数的幂等资源关闭函数。

        Returns:
            可用于解除登记的进程内整数标识。

        """
        with self._lock:
            if self._event.is_set():
                registration = -1
            else:
                registration = self._next_registration
                self._next_registration += 1
                self._closers[registration] = closer
        if registration == -1:
            _close_quietly(closer)
        return registration

    def unregister(self, registration: int) -> None:
        """移除已自然结束的上游关闭器。

        Args:
            registration: ``register`` 返回的整数标识。

        Returns:
            无返回值；标识已移除时保持幂等。

        """
        with self._lock:
            self._closers.pop(registration, None)


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    """一次成功外部调用的非敏感审计信息。"""

    endpoint: str
    payload: object
    retry_count: int
    elapsed_seconds: float


@dataclass(slots=True)
class HttpStreamAttempt:
    """一次已选定端点且尚未完成的 HTTP 字节流。"""

    _response: httpx.Response
    content_delta_received: bool = False

    def iter_bytes(self) -> Iterator[bytes]:
        """逐块读取未解码响应体。

        Args:
            无参数；读取当前响应。

        Yields:
            transport 实际提供的任意字节分片。

        Returns:
            响应结束后无额外返回值。

        """
        yield from self._response.iter_bytes()

    def mark_content_delta(self) -> None:
        """记录已收到首个非空模型 content delta。

        Args:
            无参数；更新当前尝试状态。

        Returns:
            无返回值。

        """
        self.content_delta_received = True


@dataclass(frozen=True, slots=True)
class HttpStreamResponse(Generic[_StreamValue]):
    """一次成功外部流的结果和非敏感调度审计。"""

    endpoint: str
    value: _StreamValue
    retry_count: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class ResiliencePolicy:
    """一个外部依赖的重试、熔断与并发参数。"""

    max_attempts: int
    failure_threshold: int
    cooldown_seconds: float
    max_concurrency: int

    def __post_init__(self) -> None:
        """拒绝无界、零值或负值策略。"""
        if min(
            self.max_attempts,
            self.failure_threshold,
            self.max_concurrency,
        ) <= 0:
            raise ValueError("尝试、失败阈值和并发上限必须为正数。")
        if self.cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds 必须为正数。")


@dataclass(slots=True)
class _EndpointState:
    base_url: str
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0
    in_flight: int = 0
    ewma_latency_seconds: float | None = None


class ResilientHttpPool:
    """在固定端点集合上执行同步、有限且可熔断的 JSON 请求。"""

    def __init__(
        self,
        endpoints: tuple[str, ...],
        *,
        client: httpx.Client,
        policy: ResiliencePolicy,
        validator: Callable[[object], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """冻结端点与韧性参数。

        Args:
            endpoints: 至少一个 http/https 基础 URL。
            client: 已配置独立超时的 httpx 客户端。
            policy: 有界重试、熔断和并发策略。
            validator: 可选的服务级响应 schema 校验器。
            clock: 可测试的单调时钟。

        Raises:
            ValueError: 端点或任一数值参数无效。

        """
        if not endpoints:
            raise ValueError("至少配置一个外部端点。")
        normalized = tuple(_normalize_endpoint(item) for item in endpoints)
        if len(set(normalized)) != len(normalized):
            raise ValueError("外部端点不能重复。")
        self._states = [_EndpointState(item) for item in normalized]
        self._client = client
        self._policy = policy
        self._validator = validator
        self._semaphore = threading.BoundedSemaphore(policy.max_concurrency)
        self._clock = clock
        self._lock = threading.Lock()
        self._next_index = 0

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None,
        headers: Mapping[str, str] | None = None,
        validator: Callable[[object], object] | None = None,
    ) -> HttpJsonResponse:
        """请求 JSON，并只对网络错误和瞬态状态码切换端点。

        Args:
            method: HTTP 方法。
            path: 以 `/` 开头且不含完整主机的路径。
            payload: JSON 请求体；不会写入异常消息。
            headers: 可选请求头；不会写入返回审计信息。
            validator: 覆盖服务级校验器的本次响应 schema 校验器。

        Returns:
            JSON 内容、所用端点、重试数与耗时。

        Raises:
            ExternalRequestRejectedError: 收到不可重试 HTTP 状态。
            ExternalServiceUnavailableError: 健康端点全部失败或熔断。
            ValueError: path 不是安全相对路径。

        """
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("path 必须是以单个斜杠开头的相对路径。")
        started = self._clock()
        last_reason = "NO_HEALTHY_ENDPOINT"
        with self._semaphore:
            for attempt in range(self._policy.max_attempts):
                state = self._select_endpoint()
                if state is None:
                    break
                attempt_started = self._clock()
                try:
                    response = self._client.request(
                        method,
                        state.base_url + path,
                        json=payload,
                        headers=headers,
                    )
                except httpx.HTTPError:
                    last_reason = "HTTP_TRANSPORT"
                    self._record_failure(
                        state,
                        self._clock() - attempt_started,
                    )
                    continue
                if _is_transient_status(response.status_code):
                    last_reason = f"HTTP_{response.status_code}"
                    self._record_failure(
                        state,
                        self._clock() - attempt_started,
                    )
                    continue
                if response.is_error or response.is_redirect:
                    self._record_rejection(
                        state,
                        self._clock() - attempt_started,
                    )
                    raise ExternalRequestRejectedError(
                        f"外部请求被拒绝：HTTP_{response.status_code}。"
                    )
                try:
                    response_payload = response.json()
                except ValueError:
                    self._record_rejection(
                        state,
                        self._clock() - attempt_started,
                    )
                    raise ExternalRequestRejectedError(
                        "外部请求返回不可用内容：INVALID_JSON。"
                    ) from None
                active_validator = (
                    validator
                    if validator is not None
                    else self._validator
                )
                if active_validator is not None:
                    try:
                        response_payload = active_validator(
                            response_payload
                        )
                    except (OverflowError, TypeError, ValueError) as error:
                        self._record_rejection(
                            state,
                            self._clock() - attempt_started,
                        )
                        raise ExternalRequestRejectedError(
                            "外部请求返回不可用内容："
                            "INVALID_RESPONSE_SCHEMA。"
                        ) from error
                self._record_success(
                    state,
                    self._clock() - attempt_started,
                )
                return HttpJsonResponse(
                    endpoint=state.base_url,
                    payload=response_payload,
                    retry_count=attempt,
                    elapsed_seconds=self._clock() - started,
                )
        raise ExternalServiceUnavailableError(
            f"外部依赖不可用：{last_reason}。"
        )

    def request_stream(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        payload: object | None,
        consumer: Callable[[HttpStreamAttempt], _StreamValue],
        cancellation: StreamCancellation,
        headers: Mapping[str, str] | None = None,
        max_attempts: int = 2,
    ) -> HttpStreamResponse[_StreamValue]:
        """消费一个可取消字节流并限制首 delta 前的故障转移。

        Args:
            method: HTTP 方法。
            path: 以单个 ``/`` 开头的安全相对路径。
            payload: 不写入异常或审计的 JSON 请求体。
            consumer: 解析当前响应并返回完整业务结果的同步函数。
            cancellation: 可从客户端断开线程触发的取消令牌。
            headers: 可选请求头。
            max_attempts: 包含首次请求的最大端点尝试数。

        Returns:
            consumer 结果、成功端点、重试数与总耗时。

        Raises:
            ExternalRequestRejectedError: 状态或响应内容不可重试。
            ExternalServiceUnavailableError: 首 delta 前的有限尝试均失败。
            ExternalStreamInterruptedError: 首 delta 后 transport 中断。
            StreamCancelledError: 调用方取消并关闭上游响应。
            ValueError: 路径或尝试上限无效。

        """
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("path 必须是以单个斜杠开头的相对路径。")
        if max_attempts <= 0:
            raise ValueError("stream max_attempts 必须为正数。")
        started = self._clock()
        last_reason = "NO_HEALTHY_ENDPOINT"
        attempted_endpoints: set[str] = set()
        attempt_limit = min(max_attempts, self._policy.max_attempts)
        with self._semaphore:
            for attempt_index in range(attempt_limit):
                if cancellation.is_cancelled():
                    raise StreamCancelledError("LLM_STREAM_CANCELLED")
                state = self._select_endpoint(
                    excluded=frozenset(attempted_endpoints)
                )
                if state is None:
                    break
                attempted_endpoints.add(state.base_url)
                attempt_started = self._clock()
                try:
                    value = self._consume_stream_attempt(
                        state=state,
                        method=method,
                        path=path,
                        payload=payload,
                        headers=headers,
                        consumer=consumer,
                        cancellation=cancellation,
                    )
                except _RetryableStreamError as error:
                    last_reason = error.reason
                    self._record_failure(
                        state,
                        self._clock() - attempt_started,
                    )
                    continue
                except (
                    ExternalRequestRejectedError,
                    StreamCancelledError,
                ):
                    self._record_rejection(
                        state,
                        self._clock() - attempt_started,
                    )
                    raise
                except ExternalStreamInterruptedError:
                    self._record_failure(
                        state,
                        self._clock() - attempt_started,
                    )
                    raise
                except Exception:
                    self._record_rejection(
                        state,
                        self._clock() - attempt_started,
                    )
                    raise
                self._record_success(
                    state,
                    self._clock() - attempt_started,
                )
                return HttpStreamResponse(
                    endpoint=state.base_url,
                    value=value,
                    retry_count=attempt_index,
                    elapsed_seconds=self._clock() - started,
                )
        raise ExternalServiceUnavailableError(
            f"外部依赖不可用：{last_reason}。"
        )

    def _consume_stream_attempt(  # noqa: PLR0913
        self,
        *,
        state: _EndpointState,
        method: str,
        path: str,
        payload: object | None,
        headers: Mapping[str, str] | None,
        consumer: Callable[[HttpStreamAttempt], _StreamValue],
        cancellation: StreamCancellation,
    ) -> _StreamValue:
        """消费一个端点；仅把首 delta 前瞬时失败标记为可重试。"""
        stream_attempt: HttpStreamAttempt | None = None
        try:
            with self._client.stream(
                method,
                state.base_url + path,
                json=payload,
                headers=headers,
            ) as response:
                registration = cancellation.register(response.close)
                try:
                    _raise_if_stream_cancelled(cancellation)
                    _validate_stream_status(response)
                    stream_attempt = HttpStreamAttempt(response)
                    return consumer(stream_attempt)
                finally:
                    cancellation.unregister(registration)
        except httpx.HTTPError as error:
            if cancellation.is_cancelled():
                raise StreamCancelledError(
                    "LLM_STREAM_CANCELLED"
                ) from error
            if (
                stream_attempt is not None
                and stream_attempt.content_delta_received
            ):
                raise ExternalStreamInterruptedError(
                    "LLM_STREAM_INTERRUPTED"
                ) from error
            raise _RetryableStreamError("HTTP_TRANSPORT") from error
        except (OverflowError, TypeError, ValueError) as error:
            raise ExternalRequestRejectedError(
                "外部流返回不可用内容：INVALID_STREAM_SCHEMA。"
            ) from error

    def _select_endpoint(
        self,
        *,
        excluded: frozenset[str] = frozenset(),
    ) -> _EndpointState | None:
        now = self._clock()
        with self._lock:
            candidates = [
                (index, state)
                for index, state in enumerate(self._states)
                if (
                    state.circuit_open_until <= now
                    and state.base_url not in excluded
                )
            ]
            if not candidates:
                return None
            minimum_in_flight = min(
                state.in_flight for _, state in candidates
            )
            least_busy = [
                (index, state)
                for index, state in candidates
                if state.in_flight == minimum_in_flight
            ]
            minimum_latency = min(
                state.ewma_latency_seconds or 0.0
                for _, state in least_busy
            )
            fastest = [
                (index, state)
                for index, state in least_busy
                if (state.ewma_latency_seconds or 0.0) == minimum_latency
            ]
            index, state = min(
                fastest,
                key=lambda item: (
                    item[0] - self._next_index
                ) % len(self._states),
            )
            state.in_flight += 1
            self._next_index = (index + 1) % len(self._states)
            return state
        return None

    def _record_failure(
        self,
        state: _EndpointState,
        elapsed_seconds: float,
    ) -> None:
        with self._lock:
            self._finish_attempt(state, elapsed_seconds)
            state.consecutive_failures += 1
            if (
                state.consecutive_failures
                >= self._policy.failure_threshold
            ):
                state.circuit_open_until = (
                    self._clock() + self._policy.cooldown_seconds
                )

    def _record_success(
        self,
        state: _EndpointState,
        elapsed_seconds: float,
    ) -> None:
        with self._lock:
            self._finish_attempt(state, elapsed_seconds)
            state.consecutive_failures = 0
            state.circuit_open_until = 0.0

    def _record_rejection(
        self,
        state: _EndpointState,
        elapsed_seconds: float,
    ) -> None:
        with self._lock:
            self._finish_attempt(state, elapsed_seconds)

    @staticmethod
    def _finish_attempt(
        state: _EndpointState,
        elapsed_seconds: float,
    ) -> None:
        state.in_flight -= 1
        previous = state.ewma_latency_seconds
        state.ewma_latency_seconds = (
            elapsed_seconds
            if previous is None
            else (0.2 * elapsed_seconds) + (0.8 * previous)
        )


def _is_transient_status(status_code: int) -> bool:
    """返回状态码是否允许在有限预算内切换端点。"""
    return (
        status_code in _TRANSIENT_STATUSES
        or _SERVER_ERROR_MIN <= status_code <= _SERVER_ERROR_MAX
    )


def _raise_if_stream_cancelled(
    cancellation: StreamCancellation,
) -> None:
    """在调用方取消后以稳定类别终止上游流。"""
    if cancellation.is_cancelled():
        raise StreamCancelledError("LLM_STREAM_CANCELLED")


def _validate_stream_status(response: httpx.Response) -> None:
    """把 HTTP 流状态分类为成功、瞬时失败或不可重试拒绝。"""
    if _is_transient_status(response.status_code):
        raise _RetryableStreamError(f"HTTP_{response.status_code}")
    if response.is_error or response.is_redirect:
        raise ExternalRequestRejectedError(
            f"外部流请求被拒绝：HTTP_{response.status_code}。"
        )


def _normalize_endpoint(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "外部端点必须是无路径、query 和 fragment 的 http(s) URL。"
        )
    return normalized


def _close_quietly(closer: Callable[[], None]) -> bool:
    """尽力关闭取消资源，失败时允许继续关闭其余上游。"""
    try:
        closer()
    except Exception:
        return False
    return True
