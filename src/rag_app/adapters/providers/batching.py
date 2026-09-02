"""Provider 输入的保守 Token 估算与稳定分批。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rag_app.core.errors import ProviderInputTooLarge


@dataclass(frozen=True, slots=True)
class BatchLimits:
    """单批条目、Token 和字符安全上限。"""

    max_items: int = 16
    max_total_estimated_tokens: int = 8192
    max_total_chars: int = 131072
    max_input_tokens: int = 32768


def estimate_tokens(text: str) -> int:
    """以 Unicode 字符数给出保守 Token 上界。

    Args:
        text: 待估算文本。

    Returns:
        至少为 1 的保守 Token 数。

    """
    return max(1, len(text))


def batch_texts(
    texts: Sequence[str], limits: BatchLimits
) -> tuple[tuple[str, ...], ...]:
    """在不截断输入的前提下稳定分批。

    Args:
        texts: 保持身份顺序的非空文本。
        limits: 固定安全上限。

    Returns:
        合并后与输入严格相同的批次。

    Raises:
        ProviderInputTooLarge: 单条输入超过本地上限。
        ValueError: 输入或限制无效。

    """
    if min(
        limits.max_items,
        limits.max_total_estimated_tokens,
        limits.max_total_chars,
        limits.max_input_tokens,
    ) <= 0:
        raise ValueError("Provider batch limits 必须为正数。")
    if not texts or any(not text.strip() for text in texts):
        raise ValueError("Provider batch 输入必须是非空文本。")
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    current_tokens = 0
    current_chars = 0
    for text in texts:
        token_count = estimate_tokens(text)
        if token_count > limits.max_input_tokens:
            raise ProviderInputTooLarge(
                "单条 Provider 输入超过本地 Token 上限。",
                stage="provider.batch",
                details={"estimated_tokens": token_count},
            )
        would_overflow = current and (
            len(current) >= limits.max_items
            or current_tokens + token_count
            > limits.max_total_estimated_tokens
            or current_chars + len(text) > limits.max_total_chars
        )
        if would_overflow:
            batches.append(tuple(current))
            current = []
            current_tokens = 0
            current_chars = 0
        if (
            token_count > limits.max_total_estimated_tokens
            or len(text) > limits.max_total_chars
        ):
            raise ProviderInputTooLarge(
                "单条 Provider 输入无法装入安全批次。",
                stage="provider.batch",
                details={"estimated_tokens": token_count},
            )
        current.append(text)
        current_tokens += token_count
        current_chars += len(text)
    if current:
        batches.append(tuple(current))
    return tuple(batches)
