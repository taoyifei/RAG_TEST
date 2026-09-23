"""私网代理保留 `/kb` 并可为公共问答开放根入口。"""

from __future__ import annotations

import argparse
import asyncio

import httpx
import pytest

from deployment.wanshitong import http_prefix_proxy


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        (b"/kb", b"/"),
        (b"/kb/", b"/"),
        (b"/kb/sso/callback", b"/sso/callback"),
        (b"/kb/api/v1/public/session", b"/api/v1/public/session"),
        (b"/kbx/sso/callback", None),
        (b"/sso/callback", None),
    ],
)
def test_strip_external_prefix(raw_path: bytes, expected: bytes | None) -> None:
    assert (
        http_prefix_proxy._strip_external_prefix(raw_path, b"/kb") == expected
    )


def test_filter_headers_preserves_end_to_end_and_repeated_headers() -> None:
    headers = [
        (b"host", b"10.242.180.54:8289"),
        (b"connection", b"keep-alive, x-remove-me"),
        (b"x-remove-me", b"connection-scoped"),
        (b"set-cookie", b"first=1"),
        (b"set-cookie", b"second=2"),
        (b"transfer-encoding", b"chunked"),
    ]

    assert http_prefix_proxy._filter_headers(headers) == [
        (b"host", b"10.242.180.54:8289"),
        (b"set-cookie", b"first=1"),
        (b"set-cookie", b"second=2"),
    ]


def test_proxy_temporarily_redirects_legacy_root_to_prefix() -> None:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        raise AssertionError("根路径重定向不应读取请求体。")

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    proxy = http_prefix_proxy.PrefixProxy(
        upstream_origin="http://127.0.0.1:8289",
        external_prefix="/kb",
    )
    asyncio.run(
        proxy(
            {"type": "http", "raw_path": b"/"},
            receive,
            send,
        )
    )

    assert messages == [
        {
            "type": "http.response.start",
            "status": 307,
            "headers": [
                (b"location", b"/kb/"),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store"),
            ],
        },
        {
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        },
    ]


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        (b"/", b"/"),
        (b"/api/public/session", b"/api/public/session"),
        (b"/sso/logout", b"/sso/logout"),
        (b"/assets/app.js", b"/assets/app.js"),
        (b"/admin", None),
        (b"/api/v1/admin/session", None),
        (b"/sso/callback", None),
        (b"/sso/entry", None),
        (b"/api/public/../v1/admin", None),
        (b"/api/public/%2e%2e/v1/admin", None),
    ],
)
def test_public_root_alias_only_allows_public_paths(
    raw_path: bytes, expected: bytes | None
) -> None:
    assert http_prefix_proxy._public_root_path(raw_path) == expected


def test_public_root_alias_rejects_admin_without_upstream_call() -> None:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        raise AssertionError("拒绝路径不应读取请求体。")

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    proxy = http_prefix_proxy.PrefixProxy(
        upstream_origin="http://127.0.0.1:8289",
        external_prefix="/kb",
        public_root_alias=True,
    )
    asyncio.run(
        proxy(
            {"type": "http", "raw_path": b"/admin"},
            receive,
            send,
        )
    )

    assert messages[0]["status"] == 404


@pytest.mark.parametrize(
    ("raw_path", "upstream_path"),
    [
        (b"/", "/"),
        (b"/api/public/session", "/api/public/session"),
        (b"/kb/admin", "/admin"),
    ],
)
def test_public_root_alias_forwards_only_one_prefix(
    monkeypatch: pytest.MonkeyPatch,
    raw_path: bytes,
    upstream_path: str,
) -> None:
    observed: list[tuple[str, str]] = []
    messages: list[dict[str, object]] = []
    original_client = httpx.AsyncClient

    def transport(request: httpx.Request) -> httpx.Response:
        observed.append((request.url.path, request.headers["host"]))
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    def client(**kwargs: object) -> httpx.AsyncClient:
        return original_client(
            transport=httpx.MockTransport(transport), **kwargs
        )

    monkeypatch.setattr(http_prefix_proxy.httpx, "AsyncClient", client)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    proxy = http_prefix_proxy.PrefixProxy(
        upstream_origin="http://127.0.0.1:8289",
        external_prefix="/kb",
        public_root_alias=True,
    )
    asyncio.run(
        proxy(
            {
                "type": "http",
                "method": "GET",
                "raw_path": raw_path,
                "query_string": b"",
                "headers": [(b"host", b"10.242.180.54:8289")],
            },
            receive,
            send,
        )
    )

    assert observed == [(upstream_path, "10.242.180.54:8289")]
    assert messages[0]["status"] == 200
    assert messages[-1]["body"] == b""


@pytest.mark.parametrize(
    "value",
    [
        "http://0.0.0.0:8289",
        "https://127.0.0.1:8289",
        "http://example.com:8289",
        "http://127.0.0.1:8289/path",
    ],
)
def test_upstream_origin_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        http_prefix_proxy._upstream_origin(value)
