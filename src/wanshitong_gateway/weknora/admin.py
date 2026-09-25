"""仅在服务端持有原生管理员凭据的固定地址 HTTP 客户端。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from wanshitong_gateway.weknora.client import NativeHttpError

_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=120.0, pool=10.0)
_PROXY_HEADERS = frozenset(
    {"content-type", "accept", "range", "if-none-match", "if-modified-since"}
)
_MAX_RELOGIN = 1
_HTTP_UNAUTHORIZED = 401
_HTTP_BAD_REQUEST = 400
_MIN_API_SEGMENTS = 3
_ADMIN_CATEGORIES = frozenset(
    {
        "agent",
        "agent-chat",
        "agents",
        "artifacts",
        "chunker",
        "chunks",
        "datasource",
        "embed-channels",
        "faq",
        "im-channels",
        "initialization",
        "knowledge",
        "knowledge-bases",
        "knowledge-chat",
        "knowledge-search",
        "knowledgebase",
        "messages",
        "me",
        "models",
        "organizations",
        "sessions",
        "shared-agents",
        "shared-knowledge-bases",
        "storage-backends",
        "system",
        "tenants",
        "user",
        "vector-stores",
        "web-search-providers",
    }
)


@dataclass(frozen=True, slots=True)
class NativeAdminResponse:
    """仅保留允许返回浏览器的响应字段。"""

    status_code: int
    content: bytes
    headers: dict[str, str]


class NativeAdminClient:
    """用候选专属服务账号为湾事通管理员会话访问原生配置。"""

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        password: str,
        tenant_id: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("原生管理员服务地址无效。")
        if not email or not password or tenant_id <= 0:
            raise ValueError("原生管理员服务账号配置不完整。")
        self._email = email
        self._password = password
        self._tenant_id = tenant_id
        self._token: str | None = None
        self._login_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        """释放候选原生服务连接。"""
        await self._http.aclose()

    async def identity(self) -> dict[str, Any]:
        """返回原生会话真实身份，校验其属于固定候选工作空间。"""
        for attempt in range(_MAX_RELOGIN + 1):
            token = await self._access_token()
            response = await self._http.get(
                "/api/v1/auth/me",
                headers=self._headers(token),
            )
            if (
                response.status_code == _HTTP_UNAUTHORIZED
                and attempt < _MAX_RELOGIN
            ):
                await self._invalidate(token)
                continue
            payload = self._json_success(response)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise NativeHttpError(502, "原生管理员身份响应无效。")
            tenant = data.get("tenant")
            user = data.get("user")
            memberships = data.get("memberships")
            if (
                not isinstance(tenant, dict)
                or tenant.get("id") != self._tenant_id
                or not isinstance(user, dict)
                or not isinstance(memberships, list)
                or not any(
                    isinstance(item, dict)
                    and item.get("tenant_id") == self._tenant_id
                    and str(item.get("role", "")).lower()
                    in {"owner", "admin"}
                    for item in memberships
                )
            ):
                raise NativeHttpError(403, "原生管理员工作空间或角色不匹配。")
            return {
                "user": user,
                "tenant": tenant,
                "memberships": [
                    item
                    for item in memberships
                    if isinstance(item, dict)
                    and item.get("tenant_id") == self._tenant_id
                ],
                "tenant_required": False,
                "capabilities": {
                    "can_create_tenant": False,
                    "auto_accept_invitation": False,
                },
                "preference_defaults": data.get("preference_defaults", {}),
            }
        raise NativeHttpError(502, "原生管理员身份不可用。")

    async def proxy(
        self,
        *,
        method: str,
        path: str,
        query: str,
        content: bytes,
        browser_headers: Mapping[str, str],
    ) -> NativeAdminResponse:
        """只把指定 API 路径转交原生服务；浏览器不见令牌。"""
        if not _allowed_admin_path(method, path):
            raise NativeHttpError(403, "管理操作不在候选白名单内。")
        for attempt in range(_MAX_RELOGIN + 1):
            token = await self._access_token()
            headers = self._headers(token)
            headers.update(
                {
                    key: value
                    for key, value in browser_headers.items()
                    if key.lower() in _PROXY_HEADERS
                }
            )
            target = "/" + path + ("?" + query if query else "")
            response = await self._http.request(
                method,
                target,
                content=content,
                headers=headers,
            )
            if (
                response.status_code == _HTTP_UNAUTHORIZED
                and attempt < _MAX_RELOGIN
            ):
                await self._invalidate(token)
                continue
            response_headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower()
                in {
                    "content-type",
                    "content-disposition",
                    "etag",
                    "last-modified",
                    "accept-ranges",
                    "content-range",
                }
            }
            response_headers["Cache-Control"] = "private, no-store"
            return NativeAdminResponse(
                status_code=response.status_code,
                content=response.content,
                headers=response_headers,
            )
        raise NativeHttpError(502, "原生管理员会话不可用。")

    async def _access_token(self) -> str:
        if self._token is not None:
            return self._token
        async with self._login_lock:
            if self._token is not None:
                return self._token
            response = await self._http.post(
                "/api/v1/auth/login",
                json={"email": self._email, "password": self._password},
            )
            payload = self._json_success(response)
            token = payload.get("token")
            if not isinstance(token, str) or not token:
                raise NativeHttpError(502, "原生管理员登录未返回令牌。")
            self._token = token
            return token

    async def _invalidate(self, token: str) -> None:
        async with self._login_lock:
            if self._token == token:
                self._token = None

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-ID": str(self._tenant_id),
            "Accept": "application/json",
        }

    @staticmethod
    def _json_success(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise NativeHttpError(502, "原生管理员响应无效。") from error
        if (
            response.status_code >= _HTTP_BAD_REQUEST
            or not isinstance(payload, dict)
            or payload.get("success") is not True
        ):
            raise NativeHttpError(response.status_code, "原生管理员请求失败。")
        return payload


def _allowed_admin_path(method: str, path: str) -> bool:  # noqa: PLR0911
    """允许原生管理 UI 的配置和知识功能，拒绝账号与跨租户操作。"""
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        return False
    if not path or "//" in path or "\\" in path or "%" in path:
        return False
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return False
    if path == "files":
        return method in {"GET", "HEAD"}
    if segments[:2] != ["api", "v1"] or len(segments) < _MIN_API_SEGMENTS:
        return False
    category = segments[2]
    if category == "auth":
        return method == "GET" and segments[3:] == ["config"]
    if category not in _ADMIN_CATEGORIES:
        return False
    if path.startswith("api/v1/system/admin/"):
        return False
    if path.startswith("api/v1/system/host-project-dir"):
        return False
    return not path.startswith(
        "api/v1/initialization/ollama/models/download"
    )
