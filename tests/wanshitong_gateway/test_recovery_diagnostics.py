"""验证原生续流、明确重生成及异常终态的网关合同。"""

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


class _FakeAuth:
    def __init__(self, path: Path) -> None:
        self.settings = SimpleNamespace(
            database_path=path,
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
        return SimpleNamespace(audit_actor="synthetic-admin")


def _gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    respond: httpx.MockTransportHandler,
) -> tuple[Any, GatewayStore]:
    database = tmp_path / "gateway.sqlite3"
    store = GatewayStore(database)
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    transport = httpx.MockTransport(respond)
    native = WeKnoraClient(
        base_url="http://native.test:8080",
        api_key="test-only-key",
        signer=ExternalPrincipalSigner(
            secret="test-only",  # noqa: S106 - 固定合成凭据。
            tenant_id=8,
            deployment_id="candidate",
        ),
        transport=transport,
    )
    admin = NativeAdminClient(
        base_url="http://native.test:8080",
        email="candidate@example.test",
        password="test-only-password",  # noqa: S106 - 固定合成凭据。
        tenant_id=8,
        transport=transport,
    )
    engine = EngineSettings(
        base_url="http://native.test:8080",
        tenant_id=8,
        public_kb_ids=("kb-public",),
        api_key_file=tmp_path / "unused-key",
        external_signing_key_file=tmp_path / "unused-signing",
        admin_email="candidate@example.test",
        admin_password_file=tmp_path / "unused-password",
    )
    app = gateway_app.create_app(
        auth=cast(GatewayAuth, _FakeAuth(database)),
        store=store,
        engine=engine,
        native=native,
        admin_native=admin,
    )
    return app, store


def _events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(frame.split("data: ", maxsplit=1)[1])
        for frame in body.strip().split("\n\n")
    ]


def _native_frame(response_type: str, **fields: object) -> bytes:
    return (
        "event: message\ndata: "
        + json.dumps(
            {
                "id": "req-1",
                "assistant_message_id": "m-1",
                "response_type": response_type,
                **fields,
            },
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode()


def test_continue_stream_does_not_generate_again_and_retry_keeps_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/v1/sessions":
            return httpx.Response(
                201, json={"success": True, "data": {"id": "s-1"}}
            )
        if path == "/api/v1/knowledge-chat/s-1":
            count = sum(item.url.path == path for item in requests)
            body = (
                _native_frame("answer", content="片段")
                + _native_frame("error", content="上游断开")
                if count == 1
                else _native_frame("answer", content="新回答")
                + _native_frame("complete", done=True)
            )
            return httpx.Response(200, content=body)
        if path == "/api/v1/sessions/continue-stream/s-1":
            assert request.method == "GET"
            assert request.url.params["message_id"] == "m-1"
            return httpx.Response(
                200,
                content=_native_frame("answer", content="恢复完整答案")
                + _native_frame("complete", done=True),
            )
        raise AssertionError(path)

    app, store = _gateway(tmp_path, monkeypatch, respond)
    with TestClient(app, base_url="http://candidate.test") as browser:
        first = browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "c-1", "query": "原问题"},
        )
        first_events = _events(first.text)
        trace_id = first_events[0]["trace_id"]
        assert first_events[-1]["type"] == "error"
        recovered = browser.post(
            f"/kb/api/public/chat/{trace_id}/continue",
            json={"conversation_id": "c-1"},
        )
        assert recovered.status_code == 200
        assert _events(recovered.text)[-1]["answer"] == "恢复完整答案"
        assert (
            sum(
                item.url.path == "/api/v1/knowledge-chat/s-1"
                for item in requests
            )
            == 1
        )
        regenerated = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "c-1",
                "query": "原问题",
                "client_context": {
                    "entrypoint": "retry",
                    "retry_of_trace_id": trace_id,
                },
            },
        )
        assert _events(regenerated.text)[-1]["answer"] == "新回答"
    turns = store.list_turns(
        deployment_id="candidate",
        owner_id="rdms:candidate:u1",
        conversation_id="c-1",
    )
    assert len(turns) == 2
    assert turns[0]["trace_id"] == trace_id
    assert (
        json.loads(turns[1]["client_context_json"])["retry_of_trace_id"]
        == trace_id
    )
    assert (
        sum(item.url.path == "/api/v1/knowledge-chat/s-1" for item in requests)
        == 2
    )


@pytest.mark.parametrize(
    ("failure", "category", "upstream_status"),
    [
        (
            httpx.ConnectError("synthetic connection"),
            "UPSTREAM_CONNECTION_ERROR",
            None,
        ),
        (httpx.ReadTimeout("synthetic timeout"), "UPSTREAM_TIMEOUT", None),
        (
            httpx.RemoteProtocolError("synthetic protocol"),
            "UPSTREAM_PROTOCOL_ERROR",
            None,
        ),
        (400, "MODEL_CONTEXT_EXCEEDED", 400),
        (401, "NATIVE_AUTH_ERROR", 401),
        (403, "NATIVE_AUTH_ERROR", 403),
        (429, "NATIVE_RATE_LIMITED", 429),
        (500, "NATIVE_HTTP_500", 500),
    ],
)
def test_upstream_failure_always_finishes_turn_with_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception | int,
    category: str,
    upstream_status: int | None,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(
                201, json={"success": True, "data": {"id": "s-1"}}
            )
        if isinstance(failure, Exception):
            raise failure
        message = (
            "maximum context length is 8192; secret=/private/key"
            if failure == 400
            else "private key invalid"
        )
        return httpx.Response(
            failure,
            json={"success": False, "error": {"message": message}},
        )

    app, store = _gateway(tmp_path, monkeypatch, respond)
    with TestClient(app, base_url="http://candidate.test") as browser:
        response = browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "c-1", "query": "问题"},
        )
    events = _events(response.text)
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == category
    assert "private" not in response.text
    trace_id = events[0]["trace_id"]
    turn = store.get_turn(trace_id=trace_id)
    assert turn is not None
    assert turn["status"] == "failed"
    assert turn["error_category"] == category
    assert turn["upstream_status"] == upstream_status


def test_history_keeps_terminal_state_and_distinguishes_deleted_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = [
        {
            "id": f"m-{index}",
            "created_at": f"2026-09-26T00:00:{index:02d}Z",
            "content": f"原生正文 {index}",
            "is_completed": index in {0, 1, 3},
        }
        for index in range(5)
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/messages/s-1/load"
        return httpx.Response(200, json={"success": True, "data": messages})

    app, store = _gateway(tmp_path, monkeypatch, respond)
    store.bind_session(
        deployment_id="candidate",
        owner_id="rdms:candidate:u1",
        conversation_id="c-1",
        native_session_id="s-1",
    )
    cases = [
        "completed",
        "failed",
        "stop_requested",
        "disconnected",
        "streaming",
        "completed",
    ]
    for index, status in enumerate(cases):
        trace_id = f"trace_{index:032x}"
        store.start_turn(
            trace_id=trace_id,
            deployment_id="candidate",
            owner_id="rdms:candidate:u1",
            conversation_id="c-1",
            native_session_id="s-1",
            question=f"问题 {index}",
        )
        store.set_native_message(
            trace_id=trace_id,
            native_message_id=f"m-{index}",
            native_request_id=f"req-{index}",
        )
        if status == "stop_requested":
            store.request_stop(trace_id=trace_id, owner_id="rdms:candidate:u1")
        elif status != "streaming":
            store.finish_turn(
                trace_id=trace_id,
                status=status,
                answer=f"本地部分正文 {index}",
                references=(),
                native_message_id=f"m-{index}",
                native_request_id=f"req-{index}",
                truncated=index == 0,
                finish_reason="length" if index == 0 else None,
            )
    with TestClient(app, base_url="http://candidate.test") as browser:
        response = browser.get("/kb/api/public/conversations/c-1")
    assert response.status_code == 200
    turns = {item["question"]: item for item in response.json()["turns"]}
    assert turns["问题 0"]["status"] == "completed"
    assert turns["问题 0"]["truncated"] is True
    assert turns["问题 0"]["finish_reason"] == "length"
    assert turns["问题 1"]["status"] == "failed"
    assert turns["问题 1"]["partial"] is True
    assert turns["问题 2"]["status"] == "stop_requested"
    assert turns["问题 3"]["status"] == "pending_confirmation"
    assert turns["问题 4"]["status"] == "streaming"
    assert turns["问题 5"]["status"] == "SOURCE_UNAVAILABLE"
    assert turns["问题 5"]["source_status"] == "missing"


def test_stop_without_native_message_never_claims_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream stop: {request.url.path}")

    app, store = _gateway(tmp_path, monkeypatch, respond)
    store.bind_session(
        deployment_id="candidate",
        owner_id="rdms:candidate:u1",
        conversation_id="c-1",
        native_session_id="s-1",
    )
    trace_id = "trace_" + "e" * 32
    store.start_turn(
        trace_id=trace_id,
        deployment_id="candidate",
        owner_id="rdms:candidate:u1",
        conversation_id="c-1",
        native_session_id="s-1",
        question="问题",
    )
    with TestClient(app, base_url="http://candidate.test") as browser:
        response = browser.post(
            f"/kb/api/public/chat/{trace_id}/stop",
            json={"conversation_id": "c-1"},
        )
    assert response.status_code == 200
    assert response.json() == {"cancelled": False}
    turn = store.get_turn(trace_id=trace_id)
    assert turn is not None
    assert turn["status"] == "streaming"
