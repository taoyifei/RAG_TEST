"""跨 Provider 与 Router 共享的保守 Token 估算。"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """按 Unicode code point 给出稳定的保守 Token 上界。

    Args:
        text: 待估算文本。

    Returns:
        至少为 1 的保守 Token 数。

    """
    return max(1, sum(1 for _ in text))


__all__ = ["estimate_tokens"]
