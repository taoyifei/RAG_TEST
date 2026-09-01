from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Generator

import pytest


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _block_non_loopback_network(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    if os.environ.get("RAG_TEST_NETWORK") != "offline":
        yield
        return

    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    def guarded_connect(
        instance: socket.socket,
        address: object,
    ) -> object:
        if isinstance(address, tuple) and _is_loopback(address[0]):
            return original_connect(instance, address)
        if isinstance(address, str) and instance.family == socket.AF_UNIX:
            return original_connect(instance, address)
        raise OSError("TEST_NETWORK_DISABLED")

    def guarded_getaddrinfo(
        host: object,
        *arguments: object,
        **keywords: object,
    ) -> list[tuple[object, ...]]:
        if not _is_loopback(host):
            raise OSError("TEST_NETWORK_DISABLED")
        return original_getaddrinfo(host, *arguments, **keywords)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
