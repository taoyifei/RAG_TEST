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


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [24, 250])
async def test_messages_load_all_pages(count: int) -> None:
    """12 轮及超过一页的历史都不能误判为来源消失。"""
    messages = [
        {
            "id": f"m-{index}",
            "created_at": f"2026-09-26T00:{index // 60:02d}:{index % 60:02d}Z",
            "content": f"正文 {index}",
        }
        for index in range(count)
    ]
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        before = request.url.params.get("before_time")
        eligible = [
            item
            for item in messages
            if before is None or item["created_at"] < before
        ]
        limit = int(request.url.params["limit"])
        return httpx.Response(
            200, json={"success": True, "data": eligible[-limit:]}
        )

    client = _message_client(respond)
    try:
        result = await client.messages(user_id="user-1", session_id="native-s1")
    finally:
        await client.close()
    assert {item["id"] for item in result} == {item["id"] for item in messages}
    assert len(calls) > 1 if count > 100 else len(calls) == 1


@pytest.mark.asyncio
async def test_messages_do_not_skip_shared_timestamp_at_page_boundary() -> None:
    messages = [
        {
            "id": f"m-{index}",
            "created_at": f"2026-09-26T00:{index // 60:02d}:{index % 60:02d}Z",
        }
        for index in range(220)
    ]
    for index in range(118, 123):
        messages[index]["created_at"] = "2026-09-26T00:01:58Z"

    def respond(request: httpx.Request) -> httpx.Response:
        before = request.url.params.get("before_time")
        eligible = [
            item
            for item in messages
            if before is None or item["created_at"] < before
        ]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": eligible[-int(request.url.params["limit"]) :],
            },
        )

    client = _message_client(respond)
    try:
        result = await client.messages(user_id="user-1", session_id="native-s1")
    finally:
        await client.close()
    assert {item["id"] for item in result} == {item["id"] for item in messages}


@pytest.mark.asyncio
async def test_messages_fail_explicitly_when_older_page_fails() -> None:
    messages = [
        {
            "id": f"m-{index}",
            "created_at": f"2026-09-26T00:{index // 60:02d}:{index % 60:02d}Z",
        }
        for index in range(120)
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("before_time"):
            return httpx.Response(503, json={"success": False})
        return httpx.Response(
            200, json={"success": True, "data": messages[-101:]}
        )

    client = _message_client(respond)
    try:
        with pytest.raises(NativeHttpError) as caught:
            await client.messages(user_id="user-1", session_id="native-s1")
    finally:
        await client.close()
    assert caught.value.status_code == 503


def _message_client(respond: httpx.MockTransportHandler) -> WeKnoraClient:
    return WeKnoraClient(
        base_url="http://native.test:8080",
        api_key="test-only-key",
        signer=ExternalPrincipalSigner(
            secret="test-only",  # noqa: S106 - 测试固定签名。
            tenant_id=9,
            deployment_id="candidate",
        ),
        transport=httpx.MockTransport(respond),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (
            400,
            {"error": {"message": "maximum context length is 8192"}},
            "MODEL_CONTEXT_EXCEEDED",
        ),
        (401, {"message": "invalid key"}, "NATIVE_AUTH_ERROR"),
        (403, {"message": "denied"}, "NATIVE_AUTH_ERROR"),
        (429, {"message": "limited"}, "NATIVE_RATE_LIMITED"),
        (500, {"message": "failed"}, "NATIVE_HTTP_500"),
    ],
)
async def test_native_http_error_keeps_safe_category(
    status: int, body: dict[str, object], category: str
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body)

    client = _message_client(respond)
    try:
        with pytest.raises(NativeHttpError) as caught:
            async for _ in client.knowledge_chat(
                user_id="user-1",
                session_id="native-s1",
                question="问题",
                knowledge_base_ids=("kb-public",),
            ):
                pass
    finally:
        await client.close()
    assert caught.value.category == category
    assert len(requests) == 1
    assert "invalid key" not in str(caught.value)


@pytest.mark.asyncio
async def test_native_non_json_error_is_classified() -> None:
    client = _message_client(
        lambda _request: httpx.Response(502, text="private path")
    )
    try:
        with pytest.raises(NativeHttpError) as caught:
            async for _ in client.knowledge_chat(
                user_id="user-1",
                session_id="native-s1",
                question="问题",
                knowledge_base_ids=("kb-public",),
            ):
                pass
    finally:
        await client.close()
    assert caught.value.category == "NATIVE_NON_JSON"
    assert "private path" not in str(caught.value)


@pytest.mark.asyncio
async def test_native_stop_already_completed_is_not_cancel_confirmation() -> (
    None
):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": True, "message": "Message already completed"},
        )

    client = _message_client(respond)
    try:
        assert not await client.stop(
            user_id="user-1", session_id="s-1", message_id="m-1"
        )
    finally:
        await client.close()
