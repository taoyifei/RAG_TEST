"""OpenAI-compatible 连接的非 Secret 协议配置。"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit, urlunsplit

OPENAI_COMPATIBLE_PROVIDER = "openai-compatible"
RERANK_PROTOCOLS = frozenset({"tei", "jina-compatible"})
_MAX_BASE_URL_LENGTH = 2048
_MAX_RERANK_PATH_LENGTH = 300


def normalize_base_url(value: str | None) -> str:
    """规范化兼容服务基础 URL，传输层另行决定是否允许 HTTP。

    Args:
        value: 页面输入的基础 URL，不得包含凭据、查询参数或片段。

    Returns:
        去除末尾斜杠、规范化 scheme 和主机大小写的 URL。

    Raises:
        ValueError: URL 不是完整 HTTP(S) 地址或混入了禁止部分。

    """
    raw = "" if value is None else value.strip()
    if not raw or len(raw) > _MAX_BASE_URL_LENGTH:
        raise ValueError("兼容服务 Base URL 必须为 1 到 2048 个字符。")
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("兼容服务 Base URL 端口无效。") from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "兼容服务 Base URL 必须是无凭据、query 和 fragment 的 HTTP(S) URL。"
        )
    hostname = parsed.hostname.casefold()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    authority = hostname if port is None else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.casefold(), authority, path, "", ""))


def normalize_rerank_protocol(
    value: str | None,
) -> Literal["tei", "jina-compatible"]:
    """验证两个明确支持的 Reranker 请求协议。"""
    protocol = "jina-compatible" if value is None else value.strip()
    if protocol == "tei":
        return "tei"
    if protocol == "jina-compatible":
        return "jina-compatible"
    raise ValueError("Reranker 协议只支持 tei 或 jina-compatible。")


def default_rerank_path(protocol: str) -> str:
    """返回所选协议的可编辑默认路径。"""
    return "/rerank" if protocol == "tei" else "/v1/rerank"


def normalize_rerank_path(value: str | None, protocol: str) -> str:
    """规范化同源 Reranker 相对路径，不接受第二个 URL。"""
    path = default_rerank_path(protocol) if value is None else value.strip()
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or len(path) > _MAX_RERANK_PATH_LENGTH
    ):
        raise ValueError("Reranker Path 必须是 1 到 300 个字符的同源绝对路径。")
    return path


__all__ = [
    "OPENAI_COMPATIBLE_PROVIDER",
    "RERANK_PROTOCOLS",
    "default_rerank_path",
    "normalize_base_url",
    "normalize_rerank_path",
    "normalize_rerank_protocol",
]
