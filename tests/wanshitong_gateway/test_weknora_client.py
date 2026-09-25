"""原生路由、请求范围及服务端签名的固定合同。"""

from __future__ import annotations

import json

import httpx
import pytest

from wanshitong_gateway.weknora.client import (
    NativeHttpError,
    WeKnoraClient,
)
from wanshitong_gateway.weknora.principal import ExternalPrincipalSigner


@pytest.mark.asyncio
async def test_session_and_chat_use_native_api_without_rewriting() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(
                201, json={"success": True, "data": {"id": "native-s1"}}
            )
        return httpx.Response(
            200,
            content=(
                b'event: message\ndata: {"response_type":"complete",'
                b'"done":true}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    signer = ExternalPrincipalSigner(
        secret="test-only",  # noqa: S106 - 测试固定签名。
        tenant_id=9,
        deployment_id="candidate",
        clock=lambda: 1000.0,
    )
    client = WeKnoraClient(
        base_url="http://native.test:8080",
        api_key="test-only-key",
        signer=signer,
        transport=httpx.MockTransport(respond),
    )
    try:
        assert await client.create_session("user-1") == "native-s1"
        frames = [
            chunk
            async for chunk in client.knowledge_chat(
                user_id="user-1",
                session_id="native-s1",
                question="OPC 是什么？",
                knowledge_base_ids=("kb-public",),
            )
        ]
    finally:
        await client.close()
    assert len(requests) == 2
    assert requests[0].method == "POST"
    assert requests[1].url.path == "/api/v1/knowledge-chat/native-s1"
    assert json.loads(requests[1].content) == {
        "query": "OPC 是什么？",
        "knowledge_base_ids": ["kb-public"],
    }
    assert requests[1].headers["x-api-key"] == "test-only-key"
    assert requests[1].headers["x-external-user-token"].count(".") == 2
    assert b"complete" in b"".join(frames)


@pytest.mark.asyncio
async def test_native_error_does_not_retry_generation() -> None:
    calls = 0

    def fail(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503, json={"success": False, "message": "unavailable"}
        )

    client = WeKnoraClient(
        base_url="http://native.test:8080",
        api_key="test-only-key",
        signer=ExternalPrincipalSigner(
            secret="test-only",  # noqa: S106 - 测试固定签名。
            tenant_id=9,
            deployment_id="candidate",
        ),
        transport=httpx.MockTransport(fail),
    )
    try:
        with pytest.raises(NativeHttpError):
            async for _ in client.knowledge_chat(
                user_id="user-1",
                session_id="native-s1",
                question="问题",
                knowledge_base_ids=("kb-public",),
            ):
                pass
    finally:
        await client.close()
    assert calls == 1
