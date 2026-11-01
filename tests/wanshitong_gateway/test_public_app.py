"""公共应用绑定从后台保存到新问答消费的回归。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import wanshitong_gateway.app as gateway_app
from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.engine_settings import EngineSettings
from wanshitong_gateway.roles import UserProfile
from wanshitong_gateway.store import GatewayStore
from wanshitong_gateway.weknora.admin import NativeAdminClient
from wanshitong_gateway.weknora.client import WeKnoraClient
from wanshitong_gateway.weknora.principal import ExternalPrincipalSigner

_AGENT_ID_A = "e45d0b61-80bc-4321-82cf-e4448fc62f84"
_AGENT_ID_B = "adb7db7b-319d-4d47-bdf6-b8b50baf5e2b"
_NATIVE_SSE = (
    b'event: message\ndata: {"response_type":"complete","done":true}\n\n'
)


class _FakeAuth:
    """仅替代登录入口；本项目的真实 CSRF 门禁另由认证测试覆盖。"""

    def __init__(
        self, database_path: Path, profile: UserProfile | None = None
    ) -> None:
        self.settings = SimpleNamespace(
            database_path=database_path,
            deployment_id="candidate",
            root_path="/kb",
        )
        self.profile = profile or UserProfile("u1", "u1", "u1", None, "user")

    def require_public(
        self, request: Request, *, csrf: bool | None = None
    ) -> SimpleNamespace:
        del request, csrf
        return SimpleNamespace(user_id=self.profile.user_id)

    def require_profile(
        self, request: Request, *, csrf: bool | None = None
    ) -> UserProfile:
        self.require_public(request, csrf=csrf)
        return self.profile

    def require_admin(
        self, request: Request, *, csrf: bool | None = None
    ) -> SimpleNamespace:
        del request, csrf
        return SimpleNamespace(audit_actor="admin_session:synthetic")


class _NativeState:
    """记录所有原生请求，以检查真正使用的模型和资料范围。"""

    def __init__(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}
        self.public_kbs = {"kb-old", "kb-new"}
        self.full_access = False
        self.requests: list[httpx.Request] = []
        self.fail_create = False
        self.missing_intent_template = False
        self.session_count = 0

    def respond(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911, PLR0912 - 测试替身逐条模拟固定接口。
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "test-admin-token"}
            )
        if path == "/api/v1/tenants/kv/prompt-templates":
            intent_ids = (
                "greeting",
                "chitchat",
                "follow_up",
                "image_only",
                "summarize",
                "web_search",
                "doc_only",
            )
            if self.missing_intent_template:
                intent_ids = intent_ids[:-1]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "intent_prompts": [
                            {
                                "id": intent_id,
                                "content": (
                                    "You are WeKnora, developed by Tencent.\n"
                                    f"Native instructions for {intent_id}."
                                ),
                            }
                            for intent_id in intent_ids
                        ]
                    },
                },
            )
        if path == "/api/v1/tenants/8/api-keys":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "api_key": "test-only-key",
                            "scope_type": "tenant",
                            "full_access": self.full_access,
                            "knowledge_base_ids": sorted(self.public_kbs),
                            "capabilities": ["chat", "message_history"],
                            "expires_at": None,
                        }
                    ],
                },
            )
        if path == "/api/v1/knowledge-bases" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [{"id": item} for item in sorted(self.public_kbs)],
                },
            )
        if path.startswith("/api/v1/knowledge-bases/"):
            kb_id = path.rsplit("/", maxsplit=1)[-1]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": kb_id,
                        "tenant_id": 8,
                        "is_temporary": False,
                    },
                },
            )
        if path.startswith("/api/v1/models/"):
            model_id = path.rsplit("/", maxsplit=1)[-1]
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "id": model_id,
                        "tenant_id": 8,
                        "type": (
                            "Rerank"
                            if model_id == "rerank-1"
                            else "KnowledgeQA"
                        ),
                        "status": "active",
                    },
                },
            )
        if path == "/api/v1/agents" and request.method == "POST":
            if self.fail_create:
                return httpx.Response(
                    503, json={"success": False, "message": "unavailable"}
                )
            agent_id = _AGENT_ID_A if not self.agents else _AGENT_ID_B
            payload = json.loads(request.content)
            agent = {
                "id": agent_id,
                "tenant_id": 8,
                "config": payload["config"],
            }
            self.agents[agent_id] = agent
            return httpx.Response(201, json={"success": True, "data": agent})
        if path.startswith("/api/v1/agents/"):
            if request.headers.get("X-API-Key") == "test-only-key":
                assert request.headers.get("X-External-User-Token")
            agent_id = path.rsplit("/", maxsplit=1)[-1]
            agent = self.agents.get(agent_id)
            return httpx.Response(
                200 if agent else 404,
                json={"success": bool(agent), "data": agent},
            )
        if path == "/api/v1/sessions":
            self.session_count += 1
            return httpx.Response(
                201,
                json={
                    "success": True,
                    "data": {"id": f"native-s{self.session_count}"},
                },
            )
        if path.startswith("/api/v1/knowledge-chat/native-s"):
            return httpx.Response(
                200,
                content=_NATIVE_SSE,
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(f"unexpected native route: {path}")


def _answer(model_id: str) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "rerank_model_id": "rerank-1",
        "system_prompt_id": "default_kb",
        "system_prompt": "已核实系统提示",
        "context_template_id": "default_context",
        "context_template": "已核实上下文模板",
        "temperature": 0.2,
        "max_completion_tokens": 2048,
        "thinking": False,
        "citation_enabled": True,
        "multi_turn_enabled": True,
        "history_turns": 6,
        "embedding_top_k": 10,
        "keyword_threshold": 0.3,
        "vector_threshold": 0.5,
        "rerank_top_k": 5,
        "rerank_threshold": 0.0,
        "enable_rewrite": True,
        "enable_query_expansion": False,
        "rewrite_prompt_system": "已核实改写系统提示",
        "rewrite_prompt_user": "已核实改写用户模板",
        "fallback_strategy": "fixed",
        "fallback_response": "无匹配资料",
        "fallback_prompt": "",
    }


def _save_body(
    *, revision: int, model_id: str, kb_ids: list[str]
) -> dict[str, Any]:
    return {
        "expected_revision": revision,
        "migration_confirmed": True,
        "knowledge_base_ids": kb_ids,
        "answer_settings": _answer(model_id),
        "page_settings": {
            "welcome_text": "欢迎使用湾事通",
            "input_placeholder": "请提问",
            "show_recommendations": True,
            "show_history": True,
            "allow_feedback": True,
            "allow_source_download": True,
        },
    }


def _app(
    tmp_path: Path,
    state: _NativeState,
    store: GatewayStore,
    profile: UserProfile | None = None,
) -> FastAPI:
    native = WeKnoraClient(
        base_url="http://native.test:8080",
        api_key="test-only-key",
        signer=ExternalPrincipalSigner(
            secret="test-only-signing",  # noqa: S106
            tenant_id=8,
            deployment_id="candidate",
        ),
        transport=httpx.MockTransport(state.respond),
    )
    admin = NativeAdminClient(
        base_url="http://native.test:8080",
        email="candidate@example.test",
        password="test-only-password",  # noqa: S106
        tenant_id=8,
        transport=httpx.MockTransport(state.respond),
    )
    engine = EngineSettings(
        base_url="http://native.test:8080",
        tenant_id=8,
        public_kb_ids=("kb-old",),
        api_key_file=tmp_path / "unused-key",
        external_signing_key_file=tmp_path / "unused-signing",
        admin_email="candidate@example.test",
        admin_password_file=tmp_path / "unused-password",
    )
    return gateway_app.create_app(
        auth=cast(
            GatewayAuth, _FakeAuth(tmp_path / "gateway.sqlite3", profile)
        ),
        store=store,
        engine=engine,
        native=native,
        admin_native=admin,
    )


class _ScopeNativeState(_NativeState):
    """提供私库解析状态和历史接口，直接检查最终原生检索目标。"""

    pending = False

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/knowledge"):
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "scoped-document",
                            "parse_status": (
                                "pending" if self.pending else "completed"
                            ),
                        }
                    ],
                },
            )
        if path.startswith("/api/v1/messages/"):
            self.requests.append(request)
            return httpx.Response(200, json={"success": True, "data": []})
        return super().respond(request)


def _scope_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: UserProfile
) -> tuple[TestClient, _ScopeNativeState, GatewayStore]:
    monkeypatch.setattr(gateway_app, "register_auth_routes", lambda *_: None)
    state = _ScopeNativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    app = _app(tmp_path, state, store, profile)
    with sqlite3.connect(tmp_path / "gateway.sqlite3") as connection:
        connection.executemany(
            "INSERT INTO library_kbs(kb_id,scope,owner_id) VALUES (?,?,?)",
            [
                ("own-private", "personal", profile.user_id),
                ("other-private", "personal", "another-user"),
                ("shared", "public", ""),
                ("hr-public", "hr_public", ""),
                ("hr-secret", "hr_internal", ""),
            ],
        )
    return TestClient(app), state, store


@pytest.mark.parametrize(
    ("scopes", "expected", "expected_scopes"),
    [
        (None, {"kb-old", "shared", "hr-public"}, ["public"]),
        (["public"], {"kb-old", "shared", "hr-public"}, ["public"]),
        (["hr_public"], {"hr-public"}, ["hr_public"]),
        (["personal"], {"own-private"}, ["personal"]),
        (
            ["personal", "public"],
            {"kb-old", "shared", "hr-public", "own-private"},
            ["public", "personal"],
        ),
        (
            ["personal", "hr_public"],
            {"hr-public", "own-private"},
            ["hr_public", "personal"],
        ),
        (
            ["hr_public", "public"],
            {"kb-old", "shared", "hr-public"},
            ["public"],
        ),
    ],
)
def test_selected_scopes_are_the_actual_native_request_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scopes: list[str] | None,
    expected: set[str],
    expected_scopes: list[str],
) -> None:
    browser, state, store = _scope_browser(
        tmp_path, monkeypatch, UserProfile("u1", "u1", "甲", None, "user")
    )
    with browser:
        body = {"conversation_id": "scope-test", "query": "查找这份资料"}
        if scopes is not None:
            body["search_scopes"] = scopes
        response = browser.post("/kb/api/public/chat", json=body)
        assert response.status_code == 200, response.text
        requests = [
            request
            for request in state.requests
            if request.url.path.startswith("/api/v1/knowledge-chat/")
        ]
        assert len(requests) == 1
        targets = json.loads(requests[0].content)["knowledge_base_ids"]
        assert set(targets) == expected
        assert len(targets) == len(expected)
        turn = store.get_turn(trace_id=response.headers["X-Trace-Id"])
        assert set(json.loads(turn["kb_scope_json"])) == expected
        history = browser.get("/kb/api/public/conversations/scope-test").json()
        assert history["turns"][0]["search_scopes"] == expected_scopes


@pytest.mark.parametrize(
    ("role", "is_super", "expected_status"),
    [
        ("user", False, 403),
        ("admin", False, 403),
        ("hr", False, 200),
        ("admin", True, 200),
    ],
)
@pytest.mark.parametrize(
    ("scopes", "expected_targets"),
    [
        (["hr_internal"], {"hr-secret"}),
        (["hr_public", "hr_internal"], {"hr-public", "hr-secret"}),
        (
            ["hr_internal", "personal", "hr_public", "public"],
            {"kb-old", "shared", "hr-public", "own-private", "hr-secret"},
        ),
    ],
)
def test_hr_internal_selection_requires_hr_or_super_admin(  # noqa: PLR0913, PLR0917 - 角色和范围矩阵显式参数化。
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    is_super: bool,
    expected_status: int,
    scopes: list[str],
    expected_targets: set[str],
) -> None:
    profile = UserProfile("u1", "u1", "甲", None, cast(Any, role), is_super)
    browser, state, _ = _scope_browser(tmp_path, monkeypatch, profile)
    with browser:
        capabilities = browser.get("/kb/api/public/capabilities").json()
        available = {item["id"] for item in capabilities["search_scopes"]}
        assert "hr_public" in available
        assert ("hr_internal" in available) == (expected_status == 200)
        assert capabilities["default_search_scopes"] == ["public"]
        response = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "hr-scope",
                "query": "查询人力内部资料",
                "search_scopes": scopes,
            },
        )
        assert response.status_code == expected_status, response.text
        requests = [
            request
            for request in state.requests
            if request.url.path.startswith("/api/v1/knowledge-chat/")
        ]
        if expected_status == 200:
            targets = json.loads(requests[0].content)["knowledge_base_ids"]
            assert set(targets) == expected_targets
            assert len(targets) == len(expected_targets)
        else:
            assert requests == []


def test_scope_change_needs_new_native_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, state, _ = _scope_browser(
        tmp_path, monkeypatch, UserProfile("u1", "u1", "甲", None, "user")
    )
    with browser:
        assert (
            browser.post(
                "/kb/api/public/chat",
                json={
                    "conversation_id": "private-chat",
                    "query": "个人问题",
                    "search_scopes": ["personal"],
                },
            ).status_code
            == 200
        )
        rejected = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "private-chat",
                "query": "只查公共",
                "search_scopes": ["public"],
            },
        )
        assert rejected.status_code == 409
        assert "新建对话" in rejected.json()["detail"]
        assert state.session_count == 1
        assert (
            browser.post(
                "/kb/api/public/chat",
                json={
                    "conversation_id": "public-chat",
                    "query": "只查公共",
                    "search_scopes": ["public"],
                },
            ).status_code
            == 200
        )
        assert state.session_count == 2


def test_hr_public_history_keeps_narrow_scope_until_new_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, state, store = _scope_browser(
        tmp_path, monkeypatch, UserProfile("u1", "u1", "甲", None, "user")
    )
    with browser:
        first = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "hr-public-chat",
                "query": "只查公开人力制度",
                "search_scopes": ["hr_public"],
            },
        )
        assert first.status_code == 200
        continued = browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "hr-public-chat", "query": "继续问"},
        )
        assert continued.status_code == 200
        assert state.session_count == 1
        assert store.conversation_search_scopes(
            deployment_id="candidate",
            owner_id="rdms:candidate:u1",
            conversation_id="hr-public-chat",
        ) == ("hr_public",)
        history = browser.get(
            "/kb/api/public/conversations/hr-public-chat"
        ).json()
        assert all(
            turn["search_scopes"] == ["hr_public"] for turn in history["turns"]
        )
        rejected = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "hr-public-chat",
                "query": "改查全部公共",
                "search_scopes": ["public", "hr_public"],
            },
        )
        assert rejected.status_code == 409
        requests = [
            request
            for request in state.requests
            if request.url.path.startswith("/api/v1/knowledge-chat/")
        ]
        assert len(requests) == 2
        assert all(
            json.loads(request.content)["knowledge_base_ids"] == ["hr-public"]
            for request in requests
        )


@pytest.mark.parametrize("scope", ["personal", "hr_public"])
def test_narrow_scope_waits_for_parsing_without_public_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    browser, state, _ = _scope_browser(
        tmp_path, monkeypatch, UserProfile("u1", "u1", "甲", None, "user")
    )
    state.pending = True
    with browser:
        response = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "pending-private",
                "query": "找个人文档",
                "search_scopes": [scope],
            },
        )
        assert response.status_code == 409
        assert "解析" in response.json()["detail"]
        assert state.session_count == 0


def test_hr_public_without_registered_library_has_no_public_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, state, _ = _scope_browser(
        tmp_path, monkeypatch, UserProfile("u1", "u1", "甲", None, "user")
    )
    with sqlite3.connect(tmp_path / "gateway.sqlite3") as connection:
        connection.execute("DELETE FROM library_kbs WHERE scope='hr_public'")
    with browser:
        response = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "empty-hr-public",
                "query": "公开人力制度",
                "search_scopes": ["hr_public"],
            },
        )
        assert response.status_code == 409
        assert state.session_count == 0
        assert not any(
            request.url.path.startswith("/api/v1/knowledge-chat/")
            for request in state.requests
        )


@pytest.mark.parametrize("scopes", [[], ["other-private"], ["all"]])
def test_browser_cannot_inject_knowledge_base_ids_as_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scopes: list[str],
) -> None:
    browser, state, _ = _scope_browser(
        tmp_path, monkeypatch, UserProfile("u1", "u1", "甲", None, "user")
    )
    with browser:
        response = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "bad-scope",
                "query": "查资料",
                "search_scopes": scopes,
            },
        )
        assert response.status_code == 422
        assert state.session_count == 0


def test_legacy_conversation_preserves_scope_and_refuses_narrowed_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, state, store = _scope_browser(
        tmp_path, monkeypatch, UserProfile("u1", "u1", "甲", None, "user")
    )
    owner_id = "rdms:candidate:u1"
    store.bind_session(
        deployment_id="candidate",
        owner_id=owner_id,
        conversation_id="legacy",
        native_session_id="native-s-old",
    )
    store.start_turn(
        trace_id="old-turn",
        deployment_id="candidate",
        owner_id=owner_id,
        conversation_id="legacy",
        native_session_id="native-s-old",
        question="旧问题",
        kb_scope=("kb-old", "own-private"),
    )
    with browser:
        rejected = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "legacy",
                "query": "只查公共",
                "search_scopes": ["public"],
            },
        )
        assert rejected.status_code == 409
        assert state.session_count == 0
        accepted = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "legacy",
                "query": "沿用之前的范围",
            },
        )
        assert accepted.status_code == 200
        assert store.conversation_search_scopes(
            deployment_id="candidate",
            owner_id=owner_id,
            conversation_id="legacy",
        ) == ("public", "personal")


def test_legacy_hr_public_hit_still_means_original_public_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser, state, store = _scope_browser(
        tmp_path, monkeypatch, UserProfile("u1", "u1", "甲", None, "user")
    )
    owner_id = "rdms:candidate:u1"
    store.bind_session(
        deployment_id="candidate",
        owner_id=owner_id,
        conversation_id="legacy-hr",
        native_session_id="legacy-native-hr",
    )
    store.start_turn(
        trace_id="legacy-hr-turn",
        deployment_id="candidate",
        owner_id=owner_id,
        conversation_id="legacy-hr",
        native_session_id="legacy-native-hr",
        question="原公共问答仅命中人力制度",
        kb_scope=("hr-public",),
    )
    with browser:
        narrowed = browser.post(
            "/kb/api/public/chat",
            json={
                "conversation_id": "legacy-hr",
                "query": "改为只查人力",
                "search_scopes": ["hr_public"],
            },
        )
        assert narrowed.status_code == 409
        continued = browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "legacy-hr", "query": "沿用原公共范围"},
        )
        assert continued.status_code == 200
        assert store.conversation_search_scopes(
            deployment_id="candidate",
            owner_id=owner_id,
            conversation_id="legacy-hr",
        ) == ("public",)
        requests = [
            request
            for request in state.requests
            if request.url.path.startswith("/api/v1/knowledge-chat/")
        ]
        assert len(requests) == 1
        assert set(json.loads(requests[0].content)["knowledge_base_ids"]) == {
            "kb-old",
            "shared",
            "hr-public",
        }


@pytest.mark.parametrize("super_admin", [False, True])
def test_admin_ops_respect_hr_internal_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    super_admin: bool,
) -> None:
    profile = UserProfile("u1", "u1", "管理员", None, "admin", super_admin)
    browser, _, store = _scope_browser(tmp_path, monkeypatch, profile)
    for name, scope in (("public", "kb-old"), ("secret", "hr-secret")):
        store.start_turn(
            trace_id="trace-" + name,
            deployment_id="candidate",
            owner_id="rdms:candidate:someone",
            conversation_id=name,
            native_session_id="native-" + name,
            question=name + " question",
            kb_scope=(scope,),
        )
        store.save_feedback(
            trace_id="trace-" + name,
            owner_id="rdms:candidate:someone",
            useful=False,
            reason_code=None,
            reason_detail=None,
            comment="note",
        )
    with browser:
        expected = 2 if super_admin else 1
        traces = browser.get("/kb/api/admin/ops/traces").json()
        assert traces["total"] == expected and len(traces["items"]) == expected
        feedback = browser.get("/kb/api/admin/ops/feedback").json()
        assert len(feedback["items"]) == expected
        questions = browser.get("/kb/api/admin/ops/questions").json()
        assert len(questions["items"]) == expected
        summary = browser.get("/kb/api/admin/ops/summary").json()
        assert summary["turns"] == expected
        detail = browser.get("/kb/api/admin/ops/traces/trace-secret")
        assert detail.status_code == (200 if super_admin else 404)
        exported = browser.post(
            "/kb/api/admin/ops/traces/trace-secret/export",
            json={
                "include_content": True,
                "confirm_id": "trace-secret",
            },
        )
        assert exported.status_code == (200 if super_admin else 404)
        bulk = browser.post(
            "/kb/api/admin/ops/traces/export",
            json={
                "include_content": True,
                "confirm_id": "candidate",
            },
        )
        assert bulk.status_code == 200
        assert ("secret question" in bulk.text) is super_admin


def test_custom_intent_prompts_survive_save_readback_and_page_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    custom = {
        "chitchat": "你是湾小度。\n闲聊自然接话，适度正向，不因缺资料而拒绝。",
        "follow_up": "你是湾小度。\n闲聊追问承接语气，业务事实仍须有依据。",
    }
    with TestClient(_app(tmp_path, state, store)) as browser:
        body = _save_body(revision=0, model_id="model-a", kb_ids=["kb-old"])
        body["answer_settings"]["intent_prompts"] = custom
        saved = browser.put("/kb/api/admin/public-app", json=body)
        assert saved.status_code == 200, saved.text
        current = browser.get("/kb/api/admin/public-app").json()
        prompts = current["answer_settings"]["intent_prompts"]
        for intent_id, content in custom.items():
            assert prompts[intent_id] == content
        assert "Native instructions for greeting." in prompts["greeting"]
        page = dict(current["page_settings"])
        page["input_placeholder"] = "修改页面而不重置闲聊风格"
        resaved = browser.put(
            "/kb/api/admin/public-app",
            json={
                "expected_revision": current["revision"],
                "knowledge_base_ids": current["knowledge_base_ids"],
                "answer_settings": current["answer_settings"],
                "page_settings": page,
            },
        )
        assert resaved.status_code == 200, resaved.text
        readback = browser.get("/kb/api/admin/public-app").json()
        assert readback["answer_settings"]["intent_prompts"] == prompts
        assert (
            state.agents[readback["native_agent_id"]]["config"][
                "intent_prompts"
            ]
            == prompts
        )


@pytest.mark.parametrize("custom", [{"unknown": "不应发布"}, {"chitchat": " "}])
def test_invalid_intent_override_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom: dict[str, str]
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    with TestClient(_app(tmp_path, state, store)) as browser:
        body = _save_body(revision=0, model_id="model-a", kb_ids=["kb-old"])
        body["answer_settings"]["intent_prompts"] = custom
        response = browser.put("/kb/api/admin/public-app", json=body)
        assert response.status_code == 422
        assert state.agents == {}
        assert store.public_app(deployment_id="candidate") is None


def test_publish_model_and_kb_changes_next_public_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    with TestClient(_app(tmp_path, state, store)) as browser:
        unbound = browser.get("/kb/api/admin/public-app").json()
        assert unbound["status"] == "MIGRATION_REQUIRED"
        assert unbound["legacy_kb_ids"] == ["kb-old"]
        legacy = browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "legacy", "query": "旧范围过渡"},
        )
        assert legacy.status_code == 200
        legacy_chat = next(
            request
            for request in state.requests
            if request.url.path.startswith("/api/v1/knowledge-chat/native-s")
        )
        assert json.loads(legacy_chat.content) == {
            "query": "旧范围过渡",
            "knowledge_base_ids": ["kb-old"],
        }
        unconfirmed = _save_body(
            revision=0, model_id="model-a", kb_ids=["kb-old"]
        )
        unconfirmed["migration_confirmed"] = False
        assert (
            browser.put(
                "/kb/api/admin/public-app",
                json=unconfirmed,
            ).status_code
            == 422
        )
        saved = browser.put(
            "/kb/api/admin/public-app",
            json=_save_body(revision=0, model_id="model-a", kb_ids=["kb-old"]),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "ACTIVE"
        assert saved.json()["model_id"] == "model-a"
        assert (
            browser.put(
                f"/kb/api/engine-admin/api/v1/agents/{_AGENT_ID_A}",
                json={"name": "越过公共应用管理"},
            ).status_code
            == 403
        )
        assert (
            browser.post(
                "/kb/api/engine-admin/api/v1/agents",
                json={"name": "绕过统一配置"},
            ).status_code
            == 403
        )
        assert (
            browser.get(
                "/kb/api/engine-admin/api/v1/tenants/8/api-keys"
            ).status_code
            == 403
        )
        browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "one", "query": "问题一"},
        )
        changed = browser.put(
            "/kb/api/admin/public-app",
            json=_save_body(revision=1, model_id="model-b", kb_ids=["kb-new"]),
        )
        assert changed.status_code == 200
        assert changed.json()["model_id"] == "model-b"
        assert changed.json()["revision"] == 2
        browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "two", "query": "问题二"},
        )
        chats = [
            json.loads(request.content)
            for request in state.requests
            if request.url.path.startswith("/api/v1/knowledge-chat/native-s")
        ][1:]
        assert chats == [
            {
                "query": "问题一",
                "knowledge_base_ids": ["kb-old"],
                "agent_id": _AGENT_ID_A,
            },
            {
                "query": "问题二",
                "knowledge_base_ids": ["kb-new"],
                "agent_id": _AGENT_ID_B,
            },
        ]
        assert state.agents[_AGENT_ID_A]["config"]["model_id"] == "model-a"
        assert state.agents[_AGENT_ID_B]["config"]["model_id"] == "model-b"
        intent_prompts = state.agents[_AGENT_ID_A]["config"]["intent_prompts"]
        assert set(intent_prompts) == {
            "greeting",
            "chitchat",
            "follow_up",
            "image_only",
            "summarize",
            "web_search",
            "doc_only",
        }
        assert intent_prompts["chitchat"] == (
            "你是湾小度，中国移动（广东）湾区研究院（GMCII）的知识库问答助手。\n"
            "Native instructions for chitchat."
        )
        assert (
            state.agents[_AGENT_ID_A]["config"]["system_prompt"]
            == "已核实系统提示"
        )
        assert browser.get("/kb/api/admin/public-app").json()["revision"] == 2
    with TestClient(_app(tmp_path, state, store)) as restarted:
        assert (
            restarted.get("/kb/api/admin/public-app").json()["model_id"]
            == "model-b"
        )
        assert store.public_app(deployment_id="candidate")["revision"] == 2


def test_publish_rejects_conflicts_key_mismatch_and_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    with TestClient(_app(tmp_path, state, store)) as browser:
        empty = _save_body(revision=0, model_id="model-a", kb_ids=[])
        assert (
            browser.put("/kb/api/admin/public-app", json=empty).status_code
            == 422
        )
        state.public_kbs.remove("kb-new")
        ungranted = _save_body(
            revision=0, model_id="model-a", kb_ids=["kb-new"]
        )
        assert (
            browser.put("/kb/api/admin/public-app", json=ungranted).status_code
            == 422
        )
        assert not state.agents
        first = _save_body(revision=0, model_id="model-a", kb_ids=["kb-old"])
        assert (
            browser.put("/kb/api/admin/public-app", json=first).status_code
            == 200
        )
        assert (
            browser.put("/kb/api/admin/public-app", json=first).status_code
            == 409
        )
        state.fail_create = True
        replacement = _save_body(
            revision=1, model_id="model-b", kb_ids=["kb-old"]
        )
        assert (
            browser.put(
                "/kb/api/admin/public-app", json=replacement
            ).status_code
            == 502
        )
        assert store.public_app(deployment_id="candidate")["revision"] == 1
        assert (
            browser.get("/kb/api/admin/public-app").json()["model_id"]
            == "model-a"
        )
        state.public_kbs.remove("kb-old")
        assert (
            browser.post(
                "/kb/api/public/chat",
                json={"conversation_id": "key-gap", "query": "问题"},
            ).status_code
            == 503
        )
        state.public_kbs.add("kb-old")
        state.agents[_AGENT_ID_A]["config"]["model_id"] = "external-change"
        assert (
            browser.get("/kb/api/admin/public-app").json()["status"]
            == "DRIFTED"
        )
        before = len(state.requests)
        response = browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "one", "query": "问题"},
        )
        assert response.status_code == 503
        assert not any(
            request.url.path == "/api/v1/sessions"
            for request in state.requests[before:]
        )


def test_publish_requires_scoped_key_and_prompt_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    with TestClient(_app(tmp_path, state, store)) as browser:
        state.full_access = True
        body = _save_body(revision=0, model_id="model-a", kb_ids=["kb-old"])
        assert (
            browser.put("/kb/api/admin/public-app", json=body).status_code
            == 422
        )
        state.full_access = False
        body["answer_settings"]["rewrite_prompt_system"] = ""
        assert (
            browser.put("/kb/api/admin/public-app", json=body).status_code
            == 422
        )
        assert store.public_app(deployment_id="candidate") is None
        assert not state.agents


def test_publish_does_not_replace_agent_when_intent_templates_are_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    state.missing_intent_template = True
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    with TestClient(_app(tmp_path, state, store)) as browser:
        response = browser.put(
            "/kb/api/admin/public-app",
            json=_save_body(revision=0, model_id="model-a", kb_ids=["kb-old"]),
        )
        assert response.status_code == 502
        assert store.public_app(deployment_id="candidate") is None
        assert not state.agents


@pytest.mark.parametrize(
    "first_line",
    [
        (
            "You are WeKnora, a knowledge-base question-answering assistant "
            "developed by Tencent."
        ),
        "你是湾事通（Wanshitong），中国移动（广东）湾区研究院（GMCII）的知识库问答助手。",
    ],
)
def test_loading_existing_prompt_uses_new_public_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_line: str
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    body = _save_body(revision=0, model_id="model-a", kb_ids=["kb-old"])
    body["answer_settings"]["system_prompt"] = (
        f"{first_line}\nKeep the native grounding guidance."
    )
    with TestClient(_app(tmp_path, state, store)) as browser:
        response = browser.put("/kb/api/admin/public-app", json=body)
        assert response.status_code == 200, response.text
        assert state.agents[_AGENT_ID_A]["config"]["system_prompt"] == (
            "你是湾小度，中国移动（广东）湾区研究院（GMCII）的知识库问答助手。\n"
            "Keep the native grounding guidance."
        )


def test_drift_requires_explicit_rebuild_without_reusing_old_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    with TestClient(_app(tmp_path, state, store)) as browser:
        first = _save_body(revision=0, model_id="model-a", kb_ids=["kb-old"])
        assert (
            browser.put("/kb/api/admin/public-app", json=first).status_code
            == 200
        )
        state.agents[_AGENT_ID_A]["config"]["web_search_enabled"] = True
        assert (
            browser.get("/kb/api/admin/public-app").json()["status"]
            == "DRIFTED"
        )
        replacement = _save_body(
            revision=1, model_id="model-b", kb_ids=["kb-new"]
        )
        replacement["migration_confirmed"] = False
        assert (
            browser.put(
                "/kb/api/admin/public-app", json=replacement
            ).status_code
            == 409
        )
        assert len(state.agents) == 1
        replacement["migration_confirmed"] = True
        repaired = browser.put("/kb/api/admin/public-app", json=replacement)
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["native_agent_id"] == _AGENT_ID_B
        assert repaired.json()["revision"] == 2
        assert (
            browser.get("/kb/api/admin/public-app").json()["status"] == "ACTIVE"
        )


def test_page_switches_control_public_response_and_server_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gateway_app, "register_auth_routes", lambda _app, _auth: None
    )
    state = _NativeState()
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    with TestClient(_app(tmp_path, state, store)) as browser:
        first = _save_body(revision=0, model_id="model-a", kb_ids=["kb-old"])
        assert (
            browser.put("/kb/api/admin/public-app", json=first).status_code
            == 200
        )
        disabled = _save_body(revision=1, model_id="model-a", kb_ids=["kb-old"])
        disabled["page_settings"].update(
            show_recommendations=False,
            allow_feedback=False,
            allow_source_download=False,
        )
        assert (
            browser.put("/kb/api/admin/public-app", json=disabled).status_code
            == 200
        )
        assert len(state.agents) == 1
        capabilities = browser.get("/kb/api/public/capabilities").json()
        assert capabilities["page_settings"]["welcome_text"] == "欢迎使用湾事通"
        assert capabilities["feedback"] is False
        assert (
            browser.get("/kb/api/public/popular-questions").json()["items"]
            == []
        )
        assert (
            browser.post(
                "/kb/api/public/feedback",
                json={"trace_id": "trace_" + "a" * 32, "useful": False},
            ).status_code
            == 403
        )
        assert (
            browser.get(
                "/kb/api/public/conversations/one/turns/trace/"
                "references/reference/original"
            ).status_code
            == 403
        )
