#!/usr/bin/env python3
"""为私网入口提供 `/kb` 前缀及可选的公共根路径代理。"""

from __future__ import annotations

import argparse
import ipaddress
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn

_HOP_BY_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
_MAX_TCP_PORT = 65535
_PUBLIC_ROOT_EXACT_PATHS = frozenset({b"/", b"/api/public", b"/sso/logout"})
_PUBLIC_ROOT_PREFIXES = (b"/api/public/", b"/assets/")

AsgiMessage = dict[str, Any]
Receive = Callable[[], Awaitable[AsgiMessage]]
Send = Callable[[AsgiMessage], Awaitable[None]]


def _private_or_loopback_ip(value: str) -> str:
    """只接受精确私网或回环 IP。"""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须提供精确 IP 地址。") from None
    if address.is_unspecified or not (
        address.is_private or address.is_loopback
    ):
        raise argparse.ArgumentTypeError("只允许私网或回环 IP。")
    return str(address)


def _upstream_origin(value: str) -> str:
    """校验仅含私网或回环主机的 HTTP origin。"""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("上游端口无效。") from None
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("上游必须是无路径的完整 HTTP origin。")
    _private_or_loopback_ip(parsed.hostname)
    return value.rstrip("/")


def _external_prefix(value: str) -> str:
    """校验单段规范外部前缀。"""
    if (
        not value.startswith("/")
        or value == "/"
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or "%" in value
        or any(part in {"", ".", ".."} for part in value.split("/")[1:])
    ):
        raise argparse.ArgumentTypeError("外部前缀必须是规范的非根绝对路径。")
    return value


def _strip_external_prefix(raw_path: bytes, prefix: bytes) -> bytes | None:
    """只剥离一次完整前缀，拒绝相似或无前缀路径。"""
    if raw_path == prefix:
        return b"/"
    marker = prefix + b"/"
    if raw_path.startswith(marker):
        return raw_path[len(prefix) :]
    return None


def _public_root_path(raw_path: bytes) -> bytes | None:
    """只放行公共页面依赖的根路径，阻断管理员及路径变体。"""
    if (
        b"%" in raw_path
        or b"\\" in raw_path
        or b"//" in raw_path
        or any(part in {b".", b".."} for part in raw_path.split(b"/"))
    ):
        return None
    if raw_path in _PUBLIC_ROOT_EXACT_PATHS or raw_path.startswith(
        _PUBLIC_ROOT_PREFIXES
    ):
        return raw_path
    return None


def _filter_headers(
    headers: Sequence[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """删除逐跳头，保留 Host、Cookie 与重复响应头。"""
    connection_tokens = {
        token.strip().lower()
        for name, value in headers
        if name.lower() == b"connection"
        for token in value.split(b",")
        if token.strip()
    }
    blocked = _HOP_BY_HOP_HEADERS | connection_tokens
    return [
        (name, value) for name, value in headers if name.lower() not in blocked
    ]


class PrefixProxy:
    """把一个精确外部前缀流式代理到回环 HTTP 服务。"""

    def __init__(
        self,
        *,
        upstream_origin: str,
        external_prefix: str,
        public_root_alias: bool = False,
    ) -> None:
        self._upstream_origin = upstream_origin
        self._external_prefix = external_prefix.encode("ascii")
        self._public_root_alias = public_root_alias
        self._timeout = httpx.Timeout(
            connect=5.0,
            read=None,
            write=30.0,
            pool=5.0,
        )

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        """处理单个 ASGI HTTP 请求。"""
        if scope.get("type") != "http":
            await self._send_json(send, 404, {"detail": "not found"})
            return
        raw_path = scope.get("raw_path") or str(scope.get("path", "/")).encode(
            "ascii"
        )
        if bytes(raw_path) == b"/" and not self._public_root_alias:
            await self._send_redirect(send, self._external_prefix + b"/")
            return
        upstream_path = (
            _public_root_path(bytes(raw_path))
            if self._public_root_alias
            else None
        )
        if upstream_path is None:
            upstream_path = _strip_external_prefix(
                bytes(raw_path), self._external_prefix
            )
        if upstream_path is None:
            await self._send_json(send, 404, {"detail": "not found"})
            return
        try:
            path_text = upstream_path.decode("ascii")
            query_text = bytes(scope.get("query_string", b"")).decode("ascii")
        except UnicodeDecodeError:
            await self._send_json(
                send, 400, {"detail": "invalid request target"}
            )
            return
        target = f"{self._upstream_origin}{path_text}"
        if query_text:
            target = f"{target}?{query_text}"
        headers = _filter_headers(scope.get("headers", []))
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            try:
                request = client.build_request(
                    str(scope.get("method", "GET")),
                    target,
                    headers=headers,
                    content=self._request_body(receive),
                )
                response = await client.send(request, stream=True)
            except httpx.RequestError:
                await self._send_json(
                    send, 502, {"detail": "candidate upstream unavailable"}
                )
                return
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": response.status_code,
                        "headers": _filter_headers(response.headers.raw),
                    }
                )
                async for chunk in response.aiter_raw():
                    await send(
                        {
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": True,
                        }
                    )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": False,
                    }
                )
            finally:
                await response.aclose()

    @staticmethod
    async def _request_body(receive: Receive) -> AsyncIterator[bytes]:
        """按 ASGI 消息流转发请求体。"""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            if body:
                yield body
            if not message.get("more_body", False):
                return

    @staticmethod
    async def _send_redirect(send: Send, location: bytes) -> None:
        """把旧根入口临时重定向到规范前缀入口。"""
        await send(
            {
                "type": "http.response.start",
                "status": 307,
                "headers": [
                    (b"location", location),
                    (b"content-length", b"0"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )

    @staticmethod
    async def _send_json(
        send: Send,
        status: int,
        payload: dict[str, str],
    ) -> None:
        """返回不包含内部异常信息的 JSON 错误。"""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把外部路径前缀及可选公共根路径代理到私网候选。"
    )
    parser.add_argument(
        "--listen-host", type=_private_or_loopback_ip, required=True
    )
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument(
        "--upstream-origin", type=_upstream_origin, required=True
    )
    parser.add_argument(
        "--external-prefix", type=_external_prefix, default="/kb"
    )
    parser.add_argument("--public-root-alias", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """校验配置并以前台 Uvicorn 运行，交由容器管理生命周期。"""
    arguments = _parser().parse_args(argv)
    if arguments.listen_port < 1 or arguments.listen_port > _MAX_TCP_PORT:
        raise SystemExit("端口必须位于 1..65535。")
    uvicorn.run(
        PrefixProxy(
            upstream_origin=arguments.upstream_origin,
            external_prefix=arguments.external_prefix,
            public_root_alias=arguments.public_root_alias,
        ),
        host=arguments.listen_host,
        port=arguments.listen_port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
