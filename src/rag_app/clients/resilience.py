"""有限重试、并发闸门、健康剔除与熔断。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

__all__ = [
    "ExternalRequestRejectedError",
    "ExternalServiceUnavailableError",
    "HttpJsonResponse",
    "ResiliencePolicy",
    "ResilientHttpPool",
]

_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class ExternalServiceUnavailableError(RuntimeError):
    """所有健康端点均未在有限尝试内成功。"""


class ExternalRequestRejectedError(RuntimeError):
    """端点明确拒绝不可重试请求。"""


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    """一次成功外部调用的非敏感审计信息。"""

    endpoint: str
    payload: object
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
                try:
                    response = self._client.request(
                        method,
                        state.base_url + path,
                        json=payload,
                        headers=headers,
                    )
                except httpx.HTTPError:
                    last_reason = "HTTP_TRANSPORT"
                    self._record_failure(state)
                    continue
                if response.status_code in _TRANSIENT_STATUSES:
                    last_reason = f"HTTP_{response.status_code}"
                    self._record_failure(state)
                    continue
                if response.is_error or response.is_redirect:
                    raise ExternalRequestRejectedError(
                        f"外部请求被拒绝：HTTP_{response.status_code}。"
                    )
                try:
                    response_payload = response.json()
                except ValueError:
                    last_reason = "INVALID_JSON"
                    self._record_failure(state)
                    continue
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
                    except (OverflowError, TypeError, ValueError):
                        last_reason = "INVALID_RESPONSE_SCHEMA"
                        self._record_failure(state)
                        continue
                self._record_success(state)
                return HttpJsonResponse(
                    endpoint=state.base_url,
                    payload=response_payload,
                    retry_count=attempt,
                    elapsed_seconds=self._clock() - started,
                )
        raise ExternalServiceUnavailableError(
            f"外部依赖不可用：{last_reason}。"
        )

    def _select_endpoint(self) -> _EndpointState | None:
        now = self._clock()
        with self._lock:
            for offset in range(len(self._states)):
                index = (self._next_index + offset) % len(self._states)
                state = self._states[index]
                if state.circuit_open_until > now:
                    continue
                self._next_index = (index + 1) % len(self._states)
                return state
        return None

    def _record_failure(self, state: _EndpointState) -> None:
        with self._lock:
            state.consecutive_failures += 1
            if (
                state.consecutive_failures
                >= self._policy.failure_threshold
            ):
                state.circuit_open_until = (
                    self._clock() + self._policy.cooldown_seconds
                )

    def _record_success(self, state: _EndpointState) -> None:
        with self._lock:
            state.consecutive_failures = 0
            state.circuit_open_until = 0.0


def _normalize_endpoint(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("外部端点必须是无 query/fragment 的 http(s) URL。")
    return normalized
