"""Product HTTP 的有界内存限流器。"""

from __future__ import annotations

import time
from collections.abc import Callable
from ipaddress import ip_address, ip_network
from threading import Lock

from fastapi import Request

_LOOPBACK_NAMES = frozenset({"localhost", "testclient", "testserver"})
_DEMO_PRIVATE_NETWORKS = tuple(
    ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)


class RequestRateLimiter:
    """按客户端与操作维护固定窗口调用计数。"""

    def __init__(
        self,
        *,
        window_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建进程内限流器。

        Args:
            window_seconds: 固定窗口秒数。
            clock: 可测试的单调时钟。

        Returns:
            无返回值。

        """
        if window_seconds <= 0:
            raise ValueError("限流窗口必须为正数。")
        self._window_seconds = window_seconds
        self._clock = clock
        self._calls: dict[tuple[str, str], list[float]] = {}
        self._lock = Lock()

    def allow(self, client: str, operation: str, *, limit: int) -> bool:
        """原子判断并记录一次调用。

        Args:
            client: 不持久化的客户端地址。
            operation: 固定操作类别。
            limit: 窗口内最大调用数。

        Returns:
            本次调用是否允许。

        """
        if limit <= 0:
            return False
        now = self._clock()
        key = (client, operation)
        with self._lock:
            calls = [
                value
                for value in self._calls.get(key, [])
                if now - value < self._window_seconds
            ]
            if len(calls) >= limit:
                self._calls[key] = calls
                return False
            calls.append(now)
            self._calls[key] = calls
            return True


def is_loopback(hostname: str | None) -> bool:
    """判断主机名或 IP 是否为本机回环地址。"""
    if hostname is None:
        return False
    if hostname.casefold() in _LOOPBACK_NAMES:
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def effective_request_scheme(
    request: Request,
    *,
    trusted_proxies: frozenset[str],
) -> str:
    """只信任精确代理来源提供的转发协议。"""
    peer = request.client.host if request.client else ""
    if peer in trusted_proxies:
        forwarded = request.headers.get("X-Forwarded-Proto", "")
        if forwarded in {"http", "https"}:
            return forwarded
    return request.url.scheme


def demo_http_request_allowed(
    request: Request,
    *,
    enabled: bool,
    trusted_origins: tuple[str, ...],
    trusted_proxies: frozenset[str],
) -> bool:
    """判断请求是否满足湾事通私网明文演示的完整边界。"""
    if not enabled:
        return False
    if (
        effective_request_scheme(
            request, trusted_proxies=trusted_proxies
        )
        != "http"
    ):
        return False
    allowed_origins = frozenset(
        origin.rstrip("/") for origin in trusted_origins
    )
    host = request.headers.get("Host", "")
    if not host or f"http://{host}" not in allowed_origins:
        return False
    origin = request.headers.get("Origin")
    if origin is not None and origin.rstrip("/") not in allowed_origins:
        return False
    peer = request.client.host if request.client else ""
    return (
        is_loopback(peer)
        or peer in trusted_proxies
        or _is_demo_private_address(peer)
    )


def secure_cookie_for_request(
    request: Request,
    *,
    trusted_proxies: frozenset[str],
) -> bool:
    """按已校验的有效协议统一决定 Cookie 的 Secure 属性。"""
    return (
        effective_request_scheme(
            request, trusted_proxies=trusted_proxies
        )
        == "https"
    )


def _is_demo_private_address(value: str) -> bool:
    try:
        address = ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in _DEMO_PRIVATE_NETWORKS)


__all__ = [
    "RequestRateLimiter",
    "demo_http_request_allowed",
    "effective_request_scheme",
    "is_loopback",
    "secure_cookie_for_request",
]
