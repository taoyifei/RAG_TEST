"""独立网关把真实原生接口形状贯通到公共浏览器合同。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import wanshitong_gateway.app as gateway_app
from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.engine_settings import EngineSettings
from wanshitong_gateway.roles import UserProfile
from wanshitong_gateway.store import GatewayStore
from wanshitong_gateway.weknora.admin import NativeAdminClient
from wanshitong_gateway.weknora.client import NativeHttpError, WeKnoraClient
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

    def require_profile(
        self, request: Request, *, csrf: bool | None = None
    ) -> UserProfile:
        principal = self.require_public(request, csrf=csrf)
        return UserProfile(
            user_id=principal.user_id,
            username=principal.user_id,
            display_name=principal.user_id,
            dept_id=None,
            role="user",
        )

    def require_admin(
        self, request: Request, *, csrf: bool | None = None
    ) -> SimpleNamespace:
        del request, csrf
        return SimpleNamespace(audit_actor="admin_session:synthetic")


@pytest.fixture
def managed_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[
    tuple[TestClient, GatewayStore, list[httpx.Request], dict[str, int]]
]:
    """提供两用户权限边界和可控原生删除失败的真实HTTP客户端合同。"""
    requests: list[httpx.Request] = []
    status = {"delete": 200}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(
                status["delete"], json={"success": status["delete"] == 200}
            )
        if request.url.path == "/api/v1/sessions":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": "native-1", "title": "历史管理验收"}],
                    "total": 1,
                    "page": 1,
                    "page_size": 1000,
                },
            )
        if request.url.path == "/api/v1/sessions/native-1":
            return httpx.Response(
                200,
                json={"success": True, "data": {"title": "历史管理验收"}},
            )
        if request.url.path == "/api/v1/messages/native-1/load":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "message-1",
                            "content": "保留原有历史",
                            "created_at": "2026-11-01T00:00:00Z",
                            "is_completed": True,
                        }
                    ],
                },
            )
        raise AssertionError(request.url.path)

    store = GatewayStore(tmp_path / "gateway.sqlite3")
    auth = _FakeAuth(tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    native = WeKnoraClient(
        base_url="http://native.test:8080",
        api_key="test-only-key",
        signer=ExternalPrincipalSigner(
            secret="test-signing",  # noqa: S106
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
    scope = {"deployment_id": "candidate", "owner_id": "rdms:candidate:u1"}
    store.bind_session(
        **scope, conversation_id="c-1", native_session_id="native-1"
    )
    store.start_turn(
        **scope,
        trace_id="trace-history",
        conversation_id="c-1",
        native_session_id="native-1",
        question="历史问题",
    )
    store.finish_turn(
        trace_id="trace-history",
        status="completed",
        answer="保留原有历史",
        references=(),
        native_message_id="message-1",
        native_request_id="req-1",
        truncated=False,
        finish_reason="stop",
    )
    app = gateway_app.create_app(
        auth=cast(GatewayAuth, auth),
        store=store,
        engine=engine,
        native=native,
        admin_native=admin,
    )
    with TestClient(app, base_url="http://candidate.test") as browser:
        yield browser, store, requests, status


def test_archive_restore_keeps_history_and_owner_boundary(
    managed_history: tuple[
        TestClient, GatewayStore, list[httpx.Request], dict[str, int]
    ],
) -> None:
    browser, store, requests, _status = managed_history
    base = "/kb/api/public/conversations"
    before = browser.get(base + "/c-1").json()
    assert (
        browser.post(
            base + "/c-1/archive",
            json={"archived": True},
            headers={"x-test-user": "u2"},
        ).status_code
        == 404
    )
    assert (
        browser.delete(base + "/c-1", headers={"x-test-user": "u2"}).status_code
        == 404
    )
    assert all(request.method != "DELETE" for request in requests)
    assert browser.post(
        base + "/c-1/archive", json={"archived": True}
    ).json() == {"conversation_id": "c-1", "archived": True}
    assert browser.get(base).json()["items"] == []
    archived = browser.get(base + "?archived=true").json()["items"]
    assert archived[0]["conversation_id"] == "c-1"
    assert archived[0]["archived"] is True
    assert browser.get(base + "/c-1").json() == before
    assert (
        store.native_session(
            deployment_id="candidate",
            owner_id="rdms:candidate:u1",
            conversation_id="c-1",
        )
        == "native-1"
    )
    assert (
        browser.get(
            base + "?archived=true", headers={"x-test-user": "u2"}
        ).json()["items"]
        == []
    )
    assert (
        browser.post(
            base + "/c-1/archive", json={"archived": False}
        ).status_code
        == 200
    )
    assert browser.get(base).json()["items"][0]["archived"] is False
    assert browser.get(base + "?archived=true").json()["items"] == []


@pytest.mark.parametrize("native_status", [200, 404])
def test_delete_requires_native_success_and_rejects_old_conversation_id(
    managed_history: tuple[
        TestClient, GatewayStore, list[httpx.Request], dict[str, int]
    ],
    native_status: int,
) -> None:
    browser, store, requests, status = managed_history
    base = "/kb/api/public/conversations/c-1"
    status["delete"] = 503
    assert browser.delete(base).status_code == 502
    assert store.get_turn(trace_id="trace-history") is not None
    assert browser.get(base).json()["turns"][0]["answer"] == "保留原有历史"
    status["delete"] = native_status
    assert browser.delete(base).json() == {"deleted": True}
    assert store.get_turn(trace_id="trace-history") is None
    assert browser.get(base).json() == {"turns": []}
    assert browser.get("/kb/api/public/conversations").json()["items"] == []
    assert (
        browser.post(base + "/archive", json={"archived": False}).status_code
        == 404
    )
    before = len(requests)
    assert (
        browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "c-1", "query": "继续"},
        ).status_code
        == 404
    )
    assert len(requests) == before


def test_busy_conversation_rejects_overlapping_history_mutations(
    managed_history: tuple[
        TestClient, GatewayStore, list[httpx.Request], dict[str, int]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, _store, _requests, _status = managed_history
    started = Event()
    release = Event()
    original = WeKnoraClient.delete_session

    async def wait_for_release(
        client: WeKnoraClient, *, user_id: str, session_id: str
    ) -> None:
        started.set()
        assert await asyncio.to_thread(release.wait, 5)
        await original(client, user_id=user_id, session_id=session_id)

    monkeypatch.setattr(WeKnoraClient, "delete_session", wait_for_release)
    base = "/kb/api/public/conversations/c-1"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(browser.delete, base)
        try:
            assert started.wait(3)
            assert (
                browser.post(
                    base + "/archive", json={"archived": True}
                ).status_code
                == 409
            )
            assert browser.delete(base).status_code == 409
        finally:
            release.set()
        assert pending.result(timeout=3).json() == {"deleted": True}


def _bind_test_history(
    store: GatewayStore,
    conversation_id: str,
    *,
    owner: str = "u1",
    archived: bool = False,
    kb_scope: tuple[str, ...] = ("kb-public",),
) -> str:
    """创建一条测试本人映射，原生列表由独立可控状态提供。"""
    native_id = "native-" + conversation_id
    scope = {
        "deployment_id": "candidate",
        "owner_id": "rdms:candidate:" + owner,
    }
    store.bind_session(
        **scope, conversation_id=conversation_id, native_session_id=native_id
    )
    store.start_turn(
        **scope,
        trace_id="trace-" + conversation_id,
        conversation_id=conversation_id,
        native_session_id=native_id,
        question="完整历史问题",
        kb_scope=kb_scope,
    )
    if archived:
        assert store.archive_conversation(
            **scope, conversation_id=conversation_id, archived=True
        )
    return native_id


def _paginated_history_titles(
    store: GatewayStore, monkeypatch: pytest.MonkeyPatch, *, archived: bool
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """65 条等时记录覆盖旧50条窗口；原生兼容行刻意含未映射会话。"""
    monkeypatch.setattr(
        "wanshitong_gateway.store._now",
        lambda: "2026-11-01T00:00:00+00:00",
    )
    sessions = {
        _bind_test_history(
            store, f"page-{index:03d}", archived=archived
        ): f"历史标题 {index:03d}"
        for index in range(65)
    }
    sessions["native-page-000"] = "很早的委托单审批说明"
    sessions["native-not-mapped"] = "未映射的原生兼容标题"
    calls: list[tuple[str, str]] = []

    async def list_sessions(
        _client: WeKnoraClient, *, user_id: str, keyword: str = ""
    ) -> list[dict[str, Any]]:
        calls.append((user_id, keyword))
        return [
            {"id": identifier, "title": title}
            for identifier, title in sessions.items()
            if keyword.casefold() in title.casefold()
        ]

    monkeypatch.setattr(WeKnoraClient, "list_sessions", list_sessions)
    return sessions, calls


@pytest.mark.parametrize("archived", [False, True])
def test_history_pagination_passes_old_window_and_stays_stable(
    managed_history: tuple[
        TestClient, GatewayStore, list[httpx.Request], dict[str, int]
    ],
    monkeypatch: pytest.MonkeyPatch,
    archived: bool,
) -> None:
    browser, store, _requests, _status = managed_history
    sessions, calls = _paginated_history_titles(
        store, monkeypatch, archived=archived
    )
    params = {"archived": str(archived).lower()}
    base = "/kb/api/public/conversations"
    first = browser.get(base, params=params).json()
    assert first["limit"] == 30
    assert first["total"] == 65
    assert first["has_more"] is True
    assert len(first["items"]) == 30
    assert first["items"][0]["conversation_id"] == "page-064"
    assert calls == [("u1", "")]

    # 新会话插入当前列表前端，后续页不应重复或跳过原有等时记录。
    identifier = _bind_test_history(store, "zz-new", archived=archived)
    sessions[identifier] = "刚新增的标题"
    identifiers = [item["conversation_id"] for item in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        response = browser.get(base, params={**params, "cursor": cursor})
        assert response.status_code == 200
        page = response.json()
        identifiers.extend(item["conversation_id"] for item in page["items"])
        assert page["has_more"] is (page["next_cursor"] is not None)
        cursor = page["next_cursor"]
    assert identifiers == [f"page-{index:03d}" for index in reversed(range(65))]
    refreshed = browser.get(base, params=params).json()
    assert refreshed["items"][0]["conversation_id"] == "zz-new"
    other_group = browser.get(
        base, params={"archived": str(not archived).lower()}
    ).json()
    assert other_group["items"] == []


def test_history_search_finds_older_native_title_and_binds_cursor(
    managed_history: tuple[
        TestClient, GatewayStore, list[httpx.Request], dict[str, int]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, store, _requests, _status = managed_history
    _sessions, calls = _paginated_history_titles(
        store, monkeypatch, archived=False
    )
    base = "/kb/api/public/conversations"
    found = browser.get(base, params={"query": "  委托单审批  "}).json()
    assert found["total"] == 1
    assert found["items"][0]["conversation_id"] == "page-000"
    assert found["has_more"] is False
    assert found["next_cursor"] is None
    assert calls[-1] == ("u1", "委托单审批")
    cursor = browser.get(base, params={"limit": 1}).json()["next_cursor"]
    for params, headers in (
        ({"cursor": cursor, "query": "历史"}, {}),
        ({"cursor": cursor, "archived": "true"}, {}),
        ({"cursor": cursor}, {"x-test-user": "u2"}),
        ({"cursor": cursor[:-4] + "AAAA"}, {}),
    ):
        assert (
            browser.get(base, params=params, headers=headers).status_code == 422
        )


def test_history_counts_exclude_unmapped_deleted_unavailable_and_revoked(
    managed_history: tuple[
        TestClient, GatewayStore, list[httpx.Request], dict[str, int]
    ],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    browser, store, _requests, _status = managed_history
    sessions, _calls = _paginated_history_titles(
        store, monkeypatch, archived=False
    )
    sessions.pop("native-page-001")
    assert store.delete_conversation(
        deployment_id="candidate",
        owner_id="rdms:candidate:u1",
        conversation_id="page-002",
    )
    foreign = _bind_test_history(store, "other-person", owner="u2")
    sessions[foreign] = "他人的标题不应公开"
    store.bind_session(
        deployment_id="other-deployment",
        owner_id="rdms:candidate:u1",
        conversation_id="other-deployment",
        native_session_id="native-other-deployment",
    )
    sessions["native-other-deployment"] = "其他部署标题不应公开"
    with sqlite3.connect(tmp_path / "gateway.sqlite3") as connection:
        connection.execute(
            "INSERT INTO library_kbs(kb_id,scope,owner_id) VALUES(?,?,?)",
            ("kb-hr-secret", "hr_internal", ""),
        )
    hidden = _bind_test_history(store, "hr-secret", kb_scope=("kb-hr-secret",))
    sessions[hidden] = "人力内部秘密标题"

    original = _FakeAuth.require_profile

    def profile(
        auth: _FakeAuth, request: Request, *, csrf: bool | None = None
    ) -> UserProfile:
        value = original(auth, request, csrf=csrf)
        return (
            replace(value, role="hr")
            if request.headers.get("x-test-hr")
            else value
        )

    monkeypatch.setattr(_FakeAuth, "require_profile", profile)
    base = "/kb/api/public/conversations"
    before = browser.get(
        base, params={"limit": 1}, headers={"x-test-hr": "1"}
    ).json()
    assert before["total"] == 64
    cursor = before["next_cursor"]
    after = browser.get(base, params={"cursor": cursor, "limit": 50}).json()
    assert after["total"] == 63
    assert "人力内部秘密标题" not in json.dumps(after, ensure_ascii=False)
    secret_search = browser.get(base, params={"query": "秘密"}).json()
    assert secret_search["total"] == 0
    assert secret_search["items"] == []
    assert secret_search["has_more"] is False
    assert secret_search["next_cursor"] is None
    own = browser.get(base, headers={"x-test-user": "u2"}).json()
    assert own["total"] == 1
    assert own["items"][0]["conversation_id"] == "other-person"


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "51"},
        {"limit": "abc"},
        {"query": "长" * 201},
        {"archived": "unknown"},
        {"cursor": ""},
        {"cursor": "not-a-valid-cursor"},
        {"cursor": "长" * 4097},
    ],
)
def test_history_rejects_invalid_pagination_input(
    managed_history: tuple[
        TestClient, GatewayStore, list[httpx.Request], dict[str, int]
    ],
    params: dict[str, str],
) -> None:
    browser, _store, _requests, _status = managed_history
    assert (
        browser.get("/kb/api/public/conversations", params=params).status_code
        == 422
    )


@pytest.mark.parametrize("native_status", [403, 503])
def test_history_native_unavailable_is_not_reported_as_empty(
    managed_history: tuple[
        TestClient, GatewayStore, list[httpx.Request], dict[str, int]
    ],
    monkeypatch: pytest.MonkeyPatch,
    native_status: int,
) -> None:
    browser, _store, _requests, _status = managed_history

    async def fail(
        _client: WeKnoraClient, *, user_id: str, keyword: str = ""
    ) -> list[dict[str, Any]]:
        del user_id, keyword
        raise NativeHttpError(native_status, "private-native-error")

    monkeypatch.setattr(WeKnoraClient, "list_sessions", fail)
    response = browser.get("/kb/api/public/conversations")
    assert response.status_code == 502
    assert "items" not in response.json()
    assert "private-native-error" not in response.text


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
                    "data": [
                        {
                            "id": "answer-1",
                            "content": "第一段第二段",
                            "created_at": "2026-09-26T00:00:00Z",
                            "is_completed": True,
                        }
                    ],
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


def test_recommendations_require_review_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """草稿不公开；人工核对后公开，下架立即停止展示。"""
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    auth = _FakeAuth(tmp_path / "gateway.sqlite3")
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )

    async def close() -> None:
        return None

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
        native=cast(
            WeKnoraClient,
            SimpleNamespace(close=close, api_key_fingerprint=lambda: b"x" * 32),
        ),
        admin_native=cast(NativeAdminClient, SimpleNamespace(close=close)),
    )
    base = "/kb/api/admin/ops/recommendations"
    question = "开发中心负责哪些工作？"
    with TestClient(app, base_url="http://candidate.test") as browser:
        draft = browser.post(
            base,
            json={"question": question, "topic_key": "研发", "state": "DRAFT"},
        )
        assert draft.status_code == 200
        item = draft.json()
        assert item["state"] == "DRAFT"
        assert browser.get(base).json()["items"][0]["question"] == question
        assert (
            browser.get("/kb/api/public/popular-questions").json()["mode"]
            == "EMPTY"
        )

        url = f"{base}/{item['recommendation_id']}"
        body = {
            "expected_version": item["version"],
            "question": question,
            "topic_key": "研发",
            "source_knowledge_ids": [_KNOWLEDGE_ID],
            "review_note": "已在候选知识库核对原件和题面",
            "state": "APPROVED",
        }
        assert browser.put(url, json=body).status_code == 422
        approved = browser.put(url, json={**body, "review_confirmed": True})
        assert approved.status_code == 200
        public = browser.get("/kb/api/public/popular-questions").json()
        assert public["mode"] == "POPULAR"
        assert public["items"] == [
            {
                "id": item["recommendation_id"],
                "question": question,
                "topic_key": "研发",
            }
        ]
        assert (
            browser.put(
                url, json={**body, "review_confirmed": True}
            ).status_code
            == 409
        )
        disabled = browser.put(
            url,
            json={
                **body,
                "expected_version": approved.json()["version"],
                "state": "DISABLED",
                "disabled_reason": "资料需更新",
            },
        )
        assert disabled.status_code == 200
        assert (
            browser.get("/kb/api/public/popular-questions").json()["items"]
            == []
        )
