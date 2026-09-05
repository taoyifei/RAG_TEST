"""远程 Provider 共用的同步、有限重试 HTTP 传输。"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from rag_app.core.errors import (
    ProviderAuthenticationError,
    ProviderInputTooLarge,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
    RagError,
)
from rag_app.core.models import ProviderCall, ProviderFailureCategory

_DEFAULT_RETRY_STATUSES = frozenset({408, 429, 502, 503, 504})
_AUTH_OR_MODEL_STATUSES = frozenset({401, 403, 404})
_INPUT_INVALID_STATUSES = frozenset({400, 422})
_HTTP_RATE_LIMITED = 429
_HTTP_SUCCESS_MIN = 200
_HTTP_SUCCESS_MAX = 300
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 600


@dataclass(frozen=True, slots=True)
class ProviderHttpResult:
    """成功 JSON 响应和脱敏调用审计。"""

    payload: object
    call: ProviderCall


class ProviderHttpError(RuntimeError):
    """携带失败分类与脱敏调用审计的传输错误。"""

    def __init__(
        self,
        category: ProviderFailureCategory,
        reason_code: str,
        call: ProviderCall,
    ) -> None:
        """保存可供应用 Router 判定的失败。

        Args:
            category: 失败分类。
            reason_code: 不含正文或凭据的稳定原因码。
            call: 脱敏调用审计。

        Returns:
            无返回值。

        """
        self.category = category
        self.reason_code = reason_code
        self.call = call
        super().__init__(f"PROVIDER_HTTP_FAILURE: {reason_code}")


class ProviderHttpClient:
    """在一个固定 endpoint 上执行同步 JSON 请求。"""

    def __init__(  # noqa: PLR0913
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        max_attempts: int = 3,
        max_response_bytes: int = 4 * 1024 * 1024,
        retry_statuses: frozenset[int] = _DEFAULT_RETRY_STATUSES,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        random_value: Callable[[], float] = random.random,
        observer: Callable[[ProviderCall], None] | None = None,
        defer_success_observation: bool = False,
    ) -> None:
        """冻结 endpoint、连接池和有界重试策略。

        Args:
            base_url: 固定 HTTPS 基础地址，可含受控路径前缀。
            client: 可注入 MockTransport 的长生命周期客户端。
            max_attempts: 包含首次调用的最大尝试次数。
            max_response_bytes: 接受的最大响应字节数。
            retry_statuses: 同一 Provider 内允许重试的状态码。
            sleeper: 可测试的等待函数。
            monotonic: 可测试的耗时钟。
            wall_clock: 解析 HTTP-date Retry-After 的墙上时钟。
            random_value: 返回 ``[0, 1]`` 的 full-jitter 随机源。
            observer: 可选脱敏调用观察器；失败不得覆盖业务结果。
            defer_success_observation: 是否等待响应语义校验后再观察成功。

        Returns:
            无返回值。

        Raises:
            ValueError: endpoint 或限制无效。

        """
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Provider base_url 必须是无凭据和 query 的 HTTPS URL。"
            )
        if max_attempts <= 0 or max_response_bytes <= 0:
            raise ValueError("HTTP 尝试次数和响应上限必须为正数。")
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._retry_statuses = retry_statuses
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._random_value = random_value
        self._observer = observer
        self._defer_success_observation = defer_success_observation
        self._closed = False

    def request_json(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        payload: object,
        headers: Mapping[str, str],
        provider_id: str,
        operation: str,
        model: str,
        input_count: int,
        estimated_tokens: int,
    ) -> ProviderHttpResult:
        """发送 JSON 并严格限制重试、大小和内容类型。

        Args:
            method: HTTP 方法。
            path: 以单斜杠开头的固定相对路径。
            payload: 不会进入错误或审计的 JSON 请求体。
            headers: 不会进入错误或审计的请求头。
            provider_id: 可审计 Provider ID。
            operation: embedding 或 reranking 操作。
            model: 固定模型身份。
            input_count: 本次输入条目数。
            estimated_tokens: 本地保守估算 Token 数。

        Returns:
            JSON payload 和脱敏调用审计。

        Raises:
            ProviderHttpFailure: 传输、HTTP 或响应外壳失败。
            RuntimeError: 客户端已经关闭。
            ValueError: path 不安全。

        """
        if self._closed:
            raise RuntimeError("ProviderHttpClient 已关闭。")
        if not path.startswith("/") or path.startswith("//") or "?" in path:
            raise ValueError("Provider path 必须是无 query 的单斜杠相对路径。")
        started = self._monotonic()
        last_retry_after_ms: int | None = None
        encountered_rate_limit = False
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.request(
                    method,
                    self._base_url + path,
                    json=payload,
                    headers=headers,
                )
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.TimeoutException,
            ):
                call = self._call(
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    "TRANSIENT",
                    "HTTP_TRANSPORT",
                    input_count,
                    estimated_tokens,
                    last_retry_after_ms,
                    encountered_rate_limit,
                )
                if attempt == self._max_attempts:
                    self._observe(call)
                    raise ProviderHttpError(
                        ProviderFailureCategory.TRANSIENT,
                        "HTTP_TRANSPORT",
                        call,
                    ) from None
                self._sleep_before_retry(attempt, None)
                continue
            status = response.status_code
            encountered_rate_limit = (
                encountered_rate_limit or status == _HTTP_RATE_LIMITED
            )
            if status in self._retry_statuses:
                retry_after = _retry_after_seconds(
                    response.headers.get("retry-after"), self._wall_clock()
                )
                last_retry_after_ms = (
                    None if retry_after is None else round(retry_after * 1000)
                )
                call = self._call(
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    "TRANSIENT",
                    f"HTTP_{status}",
                    input_count,
                    estimated_tokens,
                    last_retry_after_ms,
                    encountered_rate_limit,
                )
                if attempt == self._max_attempts:
                    self._observe(call)
                    raise ProviderHttpError(
                        ProviderFailureCategory.TRANSIENT,
                        f"HTTP_{status}",
                        call,
                    )
                self._sleep_before_retry(attempt, retry_after)
                continue
            category = _status_category(status)
            if category is not None:
                call = self._call(
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    category.name,
                    f"HTTP_{status}",
                    input_count,
                    estimated_tokens,
                    None,
                    encountered_rate_limit,
                )
                self._observe(call)
                raise ProviderHttpError(category, f"HTTP_{status}", call)
            content = response.content
            if len(content) > self._max_response_bytes:
                raise self._contract_failure(
                    "RESPONSE_TOO_LARGE",
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    input_count,
                    estimated_tokens,
                    encountered_rate_limit,
                )
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type.casefold():
                raise self._contract_failure(
                    "INVALID_CONTENT_TYPE",
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    input_count,
                    estimated_tokens,
                    encountered_rate_limit,
                )
            try:
                response_payload = response.json()
            except ValueError:
                raise self._contract_failure(
                    "INVALID_JSON",
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    input_count,
                    estimated_tokens,
                    encountered_rate_limit,
                ) from None
            call = self._call(
                provider_id,
                operation,
                model,
                path,
                attempt,
                started,
                "SUCCESS",
                "OK",
                input_count,
                estimated_tokens,
                last_retry_after_ms,
                encountered_rate_limit,
            )
            if not self._defer_success_observation:
                self._observe(call)
            return ProviderHttpResult(payload=response_payload, call=call)
        raise AssertionError("有限尝试循环必须返回或抛出。")

    def complete_call(
        self,
        call: ProviderCall,
        *,
        observed_tokens: int | None = None,
        failure_reason_code: str | None = None,
    ) -> ProviderCall:
        """在响应语义校验后生成并观察唯一终态调用。

        Args:
            call: HTTP 层返回但尚未观察的成功调用。
            observed_tokens: Provider 返回且已严格校验的 Token 数。
            failure_reason_code: 可选的稳定响应合同失败码。

        Returns:
            带最终状态和实际 Token 的脱敏调用。

        """
        values = call.model_dump()
        values["observed_tokens"] = observed_tokens
        if failure_reason_code is not None:
            values["status_category"] = "RESPONSE_CONTRACT"
            values["reason_code"] = failure_reason_code
        completed_call = ProviderCall.model_validate(values)
        if self._defer_success_observation:
            self._observe(completed_call)
        return completed_call

    def close(self) -> None:
        """幂等关闭连接池。

        Args:
            无参数；关闭当前客户端。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self._client.close()

    def _contract_failure(  # noqa: PLR0913, PLR0917
        self,
        reason_code: str,
        provider_id: str,
        operation: str,
        model: str,
        path: str,
        attempt: int,
        started: float,
        input_count: int,
        estimated_tokens: int,
        rate_limited: bool,
    ) -> ProviderHttpError:
        call = self._call(
            provider_id,
            operation,
            model,
            path,
            attempt,
            started,
            "RESPONSE_CONTRACT",
            reason_code,
            input_count,
            estimated_tokens,
            None,
            rate_limited,
        )
        self._observe(call)
        return ProviderHttpError(
            ProviderFailureCategory.RESPONSE_CONTRACT,
            reason_code,
            call,
        )

    def _call(  # noqa: PLR0913, PLR0917
        self,
        provider_id: str,
        operation: str,
        model: str,
        path: str,
        attempt_count: int,
        started: float,
        status_category: str,
        reason_code: str,
        input_count: int,
        estimated_tokens: int,
        retry_after_ms: int | None,
        rate_limited: bool,
    ) -> ProviderCall:
        parsed = urlparse(self._base_url)
        host = parsed.hostname or "invalid"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        endpoint = f"{host}{parsed.path.rstrip('/')}{path}"
        return ProviderCall(
            provider_id=provider_id,
            operation=operation,
            call_count=1,
            retry_count=attempt_count - 1,
            elapsed_ms=max(0, round((self._monotonic() - started) * 1000)),
            reason_code=reason_code,
            model=model,
            endpoint=endpoint,
            attempt_count=attempt_count,
            status_category=status_category,
            retry_after_ms=retry_after_ms,
            rate_limited=rate_limited,
            input_count=input_count,
            estimated_tokens=estimated_tokens,
        )

    def _observe(self, call: ProviderCall) -> None:
        if self._observer is None:
            return
        try:
            self._observer(call)
        except Exception:
            # 可观测持久层故障不能覆盖检索或建索引的业务结果。
            return

    def _sleep_before_retry(
        self, attempt: int, retry_after: float | None
    ) -> None:
        jitter = self._random_value() * min(8.0, 0.25 * (2 ** (attempt - 1)))
        self._sleeper(max(jitter, retry_after or 0.0))


def _status_category(status: int) -> ProviderFailureCategory | None:
    if _HTTP_SUCCESS_MIN <= status < _HTTP_SUCCESS_MAX:
        return None
    if status in _AUTH_OR_MODEL_STATUSES:
        return ProviderFailureCategory.AUTH_OR_MODEL
    if status in _INPUT_INVALID_STATUSES:
        return ProviderFailureCategory.INPUT_INVALID
    if _HTTP_SERVER_ERROR_MIN <= status < _HTTP_SERVER_ERROR_MAX:
        return ProviderFailureCategory.TRANSIENT
    return ProviderFailureCategory.RESPONSE_CONTRACT


def provider_error(failure: ProviderHttpError, *, stage: str) -> RagError:
    """把传输失败映射为稳定 Core 错误并保留脱敏审计。

    Args:
        failure: HTTP 层分类失败。
        stage: 稳定 Provider 阶段名。

    Returns:
        带 ``provider_call`` 审计属性的 Core 错误。

    """
    if failure.category is ProviderFailureCategory.AUTH_OR_MODEL:
        error: RagError = ProviderAuthenticationError(
            "Provider 鉴权或模型身份无效。",
            stage=stage,
            details={"reason_code": failure.reason_code},
        )
    elif failure.category is ProviderFailureCategory.INPUT_INVALID:
        error = ProviderInputTooLarge(
            "Provider 拒绝了调用方输入。",
            stage=stage,
            retryable=False,
            details={"reason_code": failure.reason_code},
        )
    elif failure.category is ProviderFailureCategory.RESPONSE_CONTRACT:
        error = ProviderInvalidResponse(
            "Provider 响应违反 JSON 外壳合同。",
            stage=stage,
            details={"reason_code": failure.reason_code},
        )
    elif failure.reason_code == "HTTP_429":
        error = ProviderRateLimited(
            "Provider 在有限重试后仍限流。",
            stage=stage,
            details={"reason_code": failure.reason_code},
        )
    else:
        error = ProviderUnavailable(
            "Provider 在有限重试后不可用。",
            stage=stage,
            details={"reason_code": failure.reason_code},
        )
    error.provider_call = failure.call
    return error


def invalid_response_error(
    reason_code: str, call: ProviderCall, *, stage: str
) -> ProviderInvalidResponse:
    """构造不携带响应正文的合同错误。

    Args:
        reason_code: 稳定合同失败码。
        call: 已完成 HTTP 调用的脱敏审计。
        stage: Provider 阶段名。

    Returns:
        带 ``provider_call`` 属性的响应错误。

    """
    error = ProviderInvalidResponse(
        "Provider 响应违反数量、索引、维度或数值合同。",
        stage=stage,
        details={"reason_code": reason_code},
    )
    error.provider_call = call
    return error


def _retry_after_seconds(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        seconds = parsed.timestamp() - now
    return max(0.0, seconds)
