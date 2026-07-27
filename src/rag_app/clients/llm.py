"""Qwen OpenAI 兼容端点的非流式缓冲生成客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rag_app.clients.model_services import ExternalCallAudit
from rag_app.clients.resilience import ResilientHttpPool

__all__ = [
    "BufferedLlmClient",
    "ChatMessage",
    "LlmGeneration",
    "TokenUsage",
]

ChatRole = Literal["system", "user", "assistant"]


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
class LlmGeneration:
    """尚未通过引用校验、不得直接发布的完整生成结果。"""

    content: str
    model: str
    usage: TokenUsage
    call: ExternalCallAudit


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
        response_format: dict[str, object] | None = None,
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


def _parse_generation(
    payload: object,
    *,
    expected_model: str,
) -> tuple[str, str, TokenUsage]:
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


def _parse_usage(raw_usage: object) -> TokenUsage:
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


def _authorization_headers(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
