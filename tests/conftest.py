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


def _render_network_host(host: object) -> str:
    if not isinstance(host, str):
        return f"<{type(host).__name__}>"
    if len(host) > 255:
        host = f"{host[:252]}..."
    return repr(host)


def _blocked_network_error(host: object) -> OSError:
    rendered_host = _render_network_host(host)
    return OSError(f"TEST_NETWORK_DISABLED host={rendered_host}")


@pytest.fixture(autouse=True)
def _block_non_loopback_network(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    if os.environ.get("RAG_TEST_NETWORK", "offline") == "live":
        yield
        return

    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    def guarded_connect(
        instance: socket.socket,
        address: object,
    ) -> object:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(address, tuple) and _is_loopback(host):
            return original_connect(instance, address)
        if isinstance(address, str) and instance.family == socket.AF_UNIX:
            return original_connect(instance, address)
        raise _blocked_network_error(host)

    def guarded_getaddrinfo(
        host: object,
        *arguments: object,
        **keywords: object,
    ) -> list[tuple[object, ...]]:
        if not _is_loopback(host):
            raise _blocked_network_error(host)
        return original_getaddrinfo(host, *arguments, **keywords)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
