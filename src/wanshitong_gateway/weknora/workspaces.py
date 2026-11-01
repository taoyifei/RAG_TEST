"""工作台专用的原生 WebUser 客户端，按账号和工作空间分别持有 JWT。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from wanshitong_gateway.weknora.admin import (
    NativeAdminResponse,
    NativeAdminStream,
)
from wanshitong_gateway.weknora.client import NativeHttpError

_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=120.0, pool=10.0)
_REQUEST_HEADERS = frozenset(
    {"accept", "accept-language", "content-type", "range", "if-none-match"}
)
_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-disposition",
        "etag",
        "last-modified",
        "accept-ranges",
        "content-range",
    }
)
_HTTP_UNAUTHORIZED = 401
_HTTP_OK = 200
_HTTP_ERROR = 400
_HTTP_SUCCESS_END = 300


@dataclass(frozen=True, slots=True)
class NativeWorkspaceAccount:
    """只在服务端使用的登录材料；service 表示受控资源创建者。"""

    email: str
    password: str
    user_id: str = ""
    service: bool = False


class NativeWorkspaceClient:
    """为每个 WebUser 分开登录，绝不借用服务账号执行用户任务。"""

    def __init__(
        self,
        base_url: str,
        service_email: str,
        service_password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("工作台原生服务地址无效。")
        self.service_account = NativeWorkspaceAccount(
            email=service_email, password=service_password, service=True
        )
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._tokens: dict[tuple[str, int], str] = {}
        self._login_lock = asyncio.Lock()

    async def close(self) -> None:
        """释放所有用户连接和仅驻留内存的登录令牌。"""
        self._tokens.clear()
        await self._http.aclose()

    def forget(self, account: NativeWorkspaceAccount, tenant_id: int) -> None:
        """撤权后清除该工作空间的用户令牌。"""
        self._tokens.pop((account.email, tenant_id), None)

    async def service_request(
        self,
        tenant_id: int,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """固定服务账号仅供工作空间、分享和 Viewer membership 管理。"""
        return await self.request_json(
            self.service_account, tenant_id, method, path, body
        )

    async def request_json(
        self,
        account: NativeWorkspaceAccount,
        tenant_id: int,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用内部已选定路径，保留官方不同接口的 JSON 包装格式。"""
        response = await self.proxy(
            account=account,
            tenant_id=tenant_id,
            method=method,
            path=path,
            query="",
            content=json.dumps(body).encode() if body is not None else b"",
            browser_headers={"Content-Type": "application/json"},
        )
        if response.stream is not None:
            await response.stream.aclose()
            raise NativeHttpError(502, "工作台配置接口不能返回事件流。")
        return decode_json(response.status_code, response.content)

    async def proxy(  # noqa: PLR0913 - 受控代理完整保留 HTTP 请求契约。
        self,
        *,
        account: NativeWorkspaceAccount,
        tenant_id: int,
        method: str,
        path: str,
        query: str,
        content: bytes,
        browser_headers: Mapping[str, str],
    ) -> NativeAdminResponse:
        """按专属 WebUser 转交请求，调用方负责功能路径与业务授权。"""
        for attempt in range(2):
            token = await self._access_token(account, tenant_id)
            headers = {
                key: value
                for key, value in browser_headers.items()
                if key.lower() in _REQUEST_HEADERS
            }
            # 浏览器的 Authorization、API Key、租户和外部身份头均不进入。
            headers.update(
                {
                    "Authorization": f"Bearer {token}",
                    "X-Tenant-ID": str(tenant_id),
                }
            )
            target = "/" + path.lstrip("/")
            if query:
                target += "?" + query
            response: httpx.Response | None = None
            transferred = False
            try:
                request = self._http.build_request(
                    method, target, headers=headers, content=content
                )
                response = await self._http.send(request, stream=True)
                if response.status_code == _HTTP_UNAUTHORIZED and not attempt:
                    self.forget(account, tenant_id)
                    continue
                response_headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in _RESPONSE_HEADERS
                }
                response_headers["Cache-Control"] = "private, no-store"
                media = response.headers.get("content-type", "").split(";")[0]
                if response.is_success and media.strip() == "text/event-stream":
                    response_headers["X-Accel-Buffering"] = "no"
                    transferred = True
                    return NativeAdminResponse(
                        response.status_code,
                        b"",
                        response_headers,
                        NativeAdminStream(response),
                    )
                return NativeAdminResponse(
                    response.status_code,
                    await response.aread(),
                    response_headers,
                )
            except httpx.RequestError as error:
                raise NativeHttpError(
                    502, "工作台原生请求连接失败或超时。"
                ) from error
            finally:
                if response is not None and not transferred:
                    await asyncio.shield(response.aclose())
        raise NativeHttpError(502, "工作台原生登录不可用。")

    async def _access_token(
        self, account: NativeWorkspaceAccount, tenant_id: int
    ) -> str:
        key = (account.email, tenant_id)
        if key in self._tokens:
            return self._tokens[key]
        async with self._login_lock:
            if key in self._tokens:
                return self._tokens[key]
            try:
                response = await self._http.post(
                    "/api/v1/auth/login",
                    json={"email": account.email, "password": account.password},
                )
                payload = decode_json(response.status_code, response.content)
                token = _login_token(payload)
                active = payload.get("active_tenant") or {}
                if not isinstance(active, dict):
                    raise NativeHttpError(502, "原生登录缺少工作空间。")
                active_id = active.get("id", 0)
                if active_id != tenant_id:
                    response = await self._http.post(
                        "/api/v1/auth/switch-tenant",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tenant_id": tenant_id},
                    )
                    payload = decode_json(
                        response.status_code, response.content
                    )
                    token = _login_token(payload)
                _verify_login(payload, account, tenant_id)
                self._tokens[key] = token
                return token
            except httpx.RequestError as error:
                raise NativeHttpError(502, "工作台原生登录失败。") from error


def decode_json(status_code: int, content: bytes) -> dict[str, Any]:
    """校验原生成功响应，不把上游错误正文或凭据回显到浏览器。"""
    if status_code >= _HTTP_ERROR:
        raise NativeHttpError(status_code, "工作台原生操作未完成。")
    if status_code < _HTTP_OK or status_code >= _HTTP_SUCCESS_END:
        raise NativeHttpError(502, "工作台原生返回了非成功状态。")
    try:
        payload = json.loads(content)
    except ValueError as error:
        raise NativeHttpError(502, "工作台原生返回无效 JSON。") from error
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise NativeHttpError(502, "工作台原生响应无效。")
    return payload


def native_data(payload: dict[str, Any]) -> dict[str, Any]:
    """取得官方对象型 data；无包装接口保留顶层对象。"""
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise NativeHttpError(502, "工作台原生响应缺少对象。")
    return data


def native_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """解析官方列表和分页列表，拒绝忽略不认识的响应形状。"""
    data: Any = payload.get("data", payload)
    if isinstance(data, dict):
        data = data.get("items", data.get("data"))
    if not isinstance(data, list) or any(
        not isinstance(item, dict) for item in data
    ):
        raise NativeHttpError(502, "工作台原生响应缺少列表。")
    return list(data)


def _login_token(payload: dict[str, Any]) -> str:
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise NativeHttpError(502, "原生登录没有返回令牌。")
    return token


def _verify_login(
    payload: dict[str, Any], account: NativeWorkspaceAccount, tenant_id: int
) -> None:
    tenant = payload.get("active_tenant")
    user = payload.get("user")
    memberships = payload.get("memberships")
    if not isinstance(tenant, dict) or tenant.get("id") != tenant_id:
        raise NativeHttpError(403, "原生登录工作空间不匹配。")
    if not isinstance(user, dict) or not isinstance(memberships, list):
        raise NativeHttpError(502, "原生登录缺少可信身份。")
    if account.user_id and user.get("id") != account.user_id:
        raise NativeHttpError(403, "原生登录账号与研发用户映射不一致。")
    expected = {"owner", "admin"} if account.service else {"viewer"}
    if not any(
        isinstance(row, dict)
        and row.get("tenant_id") == tenant_id
        and row.get("role") in expected
        for row in memberships
    ):
        raise NativeHttpError(403, "工作台原生 membership 角色不匹配。")
    if not account.service and (
        user.get("is_system_admin") is not False
        or user.get("can_access_all_tenants") is not False
    ):
        raise NativeHttpError(403, "工作台用户不能拥有原生平台管理权限。")
