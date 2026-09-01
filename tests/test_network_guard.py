from __future__ import annotations

import os
import socket

import pytest


def _require_offline_guard() -> None:
    if os.environ.get("RAG_TEST_NETWORK", "offline") == "live":
        pytest.skip("显式 live 模式不运行离线网络守卫测试。")


def test_non_loopback_resolution_reports_only_host() -> None:
    _require_offline_guard()

    with pytest.raises(OSError) as error:
        socket.getaddrinfo("blocked.invalid", 443)

    assert str(error.value) == (
        "TEST_NETWORK_DISABLED host='blocked.invalid'"
    )


def test_non_loopback_connection_reports_only_host() -> None:
    _require_offline_guard()

    with socket.socket() as client, pytest.raises(OSError) as error:
        client.connect(("198.51.100.1", 443))

    assert str(error.value) == "TEST_NETWORK_DISABLED host='198.51.100.1'"


def test_loopback_resolution_remains_available() -> None:
    _require_offline_guard()

    addresses = socket.getaddrinfo("127.0.0.1", 0)

    assert addresses
