"""验证原生工作台身份、范围、代理和撤权的实际 HTTP 边界。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException

from wanshitong_gateway.compat.wanshitong.sso_session import SsoIdentity
from wanshitong_gateway.library import LibraryService
from wanshitong_gateway.roles import (
    UserProfile,
    UserStore,
    can_access_hr_internal,
)
from wanshitong_gateway.weknora.admin import NativeAdminResponse
from wanshitong_gateway.weknora.client import NativeHttpError
from wanshitong_gateway.weknora.workspaces import (
    NativeWorkspaceAccount,
    NativeWorkspaceClient,
)
from wanshitong_gateway.workbench import WorkbenchService

_SERVICE_EMAIL = "service@example.test"
_SOURCE = 10000


class _Library:
    """资料来源由现有授权层返回，测试只构造可区分的业务资料区。"""

    public: tuple[str, ...] = ("public-kb", "hr-public-kb")

    def search_kbs(
        self, profile: UserProfile, scopes: tuple[str, ...]
    ) -> tuple[str, ...]:
        if "hr_internal" in scopes and not can_access_hr_internal(profile):
            raise HTTPException(403, "禁止人力内部资料。")
        ids = list(self.public) if "public" in scopes else []
        if "personal" in scopes:
            ids.append("personal-" + profile.user_id)
        if "hr_internal" in scopes:
            ids.append("hr-internal-kb")
        return tuple(ids)


class _Stream(httpx.AsyncByteStream):
    """可观测关闭的 SSE，验证撤权后不会继续交付已经排队的片段。"""

    closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'data: {"type":"agent_query","content":"first"}\n\n'
        yield b'data: {"type":"answer","content":"second"}\n\n'

    async def aclose(self) -> None:
        self.closed = True


class _Native:
    """模拟官方账号/租户/组织分享契约，不替代权限行为的真实验收。"""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {
            _SERVICE_EMAIL: self.user(
                "service", "service", _SERVICE_EMAIL, True
            )
        }
        self.members = {(_SERVICE_EMAIL, _SOURCE): "owner"}
        self.tokens: dict[str, tuple[str, int]] = {}
        self.passwords = {_SERVICE_EMAIL: "test-only"}
        self.next_tenant = 20000
        self.orgs: dict[str, dict[str, Any]] = {}
        self.shares: dict[str, set[str]] = {}
        self.configs: dict[int, dict[str, Any]] = {}
        self.agents: dict[tuple[int, str], dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, str, int, dict[str, Any]]] = []
        self.streams: list[_Stream] = []
        self.fail_pause = False
        self.fail_revoke = False
        self.reject_template_override = False
        self.models = [
            {
                "id": "chat-builtin",
                "type": "KnowledgeQA",
                "status": "active",
                "is_builtin": True,
            },
            {
                "id": "rerank-builtin",
                "type": "Rerank",
                "status": "active",
                "is_builtin": True,
            },
        ]

    @staticmethod
    def user(
        identifier: str, name: str, email: str, privileged: bool = False
    ) -> dict[str, Any]:
        return {
            "id": identifier,
            "username": name,
            "email": email,
            "is_system_admin": privileged,
            "can_access_all_tenants": privileged,
        }

    @staticmethod
    def ok(data: object, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"success": True, "data": data})

    def login(self, email: str, tenant: int) -> httpx.Response:
        token = f"jwt-{len(self.tokens)}"
        self.tokens[token] = email, tenant
        return httpx.Response(
            200,
            json={
                "success": True,
                "token": token,
                "refresh_token": "hidden-refresh",
                "user": self.users[email],
                "active_tenant": {"id": tenant},
                "memberships": [
                    {"tenant_id": key[1], "role": role}
                    for key, role in self.members.items()
                    if key[0] == email
                ],
            },
        )

    def respond(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911, PLR0912, PLR0915 - 明确模拟官方 HTTP 分支。
        path = request.url.path.removeprefix("/api/v1/")
        body = json.loads(request.content) if request.content else {}
        method = request.method
        if path == "auth/login":
            email = body["email"]
            if (
                email not in self.users
                or body["password"] != self.passwords[email]
            ):
                return httpx.Response(401)
            return self.login(email, _SOURCE if email == _SERVICE_EMAIL else 0)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.tokens:
            return httpx.Response(401)
        email, tenant = self.tokens[token]
        if path == "auth/switch-tenant":
            target = body["tenant_id"]
            if (email, target) not in self.members:
                return httpx.Response(403)
            return self.login(email, target)
        if request.headers.get("x-tenant-id") != str(tenant):
            return httpx.Response(403)
        if (email, tenant) not in self.members:
            return httpx.Response(403)
        self.calls.append((email, method, path, tenant, body))
        if path == "system/admin/users/create":
            assert email == _SERVICE_EMAIL and tenant == _SOURCE
            if not 8 <= len(body["password"]) <= 32:
                return httpx.Response(400, json={"error": "password length"})
            self.users.setdefault(
                body["email"],
                self.user(
                    f"user-{len(self.users)}", body["username"], body["email"]
                ),
            )
            self.passwords.setdefault(body["email"], body["password"])
            return httpx.Response(201, json={"user": self.users[body["email"]]})
        if path == "tenants" and method == "POST":
            assert email == _SERVICE_EMAIL
            self.next_tenant += 1
            self.members[email, self.next_tenant] = "owner"
            return self.ok({"id": self.next_tenant, "name": body["name"]}, 201)
        if path == "organizations" and method == "POST":
            assert tenant == _SOURCE
            identifier = f"org-{len(self.orgs)}"
            self.orgs[identifier] = {"id": identifier, **body}
            self.shares[identifier] = set()
            return self.ok(self.orgs[identifier], 201)
        if path.startswith("organizations/"):
            assert tenant == _SOURCE
            org = self.orgs[path.split("/")[1]]
            org.update(body)
            return self.ok(org)
        if path.endswith("/shares"):
            assert tenant == _SOURCE and email == _SERVICE_EMAIL
            self.shares[body["organization_id"]].add(path.split("/")[1])
            return self.ok({"id": "share-1"}, 201)
        if path.startswith("tenants/") and "/members" in path:
            assert email == _SERVICE_EMAIL
            if method == "POST":
                self.members[body["email"], tenant] = body["role"]
                return self.ok({"role": body["role"]}, 201)
            if self.fail_revoke:
                return httpx.Response(503)
            identifier = path.split("/")[-1]
            for user_email, user in self.users.items():
                if user["id"] == identifier:
                    self.members.pop((user_email, tenant), None)
            return self.ok({})
        if path == "tenants/kv/assistant-config":
            if method == "PUT":
                self.configs[tenant] = body
            return self.ok(self.configs.get(tenant, {}))
        if path == "models":
            if method == "POST":
                model = {"id": f"private-model-{tenant}", **body}
                model["status"] = "active"
                self.models.append(model)
                return self.ok(model, 201)
            return self.ok(self.models)
        if path.startswith("models/"):
            found_model = next(
                (
                    item
                    for item in self.models
                    if item["id"] == path.split("/")[1]
                ),
                None,
            )
            return self.ok(found_model) if found_model else httpx.Response(404)
        if path.startswith("agents/"):
            key = tenant, path.split("/")[1]
            if method == "PUT":
                if (
                    self.reject_template_override
                    and key[1] != "builtin-weknora-assistant"
                ):
                    body["config"]["agent_mode"] = "smart-reasoning"
                self.agents[key] = {"id": key[1], **body}
            agent = self.agents.get(
                key,
                {
                    "id": key[1],
                    "name": key[1],
                    "config": {"agent_mode": "smart-reasoning"},
                },
            )
            # 官方 Config 对空 sandbox_config_id 使用 omitempty。
            visible = {**agent, "config": dict(agent["config"])}
            if not visible["config"].get("sandbox_config_id"):
                visible["config"].pop("sandbox_config_id", None)
            return self.ok(visible)
        if path == "auth/me":
            return self.ok({"user": self.users[email]})
        if path == "sessions":
            if method == "POST":
                identifier = f"session-{len(self.sessions)}"
                session = {
                    "id": identifier,
                    "tenant": tenant,
                    "email": email,
                    **body,
                }
                self.sessions[identifier] = session
                return self.ok(session, 201)
            return self.ok(
                [
                    item
                    for item in self.sessions.values()
                    if item["tenant"] == tenant
                    and item["email"] == email
                    and item.get("kind", "")
                    == request.url.params.get("kind", "")
                ]
            )
        if path.startswith("sessions/"):
            found_session = self.sessions.get(path.split("/")[1])
            if (
                found_session is None
                or found_session["tenant"] != tenant
                or found_session["email"] != email
            ):
                return httpx.Response(404)
            return self.ok(found_session)
        if path.startswith("messages/"):
            return self.ok(self.messages.get(path.split("/")[1], []))
        if path.startswith("agent-chat/"):
            found_session = self.sessions.get(path.split("/")[1])
            if (
                found_session is None
                or found_session["tenant"] != tenant
                or found_session["email"] != email
            ):
                return httpx.Response(404)
            stream = _Stream()
            self.streams.append(stream)
            return httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/event-stream"},
            )
        if path == "automations":
            if method == "POST":
                identifier = f"auto-{len(self.tasks)}"
                self.tasks[identifier] = {
                    "id": identifier,
                    "tenant": tenant,
                    "email": email,
                    **body,
                }
                return self.ok(self.tasks[identifier], 201)
            return self.ok(
                [
                    item
                    for item in self.tasks.values()
                    if item["tenant"] == tenant and item["email"] == email
                ]
            )
        if path.startswith("automations/"):
            return self.automation(request, email, tenant, path)
        if path.startswith("automation-runs/"):
            return self.ok({})
        if path in {"knowledge-bases", "shared-knowledge-bases"}:
            return self.ok(
                [{"id": "public-kb"}, {"id": "unrelated-private-kb"}]
            )
        return self.ok({})

    def automation(
        self, request: httpx.Request, email: str, tenant: int, path: str
    ) -> httpx.Response:
        parts = path.split("/")
        task = self.tasks.get(parts[1])
        if task is None or (task["email"], task["tenant"]) != (email, tenant):
            return httpx.Response(404)
        if parts[-1] == "pause":
            if self.fail_pause:
                return httpx.Response(503)
            task["enabled"] = False
        if parts[-1] == "runs":
            page = int(request.url.params.get("page", "1"))
            size = int(request.url.params.get("page_size", "20"))
            return self.ok(
                self.runs.get(parts[1], [])[(page - 1) * size : page * size]
            )
        return self.ok(task)


_Setup = tuple[WorkbenchService, _Native, UserStore, _Library, Path]


@pytest_asyncio.fixture
async def setup_workbench(
    tmp_path: Path,
) -> AsyncIterator[tuple[WorkbenchService, _Native, UserStore, _Library, Path]]:
    database = tmp_path / "gateway.sqlite3"
    users = UserStore(database)
    for identifier, name in (
        ("325", "taoyifei"),
        ("138", "luoyang"),
        ("139", "other"),
        ("140", "hr"),
    ):
        users.record_identity(SsoIdentity(identifier, name, name, "42", (), ()))
    users.set_role("140", "hr", actor_user_id="325")
    native = _Native()
    library = _Library()
    service = WorkbenchService(
        database,
        "http://native.test",
        _SERVICE_EMAIL,
        "test-only",
        _SOURCE,
        "isolated-test",
        b"master-key-for-tests",
        cast(LibraryService, library),
        users,
        transport=httpx.MockTransport(native.respond),
    )
    yield service, native, users, library, database
    await service.close()


def _profile(users: UserStore, identifier: str = "138") -> UserProfile:
    profile = users.get(identifier)
    assert profile is not None
    return profile


async def _proxy(  # noqa: PLR0913, PLR0917 - 与代理契约相同的测试助手。
    service: WorkbenchService,
    user: UserProfile,
    workspace: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> NativeAdminResponse:
    return await service.proxy(
        user,
        workspace["workspace_id"],
        method,
        "api/v1/" + path,
        "",
        json.dumps(body).encode() if body is not None else b"",
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer forged",
            "X-Tenant-ID": "10000",
            "X-API-Key": "forged",
        },
    )


@pytest.mark.asyncio
async def test_bootstrap_uses_dedicated_viewer_and_never_returns_credentials(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, database = setup_workbench
    user = _profile(users)
    result = await service.bootstrap(user, ("personal", "public"))
    repeated = await service.bootstrap(user, ("public", "personal"))
    assert repeated == result
    assert result["tenant"]["id"] != _SOURCE
    assert result["user"]["role"] == "viewer"
    assert result["user"]["is_system_admin"] is False
    assert result["knowledge_base_ids"] == [
        "hr-public-kb",
        "personal-138",
        "public-kb",
    ]
    assert "token" not in json.dumps(result) and "password" not in json.dumps(
        result
    )
    assert len(native.orgs) == 1
    assert next(iter(native.orgs.values()))["searchable"] is False
    assert next(iter(native.shares.values())) == set(
        result["knowledge_base_ids"]
    )
    for (tenant_id, agent_id), agent in native.agents.items():
        assert tenant_id == result["tenant"]["id"]
        assert agent["config"]["access_level"] == "read_only"
        assert agent["config"]["web_search_enabled"] is False
        if agent_id != "builtin-weknora-assistant":
            assert agent["config"]["agent_mode"] == "quick-answer"
    with sqlite3.connect(database) as connection:
        columns = [
            item[1]
            for item in connection.execute(
                "PRAGMA table_info(workbench_identities)"
            )
        ]
    assert not set(columns).intersection({"password", "token", "refresh_token"})


@pytest.mark.asyncio
async def test_proxy_forces_real_assistant_and_readonly_automation(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    user = _profile(users)
    workspace = await service.bootstrap(user, ("public",))
    session = json.loads(
        (
            await _proxy(
                service,
                user,
                workspace,
                "POST",
                "sessions",
                {"kind": "", "user_id": "other", "tenant_id": _SOURCE},
            )
        ).content
    )["data"]
    stream = await _proxy(
        service,
        user,
        workspace,
        "POST",
        "agent-chat/" + session["id"],
        {
            "query": "请分工阅读资料并比较",
            "agent_id": "forged-agent",
            "agent_source_tenant_id": _SOURCE,
            "assistant_access": "auto_write",
            "web_search_enabled": True,
        },
    )
    assert stream.stream is not None
    chat = next(
        row
        for row in reversed(native.calls)
        if row[2].startswith("agent-chat/")
    )
    assert chat[0] != _SERVICE_EMAIL and chat[3] == workspace["tenant"]["id"]
    assert chat[4]["agent_id"] == "builtin-weknora-assistant"
    assert chat[4]["assistant_access"] == "read_only"
    assert chat[4]["web_search_enabled"] is False
    assert "agent_source_tenant_id" not in chat[4]
    await stream.stream.aclose()
    task_response = await _proxy(
        service,
        user,
        workspace,
        "POST",
        "automations",
        {
            "name": "定期阅读",
            "instruction": "请分工检查资料变动",
            "owner_user_id": "other",
            "access_level": "auto_write",
            "session_mode": "persistent",
            "carry_last_result": True,
        },
    )
    task = json.loads(task_response.content)["data"]
    assert task["access_level"] == "read_only"
    assert (
        task["session_mode"] == "per_run" and task["carry_last_result"] is False
    )
    assert "owner_user_id" not in task


@pytest.mark.asyncio
async def test_other_user_and_cross_scope_session_ids_are_rejected(
    setup_workbench: _Setup,
) -> None:
    service, _, users, _, _ = setup_workbench
    user = _profile(users)
    public = await service.bootstrap(user, ("public",))
    personal = await service.bootstrap(user, ("personal",))
    session = json.loads(
        (await _proxy(service, user, personal, "POST", "sessions")).content
    )["data"]
    with pytest.raises(HTTPException, match="404"):
        await _proxy(
            service, _profile(users, "139"), personal, "GET", "sessions"
        )
    response = await _proxy(
        service, user, public, "GET", "sessions/" + session["id"]
    )
    assert response.status_code == 404
    with pytest.raises(HTTPException, match="403"):
        await _proxy(
            service, user, public, "GET", "knowledge-bases/personal-138"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identifier,allowed",
    [("138", False), ("139", False), ("140", True), ("325", True)],
)
async def test_hr_scope_follows_business_role_not_native_admin(
    setup_workbench: _Setup, identifier: str, allowed: bool
) -> None:
    service, _, users, _, _ = setup_workbench
    if identifier == "139":
        users.set_role("139", "admin", actor_user_id="325")
    if allowed:
        result = await service.bootstrap(
            _profile(users, identifier), ("hr_internal",)
        )
        assert result["knowledge_base_ids"] == ["hr-internal-kb"]
        assert result["user"]["role"] == "viewer"
    else:
        with pytest.raises(HTTPException, match="403"):
            await service.bootstrap(
                _profile(users, identifier), ("hr_internal",)
            )


@pytest.mark.asyncio
async def test_downgrade_closes_stream_pauses_tasks_then_revokes_membership(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    user = _profile(users, "140")
    workspace = await service.bootstrap(user, ("hr_internal",))
    session = json.loads(
        (await _proxy(service, user, workspace, "POST", "sessions")).content
    )["data"]
    task = json.loads(
        (
            await _proxy(
                service,
                user,
                workspace,
                "POST",
                "automations",
                {"name": "daily"},
            )
        ).content
    )["data"]
    native.runs[task["id"]] = [{"id": "run-1", "status": "running"}]
    native.messages[session["id"]] = [
        {"id": "answer-1", "role": "assistant", "is_completed": True}
    ]
    response = await _proxy(
        service,
        user,
        workspace,
        "POST",
        "agent-chat/" + session["id"],
        {"query": "阅读人力资料"},
    )
    assert response.stream is not None
    assert b"first" in await anext(response.stream)
    before = len(native.calls)
    await service.before_role_change(user, "user")
    users.set_role("140", "user", actor_user_id="325")
    assert native.streams[-1].closed
    with pytest.raises(StopAsyncIteration):
        await anext(response.stream)
    after = native.calls[before:]
    revoked = next(
        i
        for i, row in enumerate(after)
        if row[1] == "DELETE" and "/members/" in row[2]
    )
    paused = next(i for i, row in enumerate(after) if row[2].endswith("/pause"))
    cancelled = next(
        i for i, row in enumerate(after) if row[2].endswith("/cancel")
    )
    stopped = next(i for i, row in enumerate(after) if row[2].endswith("/stop"))
    assert max(paused, cancelled, stopped) < revoked
    assert all(row[0] != _SERVICE_EMAIL for row in after[:revoked])
    assert after[revoked][0] == _SERVICE_EMAIL
    assert not any(
        tenant == workspace["tenant"]["id"] and role == "viewer"
        for (_, tenant), role in native.members.items()
    )
    with pytest.raises(HTTPException, match="403"):
        await _proxy(service, user, workspace, "GET", "sessions")


@pytest.mark.asyncio
async def test_failed_task_stop_still_revokes_and_keeps_workspace_disabled(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    user = _profile(users, "140")
    workspace = await service.bootstrap(user, ("hr_internal",))
    await _proxy(
        service, user, workspace, "POST", "automations", {"name": "daily"}
    )
    native.fail_pause = True
    with pytest.raises(NativeHttpError):
        await service.before_role_change(user, "user")
    assert not any(
        tenant == workspace["tenant"]["id"] and role == "viewer"
        for (_, tenant), role in native.members.items()
    )
    with pytest.raises(HTTPException, match="403"):
        await _proxy(service, user, workspace, "GET", "sessions")


@pytest.mark.asyncio
async def test_removed_kb_retires_old_history_and_bootstraps_new_generation(
    setup_workbench: _Setup,
) -> None:
    service, _, users, library, _ = setup_workbench
    user = _profile(users)
    old = await service.bootstrap(user, ("public",))
    library.public = ("replacement-kb",)
    with pytest.raises(HTTPException, match="403"):
        await _proxy(service, user, old, "GET", "sessions")
    new = await service.bootstrap(user, ("public",))
    assert new["generation"] == old["generation"] + 1
    assert new["workspace_id"] != old["workspace_id"]
    assert new["knowledge_base_ids"] == ["replacement-kb"]


@pytest.mark.asyncio
async def test_stale_hr_profile_is_rechecked_on_each_proxy(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    old_profile = _profile(users, "140")
    workspace = await service.bootstrap(old_profile, ("hr_internal",))
    users.set_role("140", "user", actor_user_id="325")
    calls = len(native.calls)
    with pytest.raises(HTTPException, match="403"):
        await _proxy(service, old_profile, workspace, "GET", "sessions")
    assert len(native.calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "auth/login"),
        ("GET", "tenants"),
        ("GET", "organizations"),
        ("PUT", "agents/builtin-weknora-assistant"),
        ("GET", "agents/builtin-data-analyst"),
        ("POST", "knowledge-bases/public-kb/knowledge/file"),
        ("POST", "automations/auto-1/grants"),
        ("GET", "system/admin/users"),
        ("POST", "sessions/session-1/sandbox/terminal-ticket"),
        ("GET", "sessions/%2e%2e"),
    ],
)
async def test_proxy_denies_unadapted_and_write_surfaces(
    setup_workbench: _Setup, method: str, path: str
) -> None:
    service, native, users, _, _ = setup_workbench
    user = _profile(users)
    workspace = await service.bootstrap(user, ("public",))
    before = len(native.calls)
    with pytest.raises(HTTPException, match="403"):
        await _proxy(service, user, workspace, method, path)
    assert len(native.calls) == before


@pytest.mark.asyncio
async def test_template_configuration_must_roundtrip_before_viewer_is_granted(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    native.reject_template_override = True
    with pytest.raises(NativeHttpError):
        await service.bootstrap(_profile(users), ("public",))
    assert all(role != "viewer" for role in native.members.values())


@pytest.mark.asyncio
async def test_identity_and_kb_listing_do_not_expose_other_workspace_metadata(
    setup_workbench: _Setup,
) -> None:
    service, _, users, _, _ = setup_workbench
    user = _profile(users)
    workspace = await service.bootstrap(user, ("public",))
    identity = json.loads(
        (await _proxy(service, user, workspace, "GET", "auth/me")).content
    )["data"]
    assert identity["memberships"] == [
        {"tenant_id": workspace["tenant"]["id"], "role": "viewer"}
    ]
    assert identity["capabilities"]["can_create_tenant"] is False
    listing = json.loads(
        (
            await _proxy(
                service, user, workspace, "GET", "shared-knowledge-bases"
            )
        ).content
    )["data"]
    assert listing == [{"id": "public-kb"}]


@pytest.mark.asyncio
async def test_invalidation_revokes_only_matching_scope_workspaces(
    setup_workbench: _Setup,
) -> None:
    service, _, users, _, _ = setup_workbench
    user = _profile(users)
    public = await service.bootstrap(user, ("public",))
    personal = await service.bootstrap(user, ("personal",))
    await service.invalidate_scopes({"public"})
    with pytest.raises(HTTPException, match="403"):
        await _proxy(service, user, public, "GET", "sessions")
    response = await _proxy(service, user, personal, "GET", "sessions")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_revoke_failure_is_closed_and_can_be_retried(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, database = setup_workbench
    profile = _profile(users, "140")
    workspace = await service.bootstrap(profile, ("hr_internal",))
    native.fail_revoke = True
    with pytest.raises(NativeHttpError):
        await service.before_role_change(profile, "user")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state,revoked FROM workbench_workspaces "
            "WHERE workspace_id=?",
            (workspace["workspace_id"],),
        ).fetchone() == ("disabled", 0)
    with pytest.raises(HTTPException, match="403"):
        await _proxy(service, profile, workspace, "GET", "sessions")
    native.fail_revoke = False
    await service.before_role_change(profile, "user")
    assert not any(
        tenant == workspace["tenant"]["id"] and role == "viewer"
        for (_, tenant), role in native.members.items()
    )


@pytest.mark.asyncio
async def test_cleanup_reads_all_automation_run_pages(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    profile = _profile(users, "140")
    workspace = await service.bootstrap(profile, ("hr_internal",))
    task = json.loads(
        (
            await _proxy(
                service,
                profile,
                workspace,
                "POST",
                "automations",
                {"name": "daily"},
            )
        ).content
    )["data"]
    native.runs[task["id"]] = [
        {"id": f"run-{index}", "status": "skipped"} for index in range(100)
    ] + [{"id": "older-pending-run", "status": "awaiting_approval"}]
    await service.before_role_change(profile, "user")
    assert any(
        row[2] == "automation-runs/older-pending-run/cancel"
        for row in native.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "tenant_id=10000",
        "%74enant_id=10000",
        "user_id=139",
        "resource_urls=public",
        "api_key=forged",
    ],
)
async def test_query_cannot_override_identity_or_make_public_resource_urls(
    setup_workbench: _Setup,
    query: str,
) -> None:
    service, native, users, _, _ = setup_workbench
    profile = _profile(users)
    workspace = await service.bootstrap(profile, ("public",))
    before = len(native.calls)
    with pytest.raises(HTTPException, match="403"):
        await service.proxy(
            profile,
            workspace["workspace_id"],
            "GET",
            "api/v1/sessions",
            query,
            b"",
            {},
        )
    assert len(native.calls) == before


@pytest.mark.asyncio
async def test_chat_cannot_inject_another_private_knowledge_base(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    profile = _profile(users)
    workspace = await service.bootstrap(profile, ("public",))
    session = json.loads(
        (await _proxy(service, profile, workspace, "POST", "sessions")).content
    )["data"]
    before = len(native.calls)
    with pytest.raises(HTTPException, match="403"):
        await _proxy(
            service,
            profile,
            workspace,
            "POST",
            "agent-chat/" + session["id"],
            {"query": "读取个人资料", "knowledge_base_ids": ["personal-139"]},
        )
    assert len(native.calls) == before


@pytest.mark.asyncio
async def test_stream_rechecks_current_role_before_delivering_next_chunk(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    profile = _profile(users, "140")
    workspace = await service.bootstrap(profile, ("hr_internal",))
    session = json.loads(
        (await _proxy(service, profile, workspace, "POST", "sessions")).content
    )["data"]
    response = await _proxy(
        service,
        profile,
        workspace,
        "POST",
        "agent-chat/" + session["id"],
        {"query": "阅读人力资料"},
    )
    assert response.stream is not None
    await anext(response.stream)
    users.set_role("140", "user", actor_user_id="325")
    with pytest.raises(HTTPException, match="403"):
        await anext(response.stream)
    assert native.streams[-1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "violation", ["owner", "system_admin", "cross_tenant", "wrong_user"]
)
async def test_native_login_rejects_an_unexpected_execution_identity(
    setup_workbench: _Setup,
    violation: str,
) -> None:
    service, native, users, _, _ = setup_workbench
    workspace = await service.bootstrap(_profile(users), ("public",))
    identifier = workspace["user"]["id"]
    email = next(
        key for key, value in native.users.items() if value["id"] == identifier
    )
    if violation == "owner":
        native.members[email, workspace["tenant"]["id"]] = "owner"
    elif violation == "system_admin":
        native.users[email]["is_system_admin"] = True
    elif violation == "cross_tenant":
        native.users[email]["can_access_all_tenants"] = True
    else:
        identifier = "wrong-user"
    client = NativeWorkspaceClient(
        "http://native.test",
        _SERVICE_EMAIL,
        "test-only",
        transport=httpx.MockTransport(native.respond),
    )
    try:
        with pytest.raises(NativeHttpError) as caught:
            await client.request_json(
                NativeWorkspaceAccount(
                    email, native.passwords[email], identifier
                ),
                workspace["tenant"]["id"],
                "GET",
                "api/v1/sessions",
            )
        assert caught.value.status_code == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_parallel_workspace_requests_keep_distinct_user_jwts(
    setup_workbench: _Setup,
) -> None:
    service, native, users, _, _ = setup_workbench
    first = await service.bootstrap(_profile(users), ("public",))
    second = await service.bootstrap(_profile(users), ("personal",))
    third = await service.bootstrap(_profile(users, "139"), ("public",))
    before = len(native.calls)
    responses = await asyncio.gather(
        _proxy(service, _profile(users), first, "GET", "sessions"),
        _proxy(service, _profile(users), second, "GET", "sessions"),
        _proxy(service, _profile(users, "139"), third, "GET", "sessions"),
    )
    assert all(response.status_code == 200 for response in responses)
    delivered = {
        (native.users[row[0]]["id"], row[3]) for row in native.calls[before:]
    }
    assert delivered == {
        (workspace["user"]["id"], workspace["tenant"]["id"])
        for workspace in (first, second, third)
    }
