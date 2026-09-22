"""8289 私网前缀代理只允许并剥离一次 `/kb`。"""

from __future__ import annotations

import argparse

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
def test_strip_external_prefix(
    raw_path: bytes, expected: bytes | None
) -> None:
    assert (
        http_prefix_proxy._strip_external_prefix(raw_path, b"/kb")
        == expected
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
