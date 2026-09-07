"""跨 Provider 与 Router 共享的稳定预算估算，不代表实际计费。"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """按 Unicode code point 产生本地估算，不保证实际 Token 上界。

    Args:
        text: 待估算文本。

    Returns:
        至少为 1 的预算估算值；实际用量以 Provider usage 为准。

    """
    return max(1, sum(1 for _ in text))


__all__ = ["estimate_tokens"]
