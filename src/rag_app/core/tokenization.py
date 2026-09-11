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


def estimate_provider_input_tokens(text: str) -> int:
    """估算远程 Provider 的可计费输入 Token。

    以三个 UTF-8 字节计一个 Token，兼顾中文单字和英文子词；发送后的
    Provider usage 仍是最终累计依据。该估算用于真实资料预算，不替代
    Adapter 的输入长度安全检查。

    Args:
        text: 将发送给远程 Provider 的单段文本。

    Returns:
        至少为 1 的有界输入 Token 估算。

    """
    byte_count = len(text.encode("utf-8"))
    return max(1, (byte_count + 2) // 3)


__all__ = ["estimate_provider_input_tokens", "estimate_tokens"]
