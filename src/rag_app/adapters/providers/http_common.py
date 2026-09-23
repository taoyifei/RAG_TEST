"""远程 Provider 共用的同步、有限重试 HTTP 传输。"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, Generic, TypedDict, TypeVar
from urllib.parse import urlparse

import httpx

from rag_app.adapters.providers.budget_transport import budgeted_client
from rag_app.adapters.providers.private_http_diagnostics import (
    PrivateHttpDiagnostic,
    PrivateProviderDiagnosticRecorder,
)
from rag_app.adapters.providers.transport_diagnostics import (
    retryable_transport,
    transport_diagnostics,
)
from rag_app.core.errors import (
    ProviderAuthenticationError,
    ProviderInputTooLarge,
    ProviderInvalidResponse,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderRequestRejected,
    ProviderUnavailable,
    QueryCancelled,
    RagError,
)
from rag_app.core.models import ProviderCall, ProviderFailureCategory
from rag_app.core.models.common import freeze_json_object
from rag_app.core.ports import CancellationPort

_DEFAULT_RETRY_STATUSES = frozenset({408, 429, 502, 503, 504})


class _RequestTimeoutOptions(TypedDict, total=False):
    """仅显式配置时传递请求超时，保留 Client 的默认时限。"""

    timeout: httpx.Timeout


_AUTH_OR_MODEL_STATUSES = frozenset({401, 403, 404})
_INPUT_INVALID_STATUSES = frozenset({400, 422})
_HTTP_RATE_LIMITED = 429
_HTTP_SUCCESS_MIN = 200
_HTTP_SUCCESS_MAX = 300
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 600
_MAX_ERROR_RESPONSE_BYTES = 64 * 1024
_MAX_PRIVATE_STREAM_BYTES = 64 * 1024
_SAFE_ERROR_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,80}$")
_SAFE_CONTENT_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$"
)
_SAFE_REQUEST_DIAGNOSTIC_KEYS = frozenset(
    {
        "grammar_backend_fingerprint",
        "capability_profile_sha256",
        "output_budget",
        "preflight_estimated_tokens",
        "purpose",
        "request_label",
        "schema_family",
        "schema_revision",
        "schema_sha256",
    }
)
_CONTEXT_CAPACITY_REASON_CODES = frozenset(
    {
        "context_length_exceeded",
        "input_too_long",
        "max_context_length_exceeded",
        "prompt_too_long",
    }
)
_StreamValue = TypeVar("_StreamValue")


@dataclass(frozen=True, slots=True)
class ProviderHttpResult:
    """成功 JSON 响应和脱敏调用审计。"""

    payload: object
    call: ProviderCall


@dataclass(frozen=True, slots=True)
class ProviderHttpStreamResult(Generic[_StreamValue]):
    """完成消费后的类型化流结果和脱敏调用审计。"""

    value: _StreamValue
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


def _bounded_stream_bytes(
    chunks: Iterator[bytes],
    max_bytes: int,
) -> Iterator[bytes]:
    """限制一个 Provider 流的累计原始字节，不能靠小事件绕过上限。"""
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise ProviderInvalidResponse(
                "Provider 流式响应超过大小上限。",
                stage="provider.http.stream",
                code="RESPONSE_TOO_LARGE",
            )
        yield chunk


def _capture_private_stream_bytes(
    chunks: Iterator[bytes], captured: bytearray
) -> Iterator[bytes]:
    """向消费方原样透传，同时只留有界私有响应字节。"""
    for chunk in chunks:
        remaining = _MAX_PRIVATE_STREAM_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        yield chunk


_MAX_REQUEST_TIMEOUT_SECONDS = 30.0


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
        response_error_code: (
            Callable[[httpx.Response], str | None] | None
        ) = None,
        allow_http: bool = False,
        use_budget_transport: bool = True,
        private_diagnostic_recorder: (
            PrivateProviderDiagnosticRecorder | None
        ) = None,
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
            response_error_code: 可选的 Provider 安全错误码解析器。
            allow_http: 是否允许 Demo 内网兼容端点使用明文 HTTP。
            use_budget_transport: 是否安装内置 Provider 活动预算传输。
            private_diagnostic_recorder: 显式启用的有界私有失败记录器。

        Returns:
            无返回值。

        Raises:
            ValueError: endpoint 或限制无效。

        """
        parsed = urlparse(base_url)
        if (
            parsed.scheme
            not in ({"http", "https"} if allow_http else {"https"})
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Provider base_url 必须是无凭据和 query 的受支持 URL。"
            )
        if max_attempts <= 0 or max_response_bytes <= 0:
            raise ValueError("HTTP 尝试次数和响应上限必须为正数。")
        self._base_url = base_url.rstrip("/")
        resolved_client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._client = (
            budgeted_client(resolved_client)
            if use_budget_transport
            else resolved_client
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
        self._response_error_code = response_error_code
        self._private_diagnostic_recorder = private_diagnostic_recorder
        self._closed = False

    @property
    def request_timeout_seconds(self) -> float:
        """读取现有 HTTP 时限，补充调用不得扩展首次调用的时限。"""
        configured = self._client.timeout.read
        return (
            min(_MAX_REQUEST_TIMEOUT_SECONDS, configured)
            if configured is not None
            else _MAX_REQUEST_TIMEOUT_SECONDS
        )

    def request_json(  # noqa: PLR0912, PLR0913, PLR0915
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
        timeout_seconds: float | None = None,
        request_diagnostics: Mapping[str, object] | None = None,
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
            timeout_seconds: 可选的单次 HTTP 请求时限。
            request_diagnostics: 不含正文的协议身份与预算诊断。

        Returns:
            JSON payload 和脱敏调用审计。

        Raises:
            ProviderHttpFailure: 传输、HTTP 或响应外壳失败。
            RuntimeError: 客户端已经关闭。
            ValueError: path 不安全。

        """
        if self._closed:
            raise RuntimeError("ProviderHttpClient 已关闭。")
        if (
            timeout_seconds is not None
            and not 0 < timeout_seconds <= _MAX_REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError("单次 Provider 超时必须位于 0 到 30 秒。")
        if not path.startswith("/") or path.startswith("//") or "?" in path:
            raise ValueError("Provider path 必须是无 query 的单斜杠相对路径。")
        started = self._monotonic()
        request_id = uuid.uuid4().hex
        last_retry_after_ms: int | None = None
        encountered_rate_limit = False
        # 有显式时限的轻量 Planner 只发送一次，避免重试放大整体时延。
        max_attempts = 1 if timeout_seconds is not None else self._max_attempts
        timeout_options: _RequestTimeoutOptions = {}
        if timeout_seconds is not None:
            timeout_options["timeout"] = httpx.Timeout(timeout_seconds)
        for attempt in range(1, max_attempts + 1):
            attempt_started = self._monotonic()
            try:
                response = self._client.request(
                    method,
                    self._base_url + path,
                    json=payload,
                    headers=headers,
                    extensions={
                        "rag_provider_retry_index": attempt - 1,
                        "rag_provider_max_attempts": max_attempts,
                        **(
                            {"rag_chat_operation": operation}
                            if operation
                            in {
                                "generation",
                                "query.interpret",
                                "query.rewrite",
                                "image.ocr",
                            }
                            else {}
                        ),
                    },
                    **timeout_options,
                )
            except httpx.TransportError as error:
                diagnostics, transport_category = self._transport_details(
                    error, attempt_started
                )
                call = self._call(
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    transport_category.name,
                    "HTTP_TRANSPORT",
                    input_count,
                    estimated_tokens,
                    last_retry_after_ms,
                    encountered_rate_limit,
                )
                call = call.model_copy(
                    update={
                        "transport_diagnostics": freeze_json_object(diagnostics)
                    }
                )
                if (
                    transport_category is not ProviderFailureCategory.TRANSIENT
                    or attempt == max_attempts
                ):
                    self._observe(call)
                    raise ProviderHttpError(
                        transport_category,
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
                if attempt == max_attempts:
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
                response_diagnostics, response_body, truncated = (
                    self._error_response_diagnostics(response)
                )
                reason_code = (
                    self._safe_response_error_code(
                        response,
                        response_body,
                        truncated=truncated,
                    )
                    or _diagnostic_error_code(response_diagnostics)
                    or f"HTTP_{status}"
                )
                call = self._call(
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    category.name,
                    reason_code,
                    input_count,
                    estimated_tokens,
                    None,
                    encountered_rate_limit,
                )
                attempt_id = f"{request_id}-{attempt}"
                diagnostics = {
                    "request_id": request_id,
                    "attempt_id": attempt_id,
                    "http_status": status,
                    **_safe_request_diagnostics(request_diagnostics),
                    **response_diagnostics,
                }
                private_status = self._record_private_response(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    operation=operation,
                    method=method,
                    path=path,
                    headers=headers,
                    payload=payload,
                    response=response,
                    response_body=response_body,
                    response_truncated=truncated,
                )
                if private_status is not None:
                    diagnostics["private_diagnostic_status"] = private_status
                call = call.model_copy(
                    update={
                        "transport_diagnostics": freeze_json_object(diagnostics)
                    }
                )
                self._observe(call)
                raise ProviderHttpError(category, reason_code, call)
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
            recorder = self._private_diagnostic_recorder
            if (
                recorder is not None
                and recorder.capture_success
                and operation
                in {"generation", "query.interpret", "query.rewrite"}
            ):
                private_status = self._record_private_response(
                    request_id=request_id,
                    attempt_id=f"{request_id}-{attempt}",
                    operation=operation,
                    method=method,
                    path=path,
                    headers=headers,
                    payload=payload,
                    response=response,
                    response_body=content,
                    response_truncated=False,
                )
                if private_status is not None:
                    call = call.model_copy(
                        update={
                            "transport_diagnostics": freeze_json_object(
                                {
                                    **dict(call.transport_diagnostics),
                                    "private_diagnostic_status": private_status,
                                }
                            )
                        }
                    )
            if not self._defer_success_observation:
                self._observe(call)
            return ProviderHttpResult(payload=response_payload, call=call)
        raise AssertionError("有限尝试循环必须返回或抛出。")

    def request_stream(  # noqa: PLR0912, PLR0913, PLR0915
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
        consumer: Callable[[Iterator[bytes]], _StreamValue],
        cancellation: CancellationPort,
        request_diagnostics: Mapping[str, object] | None = None,
    ) -> ProviderHttpStreamResult[_StreamValue]:
        """发送并在当前调用线程消费一个可取消的有限 SSE 响应。

        只允许在尚未收到响应头时按既有策略重试；开始消费响应后不重放
        生成，以免把两个答案拼接为同一流。

        Args:
            method: HTTP 方法。
            path: 受控相对路径。
            payload: 已由 adapter 构造的固定 JSON 请求。
            headers: 不进入错误与审计的认证请求头。
            provider_id: 可审计 Provider ID。
            operation: 本次生成用途。
            model: 固定模型身份。
            input_count: 有限消息条目数。
            estimated_tokens: 本地输入估算。
            consumer: 在响应作用域内消费原始字节的函数。
            cancellation: 可由 HTTP 断连线程触发的取消令牌。
            request_diagnostics: 不含正文的协议身份与预算诊断。

        Returns:
            consumer 的完整结果与唯一调用审计。

        Raises:
            ProviderHttpError: HTTP、传输或响应合同失败。
            QueryCancelled: 调用方取消且已尝试关闭上游响应。

        """
        if self._closed:
            raise RuntimeError("ProviderHttpClient 已关闭。")
        if not path.startswith("/") or path.startswith("//") or "?" in path:
            raise ValueError("Provider path 必须是无 query 的单斜杠相对路径。")
        started = self._monotonic()
        request_id = uuid.uuid4().hex
        last_retry_after_ms: int | None = None
        encountered_rate_limit = False
        for attempt in range(1, self._max_attempts + 1):
            if cancellation.is_cancelled():
                raise QueryCancelled("PROVIDER_STREAM_CANCELLED")
            attempt_started = self._monotonic()
            response: httpx.Response | None = None
            try:
                stream_context = self._client.stream(
                    method,
                    self._base_url + path,
                    json=payload,
                    headers=headers,
                    extensions={
                        "rag_provider_retry_index": attempt - 1,
                        "rag_provider_max_attempts": self._max_attempts,
                        "rag_chat_operation": operation,
                    },
                )
                with stream_context as response:
                    registration = cancellation.register(response.close)
                    try:
                        if cancellation.is_cancelled():
                            raise QueryCancelled("PROVIDER_STREAM_CANCELLED")
                        status = response.status_code
                        encountered_rate_limit = encountered_rate_limit or (
                            status == _HTTP_RATE_LIMITED
                        )
                        if status in self._retry_statuses:
                            retry_after = _retry_after_seconds(
                                response.headers.get("retry-after"),
                                self._wall_clock(),
                            )
                            last_retry_after_ms = (
                                None
                                if retry_after is None
                                else round(retry_after * 1000)
                            )
                            if attempt < self._max_attempts:
                                self._sleep_before_retry(attempt, retry_after)
                                continue
                        category = _status_category(status)
                        if category is not None:
                            (
                                response_diagnostics,
                                response_body,
                                truncated,
                            ) = self._error_response_diagnostics(response)
                            reason_code = (
                                self._safe_response_error_code(
                                    response,
                                    response_body,
                                    truncated=truncated,
                                )
                                or _diagnostic_error_code(response_diagnostics)
                                or f"HTTP_{status}"
                            )
                            call = self._call(
                                provider_id,
                                operation,
                                model,
                                path,
                                attempt,
                                started,
                                category.name,
                                reason_code,
                                input_count,
                                estimated_tokens,
                                last_retry_after_ms,
                                encountered_rate_limit,
                            )
                            attempt_id = f"{request_id}-{attempt}"
                            diagnostics = {
                                "request_id": request_id,
                                "attempt_id": attempt_id,
                                "http_status": status,
                                **_safe_request_diagnostics(
                                    request_diagnostics
                                ),
                                **response_diagnostics,
                            }
                            private_status = self._record_private_response(
                                request_id=request_id,
                                attempt_id=attempt_id,
                                operation=operation,
                                method=method,
                                path=path,
                                headers=headers,
                                payload=payload,
                                response=response,
                                response_body=response_body,
                                response_truncated=truncated,
                            )
                            if private_status is not None:
                                diagnostics["private_diagnostic_status"] = (
                                    private_status
                                )
                            call = call.model_copy(
                                update={
                                    "transport_diagnostics": freeze_json_object(
                                        diagnostics
                                    )
                                }
                            )
                            self._observe(call)
                            raise ProviderHttpError(category, reason_code, call)
                        content_type = response.headers.get("content-type", "")
                        if "text/event-stream" not in content_type.casefold():
                            raise self._contract_failure(
                                "INVALID_STREAM_CONTENT_TYPE",
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
                            recorder = self._private_diagnostic_recorder
                            capture_success = (
                                recorder is not None
                                and recorder.capture_success
                                and operation in {
                                    "generation",
                                    "query.interpret",
                                    "query.rewrite",
                                }
                            )
                            private_body = bytearray()
                            chunks = _bounded_stream_bytes(
                                response.iter_bytes(),
                                self._max_response_bytes,
                            )
                            if capture_success:
                                chunks = _capture_private_stream_bytes(
                                    chunks, private_body
                                )
                            value = consumer(
                                chunks
                            )
                        except QueryCancelled as error:
                            call = self._call(
                                provider_id,
                                operation,
                                model,
                                path,
                                attempt,
                                started,
                                "CANCELLED",
                                "STREAM_CANCELLED",
                                input_count,
                                estimated_tokens,
                                last_retry_after_ms,
                                encountered_rate_limit,
                            )
                            self._observe(call)
                            error.provider_calls = (
                                *error.provider_calls,
                                call,
                            )
                            raise
                        except RagError as error:
                            call = self._call(
                                provider_id,
                                operation,
                                model,
                                path,
                                attempt,
                                started,
                                "RESPONSE_CONTRACT",
                                error.code,
                                input_count,
                                estimated_tokens,
                                last_retry_after_ms,
                                encountered_rate_limit,
                            )
                            self._observe(call)
                            error.provider_call = call
                            error.provider_calls = (call,)
                            raise
                        except httpx.TransportError as error:
                            if cancellation.is_cancelled():
                                call = self._call(
                                    provider_id,
                                    operation,
                                    model,
                                    path,
                                    attempt,
                                    started,
                                    "CANCELLED",
                                    "STREAM_CANCELLED",
                                    input_count,
                                    estimated_tokens,
                                    last_retry_after_ms,
                                    encountered_rate_limit,
                                )
                                self._observe(call)
                                raise QueryCancelled(
                                    "PROVIDER_STREAM_CANCELLED",
                                    provider_calls=(call,),
                                ) from error
                            diagnostics, category = self._transport_details(
                                error, attempt_started
                            )
                            call = self._call(
                                provider_id,
                                operation,
                                model,
                                path,
                                attempt,
                                started,
                                category.name,
                                "STREAM_INTERRUPTED",
                                input_count,
                                estimated_tokens,
                                last_retry_after_ms,
                                encountered_rate_limit,
                            ).model_copy(
                                update={
                                    "transport_diagnostics": freeze_json_object(
                                        diagnostics
                                    )
                                }
                            )
                            self._observe(call)
                            raise ProviderHttpError(
                                category, "STREAM_INTERRUPTED", call
                            ) from error
                        except (OverflowError, TypeError, ValueError) as error:
                            detail = getattr(error, "reason_code", None)
                            diagnostics = {
                                "contract_exception_type": type(error).__name__
                            }
                            if isinstance(detail, str) and re.fullmatch(
                                r"CHAT_[A-Z0-9_]{4,64}", detail
                            ):
                                diagnostics["contract_detail"] = detail
                            call = self._call(
                                provider_id,
                                operation,
                                model,
                                path,
                                attempt,
                                started,
                                "RESPONSE_CONTRACT",
                                "INVALID_STREAM_SCHEMA",
                                input_count,
                                estimated_tokens,
                                last_retry_after_ms,
                                encountered_rate_limit,
                            ).model_copy(
                                update={
                                    "transport_diagnostics": freeze_json_object(
                                        diagnostics
                                    )
                                }
                            )
                            self._observe(call)
                            raise ProviderHttpError(
                                ProviderFailureCategory.RESPONSE_CONTRACT,
                                "INVALID_STREAM_SCHEMA",
                                call,
                            ) from error
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
                        if capture_success:
                            private_status = self._record_private_response(
                                request_id=request_id,
                                attempt_id=f"{request_id}-{attempt}",
                                operation=operation,
                                method=method,
                                path=path,
                                headers=headers,
                                payload=payload,
                                response=response,
                                response_body=bytes(private_body),
                                response_truncated=(
                                    response.num_bytes_downloaded
                                    > len(private_body)
                                ),
                            )
                            if private_status is not None:
                                diagnostics = {
                                    **dict(call.transport_diagnostics),
                                    "private_diagnostic_status": private_status,
                                }
                                frozen_diagnostics = freeze_json_object(
                                    diagnostics
                                )
                                call_update = {
                                    "transport_diagnostics": frozen_diagnostics
                                }
                                call = call.model_copy(update=call_update)
                        if not self._defer_success_observation:
                            self._observe(call)
                        return ProviderHttpStreamResult(value=value, call=call)
                    finally:
                        cancellation.unregister(registration)
            except QueryCancelled:
                raise
            except ProviderHttpError:
                raise
            except httpx.TransportError as error:
                if cancellation.is_cancelled():
                    call = self._call(
                        provider_id,
                        operation,
                        model,
                        path,
                        attempt,
                        started,
                        "CANCELLED",
                        "STREAM_CANCELLED",
                        input_count,
                        estimated_tokens,
                        last_retry_after_ms,
                        encountered_rate_limit,
                    )
                    self._observe(call)
                    raise QueryCancelled(
                        "PROVIDER_STREAM_CANCELLED",
                        provider_calls=(call,),
                    ) from error
                diagnostics, category = self._transport_details(
                    error, attempt_started
                )
                call = self._call(
                    provider_id,
                    operation,
                    model,
                    path,
                    attempt,
                    started,
                    category.name,
                    "HTTP_TRANSPORT",
                    input_count,
                    estimated_tokens,
                    last_retry_after_ms,
                    encountered_rate_limit,
                ).model_copy(
                    update={
                        "transport_diagnostics": freeze_json_object(diagnostics)
                    }
                )
                if (
                    category is not ProviderFailureCategory.TRANSIENT
                    or attempt == self._max_attempts
                ):
                    self._observe(call)
                    raise ProviderHttpError(
                        category, "HTTP_TRANSPORT", call
                    ) from None
                self._sleep_before_retry(attempt, None)
                continue
        raise AssertionError("有限流式尝试必须返回或抛出。")

    def _transport_details(
        self, error: httpx.TransportError, started: float
    ) -> tuple[dict[str, Any], ProviderFailureCategory]:
        diagnostics = transport_diagnostics(
            error,
            elapsed_ms=round((self._monotonic() - started) * 1000),
            extensions=error.request.extensions,
        )
        category = (
            ProviderFailureCategory.TRANSIENT
            if retryable_transport(error, diagnostics)
            else ProviderFailureCategory.AUTH_OR_MODEL
        )
        return diagnostics, category

    def _safe_response_error_code(
        self,
        response: httpx.Response,
        content: bytes,
        *,
        truncated: bool,
    ) -> str | None:
        """只把完整、有界错误外壳交给受信解析器。"""
        resolver = self._response_error_code
        if resolver is None or truncated:
            return None
        try:
            if len(content) > _MAX_ERROR_RESPONSE_BYTES:
                return None
            bounded = httpx.Response(
                response.status_code,
                headers=response.headers,
                content=content,
            )
            return resolver(bounded)
        except (httpx.HTTPError, TypeError, ValueError):
            return None

    def _error_response_diagnostics(
        self, response: httpx.Response
    ) -> tuple[dict[str, object], bytes, bool]:
        """读取至多 64 KiB，并只返回可进入普通 Trace 的结构字段。"""
        buffered = bytearray()
        truncated = False
        try:
            chunks = (
                iter((response.content,))
                if response.is_stream_consumed
                else response.iter_bytes()
            )
            for chunk in chunks:
                remaining = _MAX_ERROR_RESPONSE_BYTES + 1 - len(buffered)
                if remaining <= 0:
                    truncated = True
                    break
                buffered.extend(chunk[:remaining])
                if (
                    len(chunk) > remaining
                    or len(buffered) > _MAX_ERROR_RESPONSE_BYTES
                ):
                    truncated = True
                    break
        except httpx.HTTPError:
            return (
                {
                    "response_body_bytes": 0,
                    "response_body_sha256": hashlib.sha256(b"").hexdigest(),
                    "response_body_hash_scope": "unavailable",
                    "response_body_truncated": True,
                },
                b"",
                True,
            )
        content = bytes(buffered[:_MAX_ERROR_RESPONSE_BYTES])
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        diagnostics: dict[str, object] = {
            "response_body_bytes": len(content),
            "response_body_sha256": hashlib.sha256(content).hexdigest(),
            "response_body_hash_scope": "prefix" if truncated else "full",
            "response_body_truncated": truncated,
        }
        if _SAFE_CONTENT_TYPE.fullmatch(content_type):
            diagnostics["response_content_type"] = content_type
        diagnostics.update(_safe_error_fields(content, truncated=truncated))
        return diagnostics, content, truncated

    def _record_private_response(  # noqa: PLR0913
        self,
        *,
        request_id: str,
        attempt_id: str,
        operation: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        payload: object,
        response: httpx.Response,
        response_body: bytes,
        response_truncated: bool,
    ) -> str | None:
        """私有记录只返回安全状态，绝不覆盖原始 HTTP 结果。"""
        recorder = self._private_diagnostic_recorder
        if recorder is None:
            return None
        try:
            recorder.record(
                PrivateHttpDiagnostic(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    operation=operation,
                    method=method,
                    endpoint=self._base_url + path,
                    request_headers=headers,
                    request_payload=payload,
                    response_status=response.status_code,
                    response_headers=dict(response.headers),
                    response_body=response_body,
                    response_truncated=response_truncated,
                )
            )
        except Exception:
            return "WRITE_FAILED"
        return "WRITTEN"

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

    def record_private_response_contract_failure(
        self,
        *,
        operation: str,
        path: str,
        request_payload: object,
        response_content: str,
        reason_code: str,
    ) -> str | None:
        """在显式私有诊断中保存 HTTP 200 后的业务合同失败。

        Args:
            operation: 当前 Provider 操作。
            path: 已发送的固定相对路径。
            request_payload: 实际请求 body；仅写入私有目录。
            response_content: 模型返回的原始 ``message.content``。
            reason_code: 本地业务合同拒绝原因。

        Returns:
            未启用时返回空，否则返回 ``WRITTEN`` 或 ``WRITE_FAILED``。

        """
        recorder = self._private_diagnostic_recorder
        if recorder is None:
            return None
        request_id = uuid.uuid4().hex
        attempt_id = f"{request_id}-response-contract"
        try:
            recorder.record(
                PrivateHttpDiagnostic(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    operation=operation,
                    method="POST",
                    endpoint=self._base_url + path,
                    request_headers={},
                    request_payload={
                        "request": request_payload,
                        "response_contract_failure": reason_code,
                    },
                    response_status=200,
                    response_headers={"content-type": "application/json"},
                    response_body=response_content.encode("utf-8"),
                    response_truncated=False,
                )
            )
        except Exception:
            return "WRITE_FAILED"
        return "WRITTEN"

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


def _safe_request_diagnostics(
    diagnostics: Mapping[str, object] | None,
) -> dict[str, object]:
    """只允许固定协议身份和整数预算进入普通 Trace。"""
    if diagnostics is None:
        return {}
    safe: dict[str, object] = {}
    for key, value in diagnostics.items():
        if key not in _SAFE_REQUEST_DIAGNOSTIC_KEYS:
            continue
        if isinstance(value, bool):
            continue
        if (isinstance(value, int) and 0 <= value <= (1 << 31) - 1) or (
            isinstance(value, str) and _SAFE_ERROR_VALUE.fullmatch(value)
        ):
            safe[key] = value
    return safe


def _safe_error_fields(content: bytes, *, truncated: bool) -> dict[str, str]:
    """从完整 JSON 错误外壳提取有限 type/code/param，不复制 message。"""
    if truncated or not content:
        return {}
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    error = value.get("error")
    containers = (error, value) if isinstance(error, Mapping) else (value,)
    safe: dict[str, str] = {}
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for source_key, target_key in (
            ("type", "provider_error_type"),
            ("code", "provider_error_code"),
            ("param", "provider_error_param"),
        ):
            item = container.get(source_key)
            if (
                target_key not in safe
                and isinstance(item, str)
                and _SAFE_ERROR_VALUE.fullmatch(item)
            ):
                safe[target_key] = item
    return safe


def _diagnostic_error_code(diagnostics: Mapping[str, object]) -> str | None:
    """优先保留 Provider 明确返回的安全 code，其次保留 error type。"""
    for key in ("provider_error_code", "provider_error_type"):
        value = diagnostics.get(key)
        if isinstance(value, str) and _SAFE_ERROR_VALUE.fullmatch(value):
            return value
    return None


def provider_error(failure: ProviderHttpError, *, stage: str) -> RagError:
    """把传输失败映射为稳定 Core 错误并保留脱敏审计。

    Args:
        failure: HTTP 层分类失败。
        stage: 稳定 Provider 阶段名。

    Returns:
        带 ``provider_call`` 审计属性的 Core 错误。

    """
    if failure.reason_code == "MODEL_FREE_TIER_EXHAUSTED":
        error: RagError = ProviderQuotaExhausted(
            "当前模型的免费调用额度已耗尽。",
            stage=stage,
            details={"reason_code": failure.reason_code},
        )
    elif failure.category is ProviderFailureCategory.AUTH_OR_MODEL:
        error = ProviderAuthenticationError(
            "Provider 鉴权或模型身份无效。",
            stage=stage,
            details={"reason_code": failure.reason_code},
        )
    elif (
        failure.category is ProviderFailureCategory.INPUT_INVALID
        and failure.reason_code.casefold() in _CONTEXT_CAPACITY_REASON_CODES
    ):
        error = ProviderInputTooLarge(
            "Provider 明确报告输入超过上下文容量。",
            stage=stage,
            retryable=False,
            details={"reason_code": failure.reason_code},
        )
    elif failure.category is ProviderFailureCategory.INPUT_INVALID:
        reason_code = (
            "REQUEST_REJECTED_UNKNOWN"
            if failure.reason_code in {"HTTP_400", "HTTP_422"}
            else failure.reason_code
        )
        error = ProviderRequestRejected(
            "Provider 拒绝了请求合同。",
            stage=stage,
            retryable=False,
            details={"reason_code": reason_code},
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
    reason_code: str,
    call: ProviderCall,
    *,
    stage: str,
    diagnostics: Mapping[str, object] | None = None,
) -> ProviderInvalidResponse:
    """构造不携带响应正文的合同错误。

    Args:
        reason_code: 稳定合同失败码。
        call: 已完成 HTTP 调用的脱敏审计。
        stage: Provider 阶段名。
        diagnostics: 可选的无正文协议阶段、字段路径与类型诊断。

    Returns:
        带 ``provider_call`` 属性的响应错误。

    """
    error = ProviderInvalidResponse(
        "Provider 响应违反数量、索引、维度或数值合同。",
        stage=stage,
        details={"reason_code": reason_code, **(diagnostics or {})},
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
