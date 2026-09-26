"""公共应用绑定从后台保存到新问答消费的回归。"""

from __future__ import annotations

import json
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

    def __init__(self, database_path: Path) -> None:
        self.settings = SimpleNamespace(
            database_path=database_path,
            deployment_id="candidate",
            root_path="/kb",
        )

    def require_public(
        self, request: Request, *, csrf: bool | None = None
    ) -> SimpleNamespace:
        del request, csrf
        return SimpleNamespace(user_id="u1")

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
        self.session_count = 0

    def respond(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - 测试替身逐条模拟固定接口。
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "test-admin-token"}
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
    tmp_path: Path, state: _NativeState, store: GatewayStore
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
        auth=cast(GatewayAuth, _FakeAuth(tmp_path / "gateway.sqlite3")),
        store=store,
        engine=engine,
        native=native,
        admin_native=admin,
    )


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
        assert browser.put(
            "/kb/api/admin/public-app",
            json=unconfirmed,
        ).status_code == 422
        saved = browser.put(
            "/kb/api/admin/public-app",
            json=_save_body(revision=0, model_id="model-a", kb_ids=["kb-old"]),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "ACTIVE"
        assert saved.json()["model_id"] == "model-a"
        assert browser.put(
            f"/kb/api/engine-admin/api/v1/agents/{_AGENT_ID_A}",
            json={"name": "越过公共应用管理"},
        ).status_code == 403
        assert browser.post(
            "/kb/api/engine-admin/api/v1/agents",
            json={"name": "绕过统一配置"},
        ).status_code == 403
        assert browser.get(
            "/kb/api/engine-admin/api/v1/tenants/8/api-keys"
        ).status_code == 403
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
        assert browser.put(
            "/kb/api/admin/public-app", json=empty
        ).status_code == 422
        state.public_kbs.remove("kb-new")
        ungranted = _save_body(
            revision=0, model_id="model-a", kb_ids=["kb-new"]
        )
        assert browser.put(
            "/kb/api/admin/public-app", json=ungranted
        ).status_code == 422
        assert not state.agents
        first = _save_body(revision=0, model_id="model-a", kb_ids=["kb-old"])
        assert browser.put(
            "/kb/api/admin/public-app", json=first
        ).status_code == 200
        assert browser.put(
            "/kb/api/admin/public-app", json=first
        ).status_code == 409
        state.fail_create = True
        replacement = _save_body(
            revision=1, model_id="model-b", kb_ids=["kb-old"]
        )
        assert browser.put(
            "/kb/api/admin/public-app", json=replacement
        ).status_code == 502
        assert store.public_app(deployment_id="candidate")["revision"] == 1
        assert (
            browser.get("/kb/api/admin/public-app").json()["model_id"]
            == "model-a"
        )
        state.public_kbs.remove("kb-old")
        assert browser.post(
            "/kb/api/public/chat",
            json={"conversation_id": "key-gap", "query": "问题"},
        ).status_code == 503
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
        assert browser.put(
            "/kb/api/admin/public-app", json=body
        ).status_code == 422
        state.full_access = False
        body["answer_settings"]["rewrite_prompt_system"] = ""
        assert browser.put(
            "/kb/api/admin/public-app", json=body
        ).status_code == 422
        assert store.public_app(deployment_id="candidate") is None
        assert not state.agents


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
        assert browser.put(
            "/kb/api/admin/public-app", json=first
        ).status_code == 200
        state.agents[_AGENT_ID_A]["config"]["web_search_enabled"] = True
        assert browser.get(
            "/kb/api/admin/public-app"
        ).json()["status"] == "DRIFTED"
        replacement = _save_body(
            revision=1, model_id="model-b", kb_ids=["kb-new"]
        )
        replacement["migration_confirmed"] = False
        assert browser.put(
            "/kb/api/admin/public-app", json=replacement
        ).status_code == 409
        assert len(state.agents) == 1
        replacement["migration_confirmed"] = True
        repaired = browser.put(
            "/kb/api/admin/public-app", json=replacement
        )
        assert repaired.status_code == 200, repaired.text
        assert repaired.json()["native_agent_id"] == _AGENT_ID_B
        assert repaired.json()["revision"] == 2
        assert browser.get(
            "/kb/api/admin/public-app"
        ).json()["status"] == "ACTIVE"


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
        assert browser.put(
            "/kb/api/admin/public-app", json=first
        ).status_code == 200
        disabled = _save_body(revision=1, model_id="model-a", kb_ids=["kb-old"])
        disabled["page_settings"].update(
            show_recommendations=False,
            allow_feedback=False,
            allow_source_download=False,
        )
        assert browser.put(
            "/kb/api/admin/public-app", json=disabled
        ).status_code == 200
        assert len(state.agents) == 1
        capabilities = browser.get("/kb/api/public/capabilities").json()
        assert capabilities["page_settings"]["welcome_text"] == "欢迎使用湾事通"
        assert capabilities["feedback"] is False
        assert (
            browser.get("/kb/api/public/popular-questions").json()["items"]
            == []
        )
        assert browser.post(
            "/kb/api/public/feedback",
            json={"trace_id": "trace_" + "a" * 32, "useful": False},
        ).status_code == 403
        assert browser.get(
            "/kb/api/public/conversations/one/turns/trace/"
            "references/reference/original"
        ).status_code == 403
