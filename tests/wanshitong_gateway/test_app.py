"""独立网关把真实原生接口形状贯通到公共浏览器合同。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import wanshitong_gateway.app as gateway_app
from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.engine_settings import EngineSettings
from wanshitong_gateway.store import GatewayStore
from wanshitong_gateway.weknora.admin import NativeAdminClient
from wanshitong_gateway.weknora.client import WeKnoraClient
from wanshitong_gateway.weknora.principal import ExternalPrincipalSigner

_ADMIN_TENANT = 8
_KNOWLEDGE_ID = "f90f5e24-2e95-48db-ad38-c942bb9212b8"
_PNG = b"\x89PNG\r\n\x1a\nsynthetic-image"
_NATIVE_SSE = (
    'event: message\ndata: {"id":"req-1","response_type":"agent_query",'
    '"content":"","done":false,"assistant_message_id":"answer-1"}\n\n'
    'event: message\ndata: {"id":"req-1","response_type":"answer",'
    '"content":"第一段","done":false}\n\n'
    'event: message\ndata: {"id":"req-1","response_type":"answer",'
    '"content":"第二段","done":true,"finish_reason":"stop"}\n\n'
    'event: message\ndata: {"id":"req-1","response_type":"references",'
    '"content":"","done":false,"knowledge_references":[{'
    '"id":"chunk-1","knowledge_id":"f90f5e24-2e95-48db-ad38-c942bb9212b8",'
    '"knowledge_title":"制度文件","knowledge_filename":"制度文件.docx",'
    '"content":"原生证据片段"}]}\n\n'
    'event: message\ndata: {"id":"req-1","response_type":"complete",'
    '"content":"","done":true}\n\n'
).encode()


class _FakeAuth:
    """本测试只代替身份入口，真实 SSO/CSRF 由 test_auth 覆盖。"""

    def __init__(self, database_path: Path) -> None:
        self.settings = SimpleNamespace(
            database_path=database_path,
            deployment_id="candidate",
            root_path="/kb",
        )

    def require_public(
        self, request: Request, *, csrf: bool | None = None
    ) -> SimpleNamespace:
        del csrf
        return SimpleNamespace(user_id=request.headers.get("x-test-user", "u1"))

    def require_admin(
        self, request: Request, *, csrf: bool | None = None
    ) -> SimpleNamespace:
        del request, csrf
        return SimpleNamespace(audit_actor="admin_session:synthetic")


def _events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(frame.split("data: ", maxsplit=1)[1])
        for frame in body.strip().split("\n\n")
    ]


@pytest.mark.parametrize("second_user", ["u2"])
def test_public_native_round_trip_preserves_answer_and_isolation(  # noqa: PLR0915 - 一次请求贯通流、历史与文件鉴权。
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_user: str,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - 原生多端点形状逐项模拟。
        requests.append(request)
        path = request.url.path
        if path == "/api/v1/sessions" and request.method == "POST":
            return httpx.Response(
                201, json={"success": True, "data": {"id": "session-1"}}
            )
        if path == "/api/v1/knowledge-chat/session-1":
            return httpx.Response(
                200,
                content=_NATIVE_SSE,
                headers={"Content-Type": "text/event-stream"},
            )
        if path == "/api/v1/messages/session-1/load":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": "answer-1", "content": "第一段第二段"}],
                },
            )
        if path == "/api/v1/sessions/session-1":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"id": "session-1", "title": "测试会话"},
                },
            )
        if path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "admin-token"}
            )
        if path == f"/api/v1/knowledge/{_KNOWLEDGE_ID}/download":
            return httpx.Response(
                200,
                content=b"original-docx",
                headers={
                    "content-type": "application/octet-stream",
                    "content-disposition": 'attachment; filename="source.docx"',
                },
            )
        if path == "/api/v1/sessions/session-1/messages/answer-1/files":
            assert request.url.params["file_path"] == "resource://chart.png"
            return httpx.Response(
                200, content=_PNG, headers={"content-type": "image/png"}
            )
        raise AssertionError(f"unexpected native route: {path}")

    store = GatewayStore(tmp_path / "gateway.sqlite3")
    auth = _FakeAuth(tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    native = WeKnoraClient(
        base_url="http://native.test:8080",
        api_key="test-only-key",
        signer=ExternalPrincipalSigner(
            secret="test-only-signing-key",  # noqa: S106
            tenant_id=_ADMIN_TENANT,
            deployment_id="candidate",
        ),
        transport=httpx.MockTransport(respond),
    )
    admin = NativeAdminClient(
        base_url="http://native.test:8080",
        email="candidate@example.test",
        password="test-only-password",  # noqa: S106
        tenant_id=_ADMIN_TENANT,
        transport=httpx.MockTransport(respond),
    )
    engine = EngineSettings(
        base_url="http://native.test:8080",
        tenant_id=_ADMIN_TENANT,
        public_kb_ids=("kb-public",),
        api_key_file=tmp_path / "unused-key",
        external_signing_key_file=tmp_path / "unused-signing",
        admin_email="candidate@example.test",
        admin_password_file=tmp_path / "unused-password",
    )
    app = gateway_app.create_app(
        auth=cast(GatewayAuth, auth),
        store=store,
        engine=engine,
        native=native,
        admin_native=admin,
    )
    with TestClient(app, base_url="http://candidate.test") as browser:
        question = "  " + "长问题" * 2100 + "  "
        response = browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "c-1", "query": question},
        )
        assert response.status_code == 200
        events = _events(response.text)
        assert [event["type"] for event in events] == [
            "meta",
            "native_event",
            "answer_delta",
            "answer_delta",
            "references",
            "final",
        ]
        final = events[-1]
        assert final["answer"] == "第一段第二段"
        assert final["native_message_id"] == "answer-1"
        assert final["finish_reason"] == "stop"
        assert events[1]["native"]["response_type"] == "agent_query"
        citation = final["citations"][0]
        assert citation["quote"] == "原生证据片段"
        assert citation["source_available"] is True
        trace_id = final["trace_id"]
        source_url = (
            "/kb/api/public/conversations/c-1/turns/"
            f"{trace_id}/references/{citation['reference_id']}/source"
        )
        source = browser.get(source_url)
        assert source.status_code == 200
        assert source.text == "原生证据片段"
        assert source.headers["content-type"].startswith("text/plain")
        assert (
            browser.get(
                source_url, headers={"x-test-user": second_user}
            ).status_code
            == 404
        )
        original_url = source_url.removesuffix("/source") + "/original"
        original = browser.get(original_url)
        assert original.status_code == 200
        assert original.content == b"original-docx"
        assert original.headers["content-disposition"].startswith("attachment;")
        assert (
            browser.get(
                original_url, headers={"x-test-user": second_user}
            ).status_code
            == 404
        )
        image_url = (
            f"/kb/api/public/conversations/c-1/turns/{trace_id}/resources"
        )
        image = browser.get(
            image_url, params={"file_path": "resource://chart.png"}
        )
        assert image.status_code == 200
        assert image.content == _PNG
        assert (
            browser.get(
                image_url,
                params={"file_path": "resource://chart.png"},
                headers={"x-test-user": second_user},
            ).status_code
            == 404
        )
        assert (
            browser.get(
                image_url, params={"file_path": "file:///etc/passwd"}
            ).status_code
            == 400
        )
        history = browser.get("/kb/api/public/conversations/c-1")
        assert history.json()["turns"][0]["answer"] == "第一段第二段"
        assert browser.get(
            "/kb/api/public/conversations/c-1",
            headers={"x-test-user": second_user},
        ).json() == {"turns": []}
    assert (
        len(
            [
                request
                for request in requests
                if request.url.path == "/api/v1/knowledge-chat/session-1"
            ]
        )
        == 1
    )
    native_chat = next(
        request
        for request in requests
        if request.url.path == "/api/v1/knowledge-chat/session-1"
    )
    assert json.loads(native_chat.content)["query"] == question
