"""白标原生管理界面所用 API 与账号边界。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from wanshitong_gateway.weknora.admin import (
    NativeAdminClient,
    _allowed_admin_path,
)
from wanshitong_gateway.weknora.client import NativeHttpError


class _ControlledSse(httpx.AsyncByteStream):
    """控制第二帧的到达，以验证网关没有预先读完整条流。"""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: first\n\n"
        self.waiting.set()
        await self.release.wait()
        yield b"data: second\n\n"

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_native_admin_sse_is_forwarded_before_upstream_finishes() -> None:
    upstream_stream = _ControlledSse()
    seen_requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "admin-token"}
            )
        return httpx.Response(
            200,
            stream=upstream_stream,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
        )

    client = NativeAdminClient(
        base_url="http://native.test:8080",
        email="admin@example.test",
        password="test-only",  # noqa: S106 - 测试固定密码。
        tenant_id=9,
        transport=httpx.MockTransport(respond),
    )
    try:
        result = await asyncio.wait_for(
            client.proxy(
                method="POST",
                path="api/v1/knowledge-search",
                query="",
                content=b'{"query":"hello"}',
                browser_headers={"Accept": "text/event-stream"},
            ),
            timeout=1,
        )
        assert result.content == b""
        assert result.stream is not None
        assert result.headers["X-Accel-Buffering"] == "no"
        assert await anext(result.stream) == b"data: first\n\n"
        assert not upstream_stream.closed
        upstream_stream.release.set()
        assert await anext(result.stream) == b"data: second\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(result.stream)
        assert upstream_stream.closed
        assert (
            seen_requests[-1].headers["authorization"] == "Bearer admin-token"
        )
    finally:
        upstream_stream.release.set()
        await client.close()


@pytest.mark.asyncio
async def test_native_admin_sse_error_closes_upstream() -> None:
    class FailingSse(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"data: first\n\n"
            raise httpx.ReadTimeout("upstream idle")

        async def aclose(self) -> None:
            self.closed = True

    upstream_stream = FailingSse()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "admin-token"}
            )
        return httpx.Response(
            200,
            stream=upstream_stream,
            headers={"Content-Type": "text/event-stream"},
        )

    client = NativeAdminClient(
        base_url="http://native.test:8080",
        email="admin@example.test",
        password="test-only",  # noqa: S106 - 测试固定密码。
        tenant_id=9,
        transport=httpx.MockTransport(respond),
    )
    try:
        result = await client.proxy(
            method="POST",
            path="api/v1/knowledge-search",
            query="",
            content=b"{}",
            browser_headers={"Accept": "text/event-stream"},
        )
        assert result.stream is not None
        assert await anext(result.stream) == b"data: first\n\n"
        with pytest.raises(httpx.ReadTimeout):
            await anext(result.stream)
        assert upstream_stream.closed
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_admin_sse_cancellation_closes_upstream() -> None:
    upstream_stream = _ControlledSse()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "admin-token"}
            )
        return httpx.Response(
            200,
            stream=upstream_stream,
            headers={"Content-Type": "text/event-stream"},
        )

    client = NativeAdminClient(
        base_url="http://native.test:8080",
        email="admin@example.test",
        password="test-only",  # noqa: S106 - 测试固定密码。
        tenant_id=9,
        transport=httpx.MockTransport(respond),
    )
    try:
        result = await client.proxy(
            method="POST",
            path="api/v1/knowledge-search",
            query="",
            content=b"{}",
            browser_headers={"Accept": "text/event-stream"},
        )
        assert result.stream is not None
        assert await anext(result.stream) == b"data: first\n\n"
        pending = asyncio.create_task(anext(result.stream))
        await asyncio.wait_for(upstream_stream.waiting.wait(), timeout=1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert upstream_stream.closed
    finally:
        upstream_stream.release.set()
        await client.close()


@pytest.mark.asyncio
async def test_native_admin_relogin_closes_unauthorized_response() -> None:
    unauthorized = _ControlledSse()
    accepted = _ControlledSse()
    login_count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal login_count
        if request.url.path == "/api/v1/auth/login":
            login_count += 1
            return httpx.Response(
                200,
                json={"success": True, "token": f"admin-token-{login_count}"},
            )
        if request.headers["authorization"] == "Bearer admin-token-1":
            return httpx.Response(
                401,
                stream=unauthorized,
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(
            200, stream=accepted, headers={"Content-Type": "text/event-stream"}
        )

    client = NativeAdminClient(
        base_url="http://native.test:8080",
        email="admin@example.test",
        password="test-only",  # noqa: S106 - 测试固定密码。
        tenant_id=9,
        transport=httpx.MockTransport(respond),
    )
    try:
        result = await client.proxy(
            method="POST",
            path="api/v1/knowledge-search",
            query="",
            content=b"{}",
            browser_headers={"Accept": "text/event-stream"},
        )
        assert login_count == 2
        assert unauthorized.closed
        assert result.stream is not None
        await result.stream.aclose()
        assert accepted.closed
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_admin_request_timeout_has_bounded_error() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "admin-token"}
            )
        raise httpx.ReadTimeout("upstream idle", request=request)

    client = NativeAdminClient(
        base_url="http://native.test:8080",
        email="admin@example.test",
        password="test-only",  # noqa: S106 - 测试固定密码。
        tenant_id=9,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(NativeHttpError) as error:
            await client.proxy(
                method="POST",
                path="api/v1/knowledge-search",
                query="",
                content=b"{}",
                browser_headers={"Accept": "text/event-stream"},
            )
        assert error.value.status_code == 502
        assert "upstream idle" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "path",
    [
        "api/v1/me/browser",
        "api/v1/user/favorites",
        "api/v1/organizations",
        "api/v1/tenants/kv/retrieval-config",
        "api/v1/web-search-providers",
        "api/v1/im-channels",
        "api/v1/embed-channels",
        "api/v1/knowledge-bases",
        "api/v1/models",
    ],
)
def test_native_admin_ui_routes_are_reachable(path: str) -> None:
    assert _allowed_admin_path("GET", path)


@pytest.mark.parametrize(
    "path",
    [
        "api/v1/auth/login",
        "api/v1/system/admin/users",
        "api/v1/system/host-project-dir",
        "api/v1/initialization/ollama/models/download",
        "api/v1/knowledge-bases/../auth/login",
    ],
)
def test_native_admin_proxy_keeps_account_and_host_boundaries(
    path: str,
) -> None:
    assert not _allowed_admin_path("POST", path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "api/v1/knowledge-chat/session-1"),
        ("POST", "api/v1/agent-chat/session-1"),
        ("POST", "api/v1/sessions"),
        ("POST", "api/v1/sessions/session-1/steer"),
        ("POST", "api/v1/sessions/session-1/stop"),
        ("GET", "api/v1/sessions/continue-stream/session-1"),
        ("DELETE", "api/v1/messages/session-1/message-1"),
        ("POST", "api/v1/agents"),
        ("PUT", "api/v1/agents/agent-1"),
        ("GET", "api/v1/tenants/8/api-keys"),
    ],
)
def test_admin_proxy_denies_chat_generation_and_mutation(
    method: str, path: str
) -> None:
    assert not _allowed_admin_path(method, path)
    assert _allowed_admin_path("GET", "api/v1/messages/session-1/load")
    assert _allowed_admin_path("GET", "api/v1/sessions/session-1")
