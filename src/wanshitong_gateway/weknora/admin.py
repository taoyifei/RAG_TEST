"""仅在服务端持有原生管理员凭据的固定地址 HTTP 客户端。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from wanshitong_gateway.weknora.client import NativeHttpError

_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=120.0, pool=10.0)
_PROXY_HEADERS = frozenset(
    {"content-type", "accept", "range", "if-none-match", "if-modified-since"}
)
_MAX_RELOGIN = 1
_HTTP_UNAUTHORIZED = 401
_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400
_HTTP_SUCCESS_END = 300
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
    """仅保留允许返回浏览器的响应字段，流由消费方持续转发。"""

    status_code: int
    content: bytes
    headers: dict[str, str]
    stream: NativeAdminStream | None = None


@dataclass(frozen=True, slots=True)
class NativePublicKeyScope:
    """仅保留验证公共发布所需的非秘密 Key 授权字段。"""

    knowledge_base_ids: frozenset[str]


class NativeAdminStream:
    """持有上游 SSE 连接，并在结束、超时或取消时释放它。"""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._chunks = response.aiter_bytes()
        self._closed = False

    def __aiter__(self) -> NativeAdminStream:
        """逐块转发原生事件。"""
        return self

    async def __anext__(self) -> bytes:
        """取下一块，并在流结束或失败时关闭原生连接。"""
        try:
            return await anext(self._chunks)
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        """供取消或未开始发送的下游响应显式关闭上游。"""
        if self._closed:
            return
        self._closed = True
        await asyncio.shield(self._response.aclose())


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
                    and str(item.get("role", "")).lower() in {"owner", "admin"}
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

    async def proxy(  # noqa: PLR0913 - 浏览器代理边界需显式传递请求字段。
        self,
        *,
        method: str,
        path: str,
        query: str,
        content: bytes,
        browser_headers: Mapping[str, str],
        allow_public_agent_create: bool = False,
    ) -> NativeAdminResponse:
        """只把指定 API 路径转交原生服务；浏览器不见令牌。"""
        if not _allowed_admin_path(
            method,
            path,
            allow_public_agent_create=allow_public_agent_create,
        ):
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
            request = self._http.build_request(
                method, target, content=content, headers=headers
            )
            response: httpx.Response | None = None
            transferred = False
            try:
                response = await self._http.send(request, stream=True)
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
                media_type = response.headers.get("content-type", "").split(
                    ";", 1
                )[0]
                if (
                    _HTTP_OK <= response.status_code < _HTTP_SUCCESS_END
                    and media_type.strip().lower() == "text/event-stream"
                ):
                    response_headers["X-Accel-Buffering"] = "no"
                    transferred = True
                    return NativeAdminResponse(
                        status_code=response.status_code,
                        content=b"",
                        headers=response_headers,
                        stream=NativeAdminStream(response),
                    )
                return NativeAdminResponse(
                    status_code=response.status_code,
                    content=await response.aread(),
                    headers=response_headers,
                )
            except httpx.RequestError as error:
                raise NativeHttpError(
                    502, "原生管理员请求超时或连接失败。"
                ) from error
            finally:
                if response is not None and not transferred:
                    await asyncio.shield(response.aclose())
        raise NativeHttpError(502, "原生管理员会话不可用。")

    async def json_request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """服务端配置路径使用与管理代理相同的身份和白名单。"""
        response = await self.proxy(
            method=method,
            path=path,
            query="",
            content=(
                json.dumps(body).encode("utf-8")
                if body is not None
                else b""
            ),
            browser_headers={"Content-Type": "application/json"},
            allow_public_agent_create=(
                method == "POST" and path == "api/v1/agents"
            ),
        )
        if response.stream is not None:
            await response.stream.aclose()
            raise NativeHttpError(502, "原生配置响应不能是流。")
        try:
            payload = json.loads(response.content)
        except ValueError as error:
            raise NativeHttpError(502, "原生配置响应不是 JSON。") from error
        if (
            response.status_code >= _HTTP_BAD_REQUEST
            or not isinstance(payload, dict)
            or payload.get("success") is not True
        ):
            raise NativeHttpError(response.status_code, "原生配置请求失败。")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise NativeHttpError(502, "原生配置缺少对象。")
        return data

    async def public_key_scope(  # noqa: PLR0912 - 授权校验逐项失败即拒绝。
        self, configured_fingerprint: bytes
    ) -> NativePublicKeyScope:
        """只在服务端核对当前公共 Key，绝不把原生 Key 列表转给浏览器。"""
        for attempt in range(_MAX_RELOGIN + 1):
            token = await self._access_token()
            try:
                response = await self._http.get(
                    f"/api/v1/tenants/{self._tenant_id}/api-keys",
                    headers=self._headers(token),
                )
            except httpx.RequestError as error:
                raise NativeHttpError(502, "公共 Key 授权读取失败。") from error
            if (
                response.status_code == _HTTP_UNAUTHORIZED
                and attempt < _MAX_RELOGIN
            ):
                await self._invalidate(token)
                continue
            rows = self._json_success(response).get("data")
            if not isinstance(rows, list):
                raise NativeHttpError(502, "公共 Key 授权响应无效。")
            for row in rows:
                if not isinstance(row, dict):
                    raise NativeHttpError(502, "公共 Key 授权响应无效。")
                candidate = row.get("api_key")
                if not isinstance(candidate, str):
                    continue
                candidate_fingerprint = hashlib.sha256(
                    candidate.encode("utf-8")
                ).digest()
                if not hmac.compare_digest(
                    candidate_fingerprint, configured_fingerprint
                ):
                    continue
                if row.get("scope_type") != "tenant":
                    raise NativeHttpError(403, "公共 Key 工作空间不匹配。")
                expiry = row.get("expires_at")
                if expiry:
                    try:
                        expires_at = datetime.fromisoformat(
                            str(expiry).replace("Z", "+00:00")
                        )
                    except ValueError as error:
                        raise NativeHttpError(
                            502, "公共 Key 过期时间无效。"
                        ) from error
                    if expires_at.tzinfo is None or expires_at <= datetime.now(
                        UTC
                    ):
                        raise NativeHttpError(403, "公共 Key 已过期。")
                full_access = row.get("full_access") is True
                if full_access:
                    raise NativeHttpError(
                        403, "公共 Key 必须使用限定资料范围。"
                    )
                capabilities = row.get("capabilities")
                kb_ids = row.get("knowledge_base_ids")
                if (
                    not isinstance(capabilities, list)
                    or "chat" not in capabilities
                ):
                    raise NativeHttpError(403, "公共 Key 无问答权限。")
                if not isinstance(kb_ids, list) or any(
                    not isinstance(item, str) for item in kb_ids
                ):
                    raise NativeHttpError(502, "公共 Key 范围无效。")
                return NativePublicKeyScope(
                    knowledge_base_ids=frozenset(kb_ids),
                )
            raise NativeHttpError(403, "当前公共 Key 不在候选租户。")
        raise NativeHttpError(502, "公共 Key 授权不可用。")

    async def _access_token(self) -> str:
        if self._token is not None:
            return self._token
        async with self._login_lock:
            if self._token is not None:
                return self._token
            try:
                response = await self._http.post(
                    "/api/v1/auth/login",
                    json={"email": self._email, "password": self._password},
                )
            except httpx.RequestError as error:
                raise NativeHttpError(
                    502, "原生管理员登录超时或连接失败。"
                ) from error
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


def _allowed_admin_path(  # noqa: PLR0911, PLR0912 - 权限矩阵逐项拒绝。
    method: str, path: str, *, allow_public_agent_create: bool = False
) -> bool:
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
    if category == "tenants" and segments[4:5] == ["api-keys"]:
        return False
    if category == "agents" and method not in {"GET", "HEAD"}:
        return allow_public_agent_create and segments == [
            "api", "v1", "agents"
        ] and method == "POST"
    # 管理壳只读查看记录，不能借原生代理新建或继续一条生成。
    if category in {"knowledge-chat", "agent-chat"}:
        return False
    if category == "sessions":
        if segments[3:4] == ["continue-stream"]:
            return False
        return method in {"GET", "HEAD"}
    if category == "messages":
        return method in {"GET", "HEAD"}
    if category == "auth":
        return method == "GET" and segments[3:] == ["config"]
    if category not in _ADMIN_CATEGORIES:
        return False
    if path.startswith("api/v1/system/admin/"):
        return False
    if path.startswith("api/v1/system/host-project-dir"):
        return False
    return not path.startswith("api/v1/initialization/ollama/models/download")
