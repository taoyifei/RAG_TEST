"""把原生助手、协作和自动化约束在当前用户选定的资料范围内。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import HTTPException

from wanshitong_gateway.library import LibraryService
from wanshitong_gateway.roles import (
    Role,
    UserProfile,
    UserStore,
)
from wanshitong_gateway.weknora.admin import (
    NativeAdminResponse,
    NativeAdminStream,
)
from wanshitong_gateway.weknora.client import NativeHttpError
from wanshitong_gateway.weknora.workspaces import (
    NativeWorkspaceAccount,
    NativeWorkspaceClient,
    decode_json,
    native_data,
    native_items,
)
from wanshitong_gateway.workbench_store import (
    WorkbenchIdentity,
    WorkbenchStore,
    WorkbenchWorkspace,
)

_ASSISTANT_ID = "builtin-weknora-assistant"
_WORKBENCH_SYSTEM_PROMPT = (
    "你是湾小度资料助手。只依据当前工作区可见资料回答。"
    "先搜索相关资料，再读取原文核对，回答标明来源；"
    "资料不足时说明缺口，不编造制度和流程。"
    "需要协作时只创建少量继承当前资料范围的子任务，"
    "不指定其他智能体模板，等待结果后汇总。"
    "保持只读，不修改资料和角色，不使用外网。用简洁中文回答。"
)
_SCOPE_LABELS = {
    "public": "公共知识库",
    "personal": "我的资料",
    "hr_internal": "人力内部资料",
}
_READ_TOOLS = [
    "search_knowledge",
    "read_document",
    "list_documents",
]
_TEMPLATE_IDS = (
    "builtin-smart-reasoning",
    "builtin-data-analyst",
    "builtin-wiki-researcher",
)
_ACTIVE_RUNS = {"queued", "running", "awaiting_approval"}
_PAGE_SIZE = 100
_MAX_PAGES = 1000
_HTTP_NOT_FOUND = 404
_HTTP_ERROR = 400
_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (methods, re.compile(pattern))
    for methods, pattern in (
        ("GET", r"auth/me|system/(?:info|capabilities)"),
        ("GET", r"tenants/kv/assistant-config"),
        ("GET", rf"models(?:/{_ID})?"),
        ("GET", rf"agents/{_ASSISTANT_ID}"),
        ("GET POST", r"sessions"),
        ("GET PUT DELETE", rf"sessions/{_ID}"),
        ("GET", rf"sessions/continue-stream/{_ID}"),
        ("POST", rf"sessions/{_ID}/(?:stop|steer|generate_title)"),
        ("DELETE", rf"sessions/{_ID}/steer/{_ID}"),
        ("POST", rf"sessions/{_ID}/steer/{_ID}/inject"),
        ("GET", rf"sessions/{_ID}/(?:steer|background-work|artifacts)"),
        ("GET", rf"sessions/{_ID}/background-work/{_ID}"),
        ("POST", rf"sessions/{_ID}/background-work/{_ID}/cancel"),
        ("GET", rf"sessions/{_ID}/messages/{_ID}/(?:files|artifacts)"),
        ("GET", rf"sessions/{_ID}/messages/{_ID}/artifacts/\d+/download"),
        ("GET", rf"messages/{_ID}/load"),
        ("POST", rf"agent-chat/{_ID}|agent/prompts/{_ID}"),
        ("GET POST", r"automations"),
        ("POST", r"automations/validate-trigger"),
        ("GET", rf"automations/by-session/{_ID}"),
        ("GET PUT DELETE", rf"automations/{_ID}"),
        ("POST", rf"automations/{_ID}/(?:pause|resume|run)"),
        ("GET", rf"automations/{_ID}/runs|automation-runs/{_ID}"),
        ("POST", rf"automation-runs/{_ID}/cancel"),
        ("GET", r"notifications"),
        ("POST DELETE", r"notifications/read"),
        ("GET", r"knowledge-bases|shared-knowledge-bases"),
        ("GET", rf"knowledge-bases/{_ID}"),
        (
            "GET",
            rf"knowledge-bases/{_ID}/(?:knowledge|knowledge/folders|images|files|tags|gallery-config)",
        ),
        ("GET", rf"knowledge/{_ID}(?:/(?:preview|stages|spans))?"),
        ("GET", rf"chunks/{_ID}"),
    )
)
_FORBIDDEN_QUERY = {
    "tenant_id",
    "source_tenant_id",
    "agent_source_tenant_id",
    "user_id",
    "owner_user_id",
    "token",
    "api_key",
    "resource_urls",
}
_SECRET_FIELDS = {
    "token",
    "refresh_token",
    "password",
    "password_hash",
    "api_key",
    "generated_password",
    "client_secret",
    "app_secret",
    "secret",
}


class WorkbenchService:
    """保持 tenant10000 原资料不变，为原生任务建立只读隔离工作空间。

    每个工作空间只接收所选 KB 的原生 Viewer 分享，业务用户也是 Viewer。
    原生任务因而能在进程恢复后继续使用真实身份检查，无需共享管理员执行。
    """

    def __init__(  # noqa: PLR0913, PLR0917 - 部署身份和现有授权服务均显式注入。
        self,
        database_path: str | Path,
        native_base_url: str,
        service_email: str,
        service_password: str,
        source_tenant_id: int,
        deployment_id: str,
        master_key: bytes,
        library: LibraryService,
        users: UserStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        chat_model_override: str | None = None,
        chat_model_config: dict[str, Any] | None = None,
    ) -> None:
        if not master_key or not deployment_id or source_tenant_id <= 0:
            raise ValueError("工作台部署身份配置不完整。")
        self._store = WorkbenchStore(database_path)
        self._native = NativeWorkspaceClient(
            native_base_url,
            service_email,
            service_password,
            transport=transport,
        )
        self._source_tenant_id = source_tenant_id
        self._deployment_id = deployment_id
        self._master_key = master_key
        self._library = library
        self._users = users
        self._chat_model_override = chat_model_override
        self._chat_model_config = chat_model_config
        self._mutation_lock = asyncio.Lock()
        self._streams: dict[str, set[_WorkbenchStream]] = {}

    async def bootstrap(
        self, profile: UserProfile, scopes: tuple[str, ...]
    ) -> dict[str, Any]:
        """按当前角色取得工作空间，资料集合变化时停用旧一代。

        Returns:
            网关工作空间 ID、资料范围、原生 Viewer 身份及助手元数据。
            不返回原生令牌、账号口令或其他工作空间 membership。

        """
        async with self._mutation_lock:
            current = self._current_profile(profile)
            normalized = _scopes(scopes)
            kb_ids = tuple(
                sorted(self._library.search_kbs(current, normalized))
            )
            if not kb_ids:
                raise HTTPException(409, "所选范围暂无资料，请先上传资料。")
            records = self._store.list_workspaces(current.user_id)
            for record in records:
                if record.search_scopes != normalized:
                    continue
                model_matches = (
                    not self._chat_model_override
                    or record.model_id == self._chat_model_override
                )
                if (
                    record.state == "active"
                    and record.kb_ids == kb_ids
                    and model_matches
                ):
                    return self._bootstrap_result(current, record)
                if not record.revoked:
                    await self._disable((record,))
            identity = await self._ensure_identity(current)
            record = self._store.create(current.user_id, normalized, kb_ids)
            try:
                record = await self._provision(record, identity)
            except BaseException:
                # 创建期间中断也先关闭网关入口；撤权失败保留记录供重试。
                latest = self._store.get(record.workspace_id)
                if latest is not None:
                    await asyncio.shield(self._disable((latest,)))
                raise
            self._store.activate(record.workspace_id)
            active = self._store.get(record.workspace_id)
            if active is None:
                raise AssertionError("已创建的工作空间不存在。")
            return self._bootstrap_result(current, active)

    async def proxy(  # noqa: PLR0913, PLR0917 - 与已有原生代理 HTTP 契约一致。
        self,
        profile: UserProfile,
        workspace_id: str,
        method: str,
        path: str,
        query: str,
        content: bytes,
        browser_headers: Mapping[str, str],
    ) -> NativeAdminResponse:
        """仅转发当前用户工作空间所需的原生功能请求。"""
        record = self._authorize(profile, workspace_id)
        route = _route(method, path)
        query = _query(query)
        _check_kb_route(route, record.kb_ids)
        if route == "auth/me":
            return _json_response(self._identity_result(profile, record))
        if route == "tenants/kv/assistant-config":
            return _json_response(
                {
                    "success": True,
                    "data": {
                        "display_name": "湾小度工作助手",
                        "max_access_level": "read_only",
                    },
                }
            )
        content = _request_content(method, route, content, record)
        account = self._account(record.owner_id)
        response = await self._native.proxy(
            account=account,
            tenant_id=record.native_tenant_id,
            method=method,
            path="api/v1/" + route,
            query=query,
            content=content,
            browser_headers=browser_headers,
        )
        try:
            self._authorize(profile, workspace_id)
        except BaseException:
            if response.stream is not None:
                await response.stream.aclose()
            raise
        if response.stream is not None:
            stream = _WorkbenchStream(
                response.stream,
                lambda: self._authorize(profile, workspace_id),
                lambda item: self._forget_stream(workspace_id, item),
            )
            self._streams.setdefault(workspace_id, set()).add(stream)
            return replace(response, stream=stream)
        return _filtered_response(response, route, record.kb_ids)

    async def before_role_change(
        self, target: UserProfile, new_role: Role
    ) -> None:
        """在保存降权前停用所有含人力资料的工作空间及后台任务。"""
        if new_role == "hr" or target.is_super_admin:
            return
        async with self._mutation_lock:
            affected = tuple(
                row
                for row in self._store.list_workspaces(target.user_id)
                if "hr_internal" in row.search_scopes and not row.revoked
            )
            await self._disable(affected)

    async def invalidate_scopes(self, scopes: set[str]) -> None:
        """在公共绑定或资料归属改变之前关闭受影响的全部旧一代。"""
        if not scopes.issubset(_SCOPE_LABELS):
            raise ValueError("工作台资料范围无效。")
        async with self._mutation_lock:
            affected = tuple(
                row
                for row in self._store.list_workspaces()
                if scopes.intersection(row.search_scopes) and not row.revoked
            )
            await self._disable(affected)

    async def close(self) -> None:
        """关闭事件流和客户端；普通进程退出不删除用户定时任务。"""
        for streams in tuple(self._streams.values()):
            for stream in tuple(streams):
                await stream.aclose()
        await self._native.close()

    def _current_profile(self, profile: UserProfile) -> UserProfile:
        current = self._users.get(profile.user_id)
        if current is None:
            raise HTTPException(403, "当前研发用户不存在。")
        return current

    def _authorize(
        self, profile: UserProfile, workspace_id: str
    ) -> WorkbenchWorkspace:
        current = self._current_profile(profile)
        record = self._store.get(workspace_id)
        if record is None or record.owner_id != current.user_id:
            raise HTTPException(404, "工作空间不存在。")
        if record.state != "active":
            raise HTTPException(403, "工作空间资料权限已变化，请重新进入。")
        allowed = tuple(
            sorted(self._library.search_kbs(current, record.search_scopes))
        )
        if allowed != record.kb_ids:
            # 旧上下文可能含已撤资料，不能过滤新结果后继续复用它。
            self._store.disable((workspace_id,))
            raise HTTPException(403, "工作空间资料范围已变化，请重新进入。")
        return record

    async def _ensure_identity(self, profile: UserProfile) -> WorkbenchIdentity:
        identity = self._store.identity(profile.user_id)
        if identity is not None:
            return identity
        username, email, password = self._credentials(profile.user_id)
        payload = await self._native.service_request(
            self._source_tenant_id,
            "POST",
            "api/v1/system/admin/users/create",
            {"username": username, "email": email, "password": password},
        )
        user = payload.get("user")
        if not isinstance(user, dict) or (
            user.get("username") != username
            or user.get("email") != email
            or user.get("is_system_admin") is not False
            or user.get("can_access_all_tenants") is not False
        ):
            raise NativeHttpError(403, "工作台原生用户身份或权限不匹配。")
        native_id = _string_id(user)
        identity = WorkbenchIdentity(
            profile.user_id, native_id, username, email
        )
        self._store.remember_identity(identity)
        return identity

    def _credentials(self, user_id: str) -> tuple[str, str, str]:
        subject = f"{self._deployment_id}\0{user_id}".encode()
        digest = hashlib.sha256(subject).hexdigest()[:28]
        username = "wxd_" + digest
        email = username + "@users.wanxiaodu.invalid"
        derived = hmac.digest(
            self._master_key, b"workbench-user\0" + subject, "sha256"
        )
        # 原生用户口令上限为32字符，保留28个HMAC字符与固定复杂度前缀。
        password = "Aa1!" + base64.urlsafe_b64encode(derived).decode()[:28]
        return username, email, password

    def _account(self, user_id: str) -> NativeWorkspaceAccount:
        identity = self._store.identity(user_id)
        if identity is None:
            raise NativeHttpError(502, "工作台原生身份尚未登记。")
        _, email, password = self._credentials(user_id)
        return NativeWorkspaceAccount(email, password, identity.native_user_id)

    async def _provision(
        self, record: WorkbenchWorkspace, identity: WorkbenchIdentity
    ) -> WorkbenchWorkspace:
        name = "湾小度-" + record.workspace_id
        tenant = native_data(
            await self._native.service_request(
                self._source_tenant_id, "POST", "api/v1/tenants", {"name": name}
            )
        )
        tenant_id = tenant.get("id")
        if not isinstance(tenant_id, int) or tenant_id <= 0:
            raise NativeHttpError(502, "原生工作空间缺少 ID。")
        if tenant_id == self._source_tenant_id:
            raise NativeHttpError(502, "不能把资料源空间作为用户工作空间。")
        self._store.save_native(record.workspace_id, tenant_id=tenant_id)
        org = native_data(
            await self._native.service_request(
                self._source_tenant_id,
                "POST",
                "api/v1/organizations",
                {
                    "name": name,
                    "description": "湾小度资料范围专用共享",
                    "member_limit": 2,
                },
            )
        )
        org_id = _string_id(org)
        self._store.save_native(
            record.workspace_id, tenant_id=tenant_id, organization_id=org_id
        )
        await self._native.service_request(
            self._source_tenant_id,
            "PUT",
            f"api/v1/organizations/{org_id}",
            {"searchable": False, "require_approval": True},
        )
        await self._native.service_request(
            self._source_tenant_id,
            "POST",
            f"api/v1/organizations/{org_id}/invite",
            {"tenant_id": tenant_id, "role": "viewer"},
        )
        for kb_id in record.kb_ids:
            await self._native.service_request(
                self._source_tenant_id,
                "POST",
                f"api/v1/knowledge-bases/{kb_id}/shares",
                {"organization_id": org_id, "permission": "viewer"},
            )
        model_id = await self._configure_workspace(tenant_id)
        self._store.save_native(
            record.workspace_id,
            tenant_id=tenant_id,
            organization_id=org_id,
            model_id=model_id,
        )
        # 所有配置和分享完成后才授 Viewer，失败的半成品始终不可进入。
        await self._native.service_request(
            tenant_id,
            "POST",
            f"api/v1/tenants/{tenant_id}/members",
            {"email": identity.email, "role": "viewer"},
        )
        await self._native.request_json(
            self._account(record.owner_id), tenant_id, "GET", "api/v1/auth/me"
        )
        return replace(
            record,
            native_tenant_id=tenant_id,
            organization_id=org_id,
            model_id=model_id,
        )

    async def _configure_workspace(self, tenant_id: int) -> str:
        await self._native.service_request(
            tenant_id,
            "PUT",
            "api/v1/tenants/kv/assistant-config",
            {"display_name": "湾小度工作助手", "max_access_level": "read_only"},
        )
        config = native_data(
            await self._native.service_request(
                tenant_id, "GET", "api/v1/tenants/kv/assistant-config"
            )
        )
        if config.get("max_access_level") != "read_only":
            raise NativeHttpError(502, "原生助手未限制为只读。")
        models = native_items(
            await self._native.service_request(
                tenant_id, "GET", "api/v1/models"
            )
        )
        model_id = self._chat_model_override or _model(models, "KnowledgeQA")
        if self._chat_model_config is not None:
            model = native_data(
                await self._native.service_request(
                    tenant_id, "POST", "api/v1/models", self._chat_model_config
                )
            )
            model_id = _string_id(model)
        if not model_id:
            raise NativeHttpError(502, "工作台缺少可用聊天模型。")
        selected = native_data(
            await self._native.service_request(
                tenant_id, "GET", f"api/v1/models/{model_id}"
            )
        )
        if (
            selected.get("type") != "KnowledgeQA"
            or selected.get("status") != "active"
        ):
            raise NativeHttpError(502, "工作台聊天模型不可用。")
        rerank_id = _model(models, "Rerank")
        for agent_id in (_ASSISTANT_ID, *_TEMPLATE_IDS):
            await self._configure_agent(
                tenant_id, agent_id, model_id, rerank_id
            )
        return model_id

    async def _configure_agent(
        self, tenant_id: int, agent_id: str, model_id: str, rerank_id: str
    ) -> None:
        agent = native_data(
            await self._native.service_request(
                tenant_id, "GET", f"api/v1/agents/{agent_id}"
            )
        )
        config = agent.get("config")
        if not isinstance(config, dict):
            raise NativeHttpError(502, "原生助手缺少配置。")
        updated = {**config, **_safe_agent_config(model_id, rerank_id)}
        # resolver 只接受 reasoning 模板；改为 quick-answer 可在原生层拒绝
        # 模型自行选用模板，仍允许未指定 agent_id 的继承只读子任务。
        mode = (
            "smart-reasoning" if agent_id == _ASSISTANT_ID else "quick-answer"
        )
        updated["agent_mode"] = mode
        await self._native.service_request(
            tenant_id,
            "PUT",
            f"api/v1/agents/{agent_id}",
            {"name": agent.get("name", agent_id), "config": updated},
        )
        verified = native_data(
            await self._native.service_request(
                tenant_id, "GET", f"api/v1/agents/{agent_id}"
            )
        ).get("config")
        expected = {
            **_safe_agent_config(model_id, rerank_id),
            "agent_mode": mode,
        }
        if not isinstance(verified, dict) or any(
            verified.get(
                key,
                ""
                if key in {"sandbox_config_id", "system_prompt_id"}
                else None,
            )
            != value
            for key, value in expected.items()
        ):
            raise NativeHttpError(502, "原生助手只读配置回读不一致。")

    async def _disable(self, records: tuple[WorkbenchWorkspace, ...]) -> None:
        if not records:
            return
        self._store.disable(tuple(row.workspace_id for row in records))
        errors: list[Exception] = []
        for record in records:
            for stream in tuple(self._streams.get(record.workspace_id, set())):
                try:
                    await stream.aclose()
                except Exception as error:  # 流关闭失败不能跳过原生撤权。
                    errors.append(error)
        for record in records:
            try:
                await self._revoke(record)
            except Exception as error:  # 每个受影响空间都必须尝试撤权。
                errors.append(error)
        if errors:
            raise NativeHttpError(
                502, "工作台已禁用，部分原生任务撤权未完成，请重试。"
            ) from errors[0]

    async def _revoke(self, record: WorkbenchWorkspace) -> None:
        if record.revoked:
            return
        if not record.native_tenant_id:
            self._store.mark_revoked(record.workspace_id)
            return
        account = self._account(record.owner_id)
        errors: list[Exception] = []
        try:
            await self._stop_owner_tasks(account, record.native_tenant_id)
        except NativeHttpError as error:
            errors.append(error)
        # 即使停止失败，也必须撤 membership，让调度恢复和内部操作拒绝运行。
        revoked = True
        try:
            await self._native.service_request(
                record.native_tenant_id,
                "DELETE",
                f"api/v1/tenants/{record.native_tenant_id}/members/{account.user_id}",
            )
        except NativeHttpError as error:
            if error.status_code != _HTTP_NOT_FOUND:
                errors.append(error)
                revoked = False
        # 同时撤组织分享接收方，已经持有旧运行上下文的读工具也会失去 KB 授权。
        if record.organization_id:
            try:
                await self._native.service_request(
                    self._source_tenant_id,
                    "DELETE",
                    f"api/v1/organizations/{record.organization_id}/members/{record.native_tenant_id}",
                )
            except NativeHttpError as error:
                if error.status_code != _HTTP_NOT_FOUND:
                    errors.append(error)
                    revoked = False
        self._native.forget(account, record.native_tenant_id)
        if revoked:
            self._store.mark_revoked(record.workspace_id)
        if errors:
            raise errors[0]

    async def _stop_owner_tasks(
        self, account: NativeWorkspaceAccount, tenant_id: int
    ) -> None:
        try:
            tasks = native_items(
                await self._native.request_json(
                    account, tenant_id, "GET", "api/v1/automations"
                )
            )
        except NativeHttpError as error:
            # 创建未完成或已经撤权时不能重建 Viewer；最终撤 membership 幂等。
            if error.status_code in {403, _HTTP_NOT_FOUND}:
                return
            raise
        errors: list[Exception] = []
        for task in tasks:
            task_id = _string_id(task)
            try:
                await self._native.request_json(
                    account,
                    tenant_id,
                    "POST",
                    f"api/v1/automations/{task_id}/pause",
                    {},
                )
            except NativeHttpError as error:
                errors.append(error)
            try:
                await self._cancel_runs(account, tenant_id, task_id)
            except NativeHttpError as error:
                errors.append(error)
        for page in range(1, _MAX_PAGES + 1):
            response = await self._native.proxy(
                account=account,
                tenant_id=tenant_id,
                method="GET",
                path="api/v1/sessions",
                query=f"kind=assistant&page={page}&page_size={_PAGE_SIZE}",
                content=b"",
                browser_headers={},
            )
            sessions = native_items(
                decode_json(response.status_code, response.content)
            )
            for session in sessions:
                try:
                    await self._stop_session(account, tenant_id, session)
                except NativeHttpError as error:
                    errors.append(error)
            if len(sessions) < _PAGE_SIZE:
                if errors:
                    raise errors[0]
                return
        raise NativeHttpError(502, "工作台会话分页未结束，无法确认任务停止。")

    async def _cancel_runs(
        self, account: NativeWorkspaceAccount, tenant_id: int, task_id: str
    ) -> None:
        for page in range(1, _MAX_PAGES + 1):
            response = await self._native.proxy(
                account=account,
                tenant_id=tenant_id,
                method="GET",
                path=f"api/v1/automations/{task_id}/runs",
                query=f"page={page}&page_size={_PAGE_SIZE}",
                content=b"",
                browser_headers={},
            )
            rows = native_items(
                decode_json(response.status_code, response.content)
            )
            for run in rows:
                if run.get("status") in _ACTIVE_RUNS:
                    await self._native.request_json(
                        account,
                        tenant_id,
                        "POST",
                        f"api/v1/automation-runs/{_string_id(run)}/cancel",
                        {},
                    )
            if len(rows) < _PAGE_SIZE:
                return
        raise NativeHttpError(502, "工作台任务分页未结束，无法确认任务停止。")

    async def _stop_session(
        self,
        account: NativeWorkspaceAccount,
        tenant_id: int,
        session: dict[str, Any],
    ) -> None:
        session_id = _string_id(session)
        cursor = ""
        for _ in range(_MAX_PAGES):
            query = urlencode({"limit": _PAGE_SIZE, "before_time": cursor})
            response = await self._native.proxy(
                account=account,
                tenant_id=tenant_id,
                method="GET",
                path=f"api/v1/messages/{session_id}/load",
                query=query,
                content=b"",
                browser_headers={},
            )
            rows = native_items(
                decode_json(response.status_code, response.content)
            )
            for message in rows:
                if message.get("role") == "assistant":
                    # 已完成的父回答仍可能有后台子任务，原生 stop 会先停协作。
                    await self._native.request_json(
                        account,
                        tenant_id,
                        "POST",
                        f"api/v1/sessions/{session_id}/stop",
                        {"message_id": _string_id(message)},
                    )
            if len(rows) < _PAGE_SIZE:
                return
            times = [row.get("created_at") for row in rows]
            if not all(isinstance(value, str) and value for value in times):
                raise NativeHttpError(502, "工作台消息缺少分页时间。")
            next_cursor = min(str(value) for value in times)
            if next_cursor == cursor:
                raise NativeHttpError(502, "工作台消息分页没有前进。")
            cursor = next_cursor
        raise NativeHttpError(502, "工作台消息分页未结束，无法确认任务停止。")

    def _bootstrap_result(
        self, profile: UserProfile, record: WorkbenchWorkspace
    ) -> dict[str, Any]:
        identity = self._identity_result(profile, record)["data"]
        return {
            "workspace_id": record.workspace_id,
            "generation": record.generation,
            "search_scopes": list(record.search_scopes),
            "scope_label": " + ".join(
                _SCOPE_LABELS[item] for item in record.search_scopes
            ),
            "knowledge_base_ids": list(record.kb_ids),
            "user": identity["user"],
            "tenant": identity["tenant"],
            "assistant": {
                "id": _ASSISTANT_ID,
                "access_level": "read_only",
                "model_id": record.model_id,
            },
            "features": {
                "collaboration": True,
                "automations": True,
                "writes": False,
            },
        }

    def _identity_result(
        self, profile: UserProfile, record: WorkbenchWorkspace
    ) -> dict[str, Any]:
        identity = self._store.identity(record.owner_id)
        if identity is None:
            raise NativeHttpError(502, "工作台原生身份未登记。")
        return {
            "success": True,
            "data": {
                "user": {
                    "id": identity.native_user_id,
                    "username": profile.username,
                    "nickname": profile.display_name,
                    "role": "viewer",
                    "is_system_admin": False,
                    "can_access_all_tenants": False,
                },
                "tenant": {
                    "id": record.native_tenant_id,
                    "name": "湾小度工作台",
                    "current_role": "viewer",
                    "is_owner": False,
                },
                "memberships": [
                    {"tenant_id": record.native_tenant_id, "role": "viewer"}
                ],
                "tenant_required": False,
                "capabilities": {
                    "can_create_tenant": False,
                    "auto_accept_invitation": False,
                },
                "preference_defaults": {},
            },
        }

    def _forget_stream(
        self, workspace_id: str, stream: _WorkbenchStream
    ) -> None:
        streams = self._streams.get(workspace_id)
        if streams is not None:
            streams.discard(stream)
            if not streams:
                self._streams.pop(workspace_id, None)


class _WorkbenchStream(NativeAdminStream):
    """每次发送前重新校验角色和资料范围，禁用后立即关闭上游。"""

    def __init__(
        self,
        upstream: NativeAdminStream,
        authorize: Callable[[], WorkbenchWorkspace],
        finished: Callable[[_WorkbenchStream], None],
    ) -> None:
        self._upstream = upstream
        self._authorize = authorize
        self._finished = finished
        self._closed = False

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            self._authorize()
            chunk = await anext(self._upstream)
            self._authorize()
        except BaseException:
            await self.aclose()
            raise
        return chunk

    async def aclose(self) -> None:
        """撤权和下游断开共用一个幂等关闭路径。"""
        if not self._closed:
            self._closed = True
            self._finished(self)
            await self._upstream.aclose()


def _scopes(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or not set(values).issubset(_SCOPE_LABELS):
        raise HTTPException(422, "请选择有效的资料范围。")
    return tuple(key for key in _SCOPE_LABELS if key in values)


def _string_id(value: dict[str, Any]) -> str:
    identifier = value.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(_ID, identifier):
        raise NativeHttpError(502, "原生响应包含无效的资源 ID。")
    return identifier


def _model(models: list[dict[str, Any]], kind: str) -> str:
    for model in models:
        if model.get("type") == kind and model.get("status") == "active":
            return _string_id(model)
    return ""


def _safe_agent_config(model_id: str, rerank_id: str) -> dict[str, Any]:
    return {
        # 精简管理说明，为8K上下文保留输出空间；协作仍由原生运行时注入。
        "system_prompt": _WORKBENCH_SYSTEM_PROMPT,
        "system_prompt_id": "",
        "max_completion_tokens": 1024,
        "model_id": model_id,
        "rerank_model_id": rerank_id,
        "access_level": "read_only",
        "allowed_tools": list(_READ_TOOLS),
        "kb_selection_mode": "all",
        "knowledge_bases": [],
        "retrieve_kb_only_when_mentioned": False,
        "web_search_enabled": False,
        "web_fetch_enabled": False,
        "mcp_selection_mode": "none",
        "mcp_services": [],
        "skills_selection_mode": "none",
        "selected_skills": [],
        "sandbox_config_id": "",
        "memory_enabled": False,
    }


def _route(method: str, path: str) -> str:
    normalized = path.removeprefix("/")
    if not normalized.startswith("api/v1/") or any(
        character in normalized for character in ("%", "\\", "?", "#")
    ):
        raise HTTPException(403, "工作台接口不在允许范围内。")
    route = normalized.removeprefix("api/v1/")
    if not any(
        method in methods.split() and pattern.fullmatch(route)
        for methods, pattern in _ROUTES
    ):
        raise HTTPException(403, "工作台接口不在允许范围内。")
    return route


def _query(query: str) -> str:
    values = parse_qsl(query, keep_blank_values=True)
    if any(key.casefold() in _FORBIDDEN_QUERY for key, _ in values):
        raise HTTPException(403, "不能指定工作台身份或公共文件令牌。")
    return urlencode(values)


def _check_kb_route(route: str, kb_ids: tuple[str, ...]) -> None:
    parts = route.split("/")
    if (
        len(parts) > 1
        and parts[0] == "knowledge-bases"
        and parts[1] not in kb_ids
    ):
        raise HTTPException(403, "资料不在当前工作空间范围内。")


def _request_content(
    method: str, route: str, content: bytes, record: WorkbenchWorkspace
) -> bytes:
    if method not in {"POST", "PUT", "PATCH"}:
        return b""
    try:
        body = json.loads(content or b"{}")
    except ValueError as error:
        raise HTTPException(422, "工作台请求必须为 JSON。") from error
    if not isinstance(body, dict):
        raise HTTPException(422, "工作台请求必须为对象。")
    if route == "sessions" or (
        route.startswith("sessions/") and method == "PUT"
    ):
        body = {
            key: value
            for key, value in body.items()
            if key in {"title", "description"}
        }
        if route == "sessions":
            body["kind"] = "assistant"
    elif route.startswith("agent-chat/"):
        body = _chat_content(body, record)
    elif route == "automations" or (
        route.startswith("automations/") and method == "PUT"
    ):
        body = {
            key: value
            for key, value in body.items()
            if key
            in {
                "name",
                "description",
                "instruction",
                "trigger",
                "enabled",
                "notify_on_success",
                "misfire_policy",
                "overlap_policy",
                "approval_timeout_minutes",
                "limits",
            }
        }
        body.update(
            {
                "access_level": "read_only",
                "session_mode": "per_run",
                "carry_last_result": False,
                "model_id": record.model_id,
            }
        )
    elif route.startswith("agent/prompts/"):
        body.pop("remember", None)
        body.pop("grant", None)
    return json.dumps(body, ensure_ascii=False).encode()


def _chat_content(
    body: dict[str, Any], record: WorkbenchWorkspace
) -> dict[str, Any]:
    requested = body.get("knowledge_base_ids") or list(record.kb_ids)
    if not isinstance(requested, list) or not all(
        isinstance(kb_id, str) and kb_id in record.kb_ids for kb_id in requested
    ):
        raise HTTPException(403, "提问资料不在当前工作空间范围内。")
    result = {
        key: value
        for key, value in body.items()
        if key
        in {
            "query",
            "knowledge_ids",
            "reasoning_effort",
        }
    }
    result.update(
        {
            "knowledge_base_ids": requested,
            "agent_id": _ASSISTANT_ID,
            "agent_enabled": True,
            "assistant_access": "read_only",
            "web_search_enabled": False,
            "local_browser_enabled": False,
            "mcp_service_ids": [],
            "skill_names": [],
            "disable_title": True,
            "summary_model_id": record.model_id,
            "channel": "web",
        }
    )
    return result


def _filtered_response(
    response: NativeAdminResponse, route: str, kb_ids: tuple[str, ...]
) -> NativeAdminResponse:
    if "json" not in response.headers.get("content-type", ""):
        return response
    try:
        payload = json.loads(response.content)
    except ValueError:
        raise NativeHttpError(502, "工作台原生响应无法解析。") from None
    if route in {"knowledge-bases", "shared-knowledge-bases"} and isinstance(
        payload, dict
    ):
        rows = payload.get("data")
        if isinstance(rows, list):
            kept = []
            for row in rows:
                kb = (
                    row.get("knowledge_base", row)
                    if isinstance(row, dict)
                    else {}
                )
                if isinstance(kb, dict) and kb.get("id") in kb_ids:
                    kept.append(row)
            payload["data"] = kept
            if "total" in payload:
                payload["total"] = len(kept)
    return replace(
        response,
        content=json.dumps(
            _without_secrets(payload), ensure_ascii=False
        ).encode(),
    )


def _without_secrets(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_secrets(item)
            for key, item in value.items()
            if key.lower() not in _SECRET_FIELDS
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


def _json_response(payload: dict[str, Any]) -> NativeAdminResponse:
    return NativeAdminResponse(
        200,
        json.dumps(payload, ensure_ascii=False).encode(),
        {
            "content-type": "application/json",
            "Cache-Control": "private, no-store",
        },
    )
