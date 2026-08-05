"""Qwen OpenAI 兼容端点的缓冲与严格 SSE 生成客户端。"""

from __future__ import annotations

import codecs
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Literal

from rag_app.clients.model_services import ExternalCallAudit
from rag_app.clients.resilience import (
    HttpStreamAttempt,
    ResilientHttpPool,
    StreamCancellation,
    StreamCancelledError,
)

__all__ = [
    "BufferedLlmClient",
    "ChatMessage",
    "LlmGeneration",
    "LlmStreamMetrics",
    "TokenUsage",
]

ChatRole = Literal["system", "user", "assistant"]
_MAX_STREAM_CONTENT_CHARS = 65_536


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一个不含工具调用的聊天消息。"""

    role: ChatRole
    content: str

    def __post_init__(self) -> None:
        """拒绝空消息。"""
        if not self.content.strip():
            raise ValueError("chat message content 不能为空。")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """模型端返回的 token 使用量。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class LlmStreamMetrics:
    """一次完整 SSE 生成的非敏感流指标。"""

    first_delta_seconds: float | None
    delta_count: int
    finish_reason: str


@dataclass(frozen=True, slots=True)
class LlmGeneration:
    """尚未通过引用校验、不得直接发布的完整生成结果。"""

    content: str
    model: str
    usage: TokenUsage
    call: ExternalCallAudit
    stream: LlmStreamMetrics | None = None


@dataclass(frozen=True, slots=True)
class _ParsedStream:
    """完成 SSE 协议校验后的内部累积结果。"""

    content: str
    usage: TokenUsage
    metrics: LlmStreamMetrics


@dataclass(slots=True)
class _SseAccumulator:
    """保存一次 SSE 的协议状态并隔离逐行分支。"""

    attempt: HttpStreamAttempt
    expected_model: str
    on_delta: Callable[[str], None]
    started: float
    content_parts: list[str] = field(default_factory=list)
    content_char_count: int = 0
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    first_delta_seconds: float | None = None
    delta_count: int = 0
    done: bool = False

    def consume_line(self, line: str) -> None:
        """校验并消费一条完整 SSE 行。"""
        normalized = line.removesuffix("\r")
        if not normalized:
            return
        if self.done or not normalized.startswith("data:"):
            raise ValueError("INVALID_LLM_SSE_LINE")
        data = normalized[5:].removeprefix(" ")
        if data == "[DONE]":
            self.done = True
            return
        try:
            event = json.loads(data)
        except json.JSONDecodeError as error:
            raise ValueError("INVALID_LLM_SSE_JSON") from error
        self._consume_event(event)

    def complete(self) -> _ParsedStream:
        """在 ``[DONE]`` 后构造完整且已校验的流结果。"""
        if not self.done:
            raise ValueError("LLM_STREAM_MISSING_DONE")
        if self.finish_reason != "stop":
            raise ValueError("LLM_FINISH_REASON_INVALID")
        if self.usage is None:
            raise ValueError("LLM_STREAM_MISSING_USAGE")
        content = "".join(self.content_parts)
        if not content.strip():
            raise ValueError("LLM_STREAM_EMPTY_CONTENT")
        return _ParsedStream(
            content=content,
            usage=self.usage,
            metrics=LlmStreamMetrics(
                first_delta_seconds=self.first_delta_seconds,
                delta_count=self.delta_count,
                finish_reason=self.finish_reason,
            ),
        )

    def _consume_event(self, event: object) -> None:
        event_finish, event_usage, content = _parse_sse_event(
            event,
            expected_model=self.expected_model,
        )
        if content:
            self._consume_content(content)
        if event_finish is not None:
            if self.finish_reason is not None:
                raise ValueError("DUPLICATE_LLM_FINISH_REASON")
            self.finish_reason = event_finish
        if event_usage is not None:
            if self.usage is not None:
                raise ValueError("DUPLICATE_LLM_STREAM_USAGE")
            self.usage = event_usage

    def _consume_content(self, content: str) -> None:
        if self.finish_reason is not None:
            raise ValueError("LLM_CONTENT_AFTER_FINISH")
        if (
            self.content_char_count + len(content)
            > _MAX_STREAM_CONTENT_CHARS
        ):
            raise ValueError("LLM_STREAM_CONTENT_TOO_LARGE")
        self.attempt.mark_content_delta()
        if self.first_delta_seconds is None:
            self.first_delta_seconds = max(
                0.0,
                time.monotonic() - self.started,
            )
        self.delta_count += 1
        self.content_char_count += len(content)
        self.content_parts.append(content)
        self.on_delta(content)


class BufferedLlmClient:
    """在 HTTP 完成前可故障转移，完成后不重放的 LLM 客户端。"""

    def __init__(
        self,
        pool: ResilientHttpPool,
        *,
        model: str,
        max_context_tokens: int,
        api_token: str | None,
    ) -> None:
        """冻结模型、上下文上限与鉴权。

        Args:
            pool: 四个 LLM 端点的共享韧性池。
            model: `/v1/models` 已核验的 served model id。
            max_context_tokens: 模型上下文硬上限。
            api_token: 可选内部 Bearer token。

        Raises:
            ValueError: 模型为空或上下文上限不为正数。

        """
        if not model.strip() or max_context_tokens <= 0:
            raise ValueError("LLM model 与上下文上限必须有效。")
        self._pool = pool
        self._model = model
        self._max_context_tokens = max_context_tokens
        self._headers = _authorization_headers(api_token)

    def generate(
        self,
        messages: tuple[ChatMessage, ...],
        *,
        max_output_tokens: int,
        response_format: Mapping[str, object] | None = None,
    ) -> LlmGeneration:
        """生成完整缓冲响应，不向调用方暴露任何中间 token。

        Args:
            messages: 至少一条非空聊天消息。
            max_output_tokens: 正数且小于上下文上限的输出预算。
            response_format: 可选 OpenAI 兼容结构化输出约束。

        Returns:
            必须继续经过业务引用校验的完整结果。

        Raises:
            ValueError: 请求预算或响应 schema/终止原因无效。

        """
        if not messages:
            raise ValueError("LLM messages 不能为空。")
        if not 0 < max_output_tokens < self._max_context_tokens:
            raise ValueError("LLM 输出预算超出上下文范围。")
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_format is not None:
            payload["response_format"] = response_format
        response = self._pool.request_json(
            "POST",
            "/v1/chat/completions",
            payload=payload,
            headers=self._headers,
            validator=partial(
                _validate_generation,
                expected_model=self._model,
            ),
        )
        content, model, usage = _parse_generation(
            response.payload,
            expected_model=self._model,
        )
        return LlmGeneration(
            content=content,
            model=model,
            usage=usage,
            call=ExternalCallAudit(
                endpoint=response.endpoint,
                retry_count=response.retry_count,
                elapsed_seconds=response.elapsed_seconds,
            ),
        )

    def generate_stream(
        self,
        messages: tuple[ChatMessage, ...],
        *,
        max_output_tokens: int,
        on_delta: Callable[[str], None],
        cancellation: StreamCancellation,
        response_format: Mapping[str, object] | None = None,
    ) -> LlmGeneration:
        """消费 OpenAI-compatible SSE 并逐个转发非空 content delta。

        Args:
            messages: 至少一条非空聊天消息。
            max_output_tokens: 小于模型上下文上限的正数输出预算。
            on_delta: 接收未发布模型字符串分片的同步解析回调。
            cancellation: 客户端断开时立即关闭上游流的令牌。
            response_format: 可选 OpenAI-compatible JSON Schema 约束。

        Returns:
            已完成 model、choice、finish reason、usage 校验的生成结果。

        Raises:
            ValueError: 请求预算无效。
            ExternalRequestRejectedError: SSE 或模型响应结构不可用。
            ExternalServiceUnavailableError: 首 delta 前有限端点均失败。
            ExternalStreamInterruptedError: 首 delta 后上游中断。
            StreamCancelledError: 调用方取消当前流。

        """
        if not messages:
            raise ValueError("LLM messages 不能为空。")
        if not 0 < max_output_tokens < self._max_context_tokens:
            raise ValueError("LLM 输出预算超出上下文范围。")
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "temperature": 0,
            "max_tokens": max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_format is not None:
            payload["response_format"] = response_format
        started = time.monotonic()
        response = self._pool.request_stream(
            "POST",
            "/v1/chat/completions",
            payload=payload,
            headers=self._headers,
            cancellation=cancellation,
            max_attempts=2,
            consumer=lambda attempt: _consume_sse(
                attempt,
                expected_model=self._model,
                on_delta=on_delta,
                cancellation=cancellation,
                started=started,
            ),
        )
        parsed = response.value
        return LlmGeneration(
            content=parsed.content,
            model=self._model,
            usage=parsed.usage,
            call=ExternalCallAudit(
                endpoint=response.endpoint,
                retry_count=response.retry_count,
                elapsed_seconds=response.elapsed_seconds,
            ),
            stream=parsed.metrics,
        )


def _parse_generation(
    payload: object,
    *,
    expected_model: str,
) -> tuple[str, str, TokenUsage]:
    """校验单候选 LLM 响应并提取生成结果。

    Args:
        payload: 服务返回的未信任 JSON 值。
        expected_model: 请求时冻结的模型名称。

    Returns:
        非空回答、模型名称和一致的 token 用量。

    Raises:
        ValueError: 响应 schema、模型、结束原因或内容无效。

    """
    if not isinstance(payload, dict):
        raise ValueError("LLM 响应必须是 JSON object。")
    model = payload.get("model")
    choices = payload.get("choices")
    if model != expected_model:
        raise ValueError("LLM 响应模型与请求不一致。")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("LLM 响应必须恰有一个 choice。")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("LLM choice 格式无效。")
    if choice.get("finish_reason") != "stop":
        raise ValueError("LLM finish_reason 不是 stop。")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("LLM choice 缺少 message。")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM message content 为空。")
    usage = _parse_usage(payload.get("usage"))
    return content, expected_model, usage


def _validate_generation(
    payload: object,
    *,
    expected_model: str,
) -> object:
    _parse_generation(payload, expected_model=expected_model)
    return payload


def _parse_usage(raw_usage: object) -> TokenUsage:
    """校验并转换 LLM token 用量。

    Args:
        raw_usage: 响应中的未信任 usage 值。

    Returns:
        三项计数非负且总量一致的 token 用量。

    Raises:
        ValueError: usage 缺失、计数无效或总量不一致。

    """
    if not isinstance(raw_usage, dict):
        raise ValueError("LLM 响应缺少 usage。")
    values: list[int] = []
    for field_name in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    ):
        value = raw_usage.get(field_name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError("LLM usage 格式无效。")
        values.append(value)
    usage = TokenUsage(*values)
    if usage.total_tokens != usage.prompt_tokens + usage.completion_tokens:
        raise ValueError("LLM usage token 总数不一致。")
    return usage


def _consume_sse(
    attempt: HttpStreamAttempt,
    *,
    expected_model: str,
    on_delta: Callable[[str], None],
    cancellation: StreamCancellation,
    started: float,
) -> _ParsedStream:
    """严格解码一次 OpenAI-compatible SSE 响应。

    Args:
        attempt: 已被韧性池选定且可标记首 content 的字节流。
        expected_model: 请求时冻结的 served model id。
        on_delta: 每个非空 content delta 的进程内解析回调。
        cancellation: 跨线程取消令牌。
        started: 逻辑模型调用开始的单调时点。

    Returns:
        完整模型 JSON 字符串、最终 usage 与流指标。

    Raises:
        ValueError: SSE 行、UTF-8、chunk schema 或结束条件无效。
        StreamCancelledError: 调用方取消当前流。

    """
    accumulator = _SseAccumulator(
        attempt=attempt,
        expected_model=expected_model,
        on_delta=on_delta,
        started=started,
    )
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    pending = ""
    for chunk in attempt.iter_bytes():
        if cancellation.is_cancelled():
            raise StreamCancelledError("LLM_STREAM_CANCELLED")
        pending += decoder.decode(chunk, final=False)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            accumulator.consume_line(line)
    pending += decoder.decode(b"", final=True)
    if pending:
        accumulator.consume_line(pending)
    return accumulator.complete()


def _parse_sse_event(
    payload: object,
    *,
    expected_model: str,
) -> tuple[str | None, TokenUsage | None, str]:
    """校验单个 SSE JSON chunk 的模型、choice、delta 和 usage。"""
    if not isinstance(payload, dict) or payload.get("model") != expected_model:
        raise ValueError("LLM_STREAM_MODEL_MISMATCH")
    choices = payload.get("choices")
    if not isinstance(choices, list):
        raise ValueError("INVALID_LLM_STREAM_CHOICES")
    raw_usage = payload.get("usage")
    parsed_usage = None if raw_usage is None else _parse_usage(raw_usage)
    if not choices:
        if parsed_usage is None:
            raise ValueError("EMPTY_LLM_STREAM_CHOICES")
        return None, parsed_usage, ""
    if len(choices) != 1 or parsed_usage is not None:
        raise ValueError("INVALID_LLM_STREAM_CHOICES")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("index") != 0:
        raise ValueError("INVALID_LLM_STREAM_CHOICE")
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ValueError("INVALID_LLM_FINISH_REASON")
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        raise ValueError("INVALID_LLM_STREAM_DELTA")
    content = delta.get("content", "")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ValueError("INVALID_LLM_STREAM_CONTENT")
    return finish_reason, None, content


def _authorization_headers(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
