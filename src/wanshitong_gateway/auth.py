"""湾事通网关的公共与管理员会话边界。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from wanshitong_gateway.compat.adapters.stores.sqlite_connection import (
    SqliteConnectionFactory,
)
from wanshitong_gateway.compat.core.errors import PolicyDenied
from wanshitong_gateway.compat.product.auth import (
    AuthStore,
    ConsoleSessionService,
    load_bootstrap_token,
)
from wanshitong_gateway.compat.product.crypto import (
    MasterKey,
    SecretCipher,
    load_master_key,
)
from wanshitong_gateway.compat.product.http_security import (
    secure_cookie_for_request,
)
from wanshitong_gateway.compat.wanshitong.public_models import (
    PublicSessionRequest,
    PublicSessionResponse,
    PublicSessionUser,
)
from wanshitong_gateway.compat.wanshitong.public_session import (
    PublicSessionPrincipal,
    PublicSessionProvider,
    PublicSessionService,
)
from wanshitong_gateway.compat.wanshitong.sso_api import register_sso_routes
from wanshitong_gateway.compat.wanshitong.sso_client import (
    SsoClient,
    load_client_secret,
)
from wanshitong_gateway.compat.wanshitong.sso_session import (
    SsoIdentity,
    SsoSessionService,
)
from wanshitong_gateway.roles import (
    Role,
    RoleChangeDeniedError,
    UserProfile,
    UserStore,
    editable_roles,
    role_edit_note,
)
from wanshitong_gateway.settings import GatewayAuthSettings
from wanshitong_gateway.weknora.client import NativeHttpError

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_NO_STORE = "no-store"


class _AdminLoginRequest(BaseModel):
    """兼容原有 Console 登录请求，同时拒绝额外字段。"""

    model_config = ConfigDict(extra="forbid")

    bootstrap_token: str = Field(min_length=1, max_length=4096)


class _RoleUpdateRequest(BaseModel):
    """管理员只能选择预定义的三个角色。"""

    model_config = ConfigDict(extra="forbid")

    role: Role


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    """独立管理员会话的最小审计身份。"""

    session_id: str

    @property
    def audit_actor(self) -> str:
        """返回不暴露 Cookie 或原始会话 ID 的审计标识。"""
        digest = hashlib.sha256(self.session_id.encode()).hexdigest()[:16]
        return f"admin_session:{digest}"


@dataclass(slots=True)
class GatewayAuth:
    """共享公共登录和独立管理员会话，不构造旧 Product Runtime。"""

    settings: GatewayAuthSettings
    public_sessions: PublicSessionProvider
    admin_store: AuthStore
    admin_sessions: ConsoleSessionService
    sso_client: SsoClient | None
    users: UserStore | None = None

    def __post_init__(self) -> None:
        """认证和业务用户共用当前网关的独立数据库。"""
        if self.users is None:
            self.users = UserStore(self.settings.database_path)

    def profile_for_principal(
        self, principal: PublicSessionPrincipal
    ) -> UserProfile:
        """读取本地角色，兼容升级前已签发的受信用户会话。"""
        if principal.user_id is None:
            raise HTTPException(
                status_code=401, detail="employee login required"
            )
        if self.users is None:
            raise AssertionError("用户资料库未初始化。")
        profile = self.users.get(principal.user_id)
        if profile is not None:
            return profile
        return self.users.record_identity(
            SsoIdentity(
                user_id=principal.user_id,
                username=principal.username,
                nick_name=principal.nick_name,
                dept_id=principal.dept_id,
                roles=(),
                permissions=(),
            )
        )

    def require_profile(
        self, request: Request, *, csrf: bool | None = None
    ) -> UserProfile:
        """认证真实员工，并在每次请求读取最新角色。"""
        return self.profile_for_principal(
            self.require_public(request, csrf=csrf)
        )

    @classmethod
    def from_settings(cls, settings: GatewayAuthSettings) -> GatewayAuth:
        """从候选专用 Secret 与 SQLite 构造会话服务。

        Args:
            settings: 已验证部署、SSO 入口与数据库路径的配置。

        Returns:
            可注册 HTTP 路由的认证服务。

        """
        master_key = load_master_key(settings.master_key_file)
        bootstrap_token = load_bootstrap_token(settings.bootstrap_token_file)
        connections = SqliteConnectionFactory(settings.database_path)
        _initialize_admin_schema(connections)
        admin_store = AuthStore(
            connections, SecretCipher(_authentication_key(bootstrap_token))
        )
        admin_sessions = ConsoleSessionService(admin_store, bootstrap_token)
        if settings.sso.enabled:
            sso = settings.sso
            if (
                sso.deployment_id is None
                or sso.validate_url is None
                or sso.client_id is None
                or sso.client_secret_file is None
            ):
                raise ValueError("SSO 配置不完整。")
            public_sessions: PublicSessionProvider = SsoSessionService(
                master_key,
                deployment_id=sso.deployment_id,
                base_path=sso.base_path,
            )
            sso_client = SsoClient(
                validate_url=sso.validate_url,
                client_id=sso.client_id,
                client_secret=load_client_secret(sso.client_secret_file),
            )
        else:
            public_sessions = PublicSessionService(master_key)
            sso_client = None
        return cls(
            settings=settings,
            public_sessions=public_sessions,
            admin_store=admin_store,
            admin_sessions=admin_sessions,
            sso_client=sso_client,
        )

    def verify_origin(self, request: Request) -> None:
        """拒绝浏览器跨站写请求，不信任任意转发 Origin。"""
        if request.method in _SAFE_METHODS:
            return
        origin = request.headers.get("Origin")
        if (
            origin is not None
            and origin.rstrip("/") not in self.settings.trusted_origins
        ):
            raise HTTPException(status_code=403, detail="origin denied")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise HTTPException(status_code=403, detail="cross-site denied")

    def require_public(
        self, request: Request, *, csrf: bool | None = None
    ) -> PublicSessionPrincipal:
        """验证公共 Cookie，并按路由要求核对页面内存中的 CSRF。"""
        self.verify_origin(request)
        cookie = request.cookies.get(self.public_sessions.cookie_name)
        if cookie is None:
            raise HTTPException(
                status_code=401, detail="public session required"
            )
        try:
            principal = self.public_sessions.validate_cookie(cookie)
        except PolicyDenied:
            raise HTTPException(
                status_code=401, detail="public session invalid"
            ) from None
        check_csrf = (
            request.method not in _SAFE_METHODS if csrf is None else csrf
        )
        if check_csrf:
            token = request.headers.get("X-CSRF-Token")
            if token is None:
                raise HTTPException(
                    status_code=403, detail="public csrf required"
                )
            try:
                principal = self.public_sessions.authenticate(cookie, token)
            except PolicyDenied:
                raise HTTPException(
                    status_code=403, detail="public csrf invalid"
                ) from None
        return principal

    def require_admin(
        self, request: Request, *, csrf: bool | None = None
    ) -> AdminPrincipal:
        """验证候选独立管理员 Cookie、吊销状态及写操作 CSRF。"""
        self.verify_origin(request)
        cookie = request.cookies.get(self.settings.admin_cookie_name)
        if cookie is None:
            principal = self.require_public(request, csrf=False)
            if self.profile_for_principal(principal).role != "admin":
                raise HTTPException(
                    status_code=401, detail="admin session required"
                )
            self.require_public(request, csrf=csrf)
            return AdminPrincipal(session_id=principal.session_id)
        check_csrf = (
            request.method not in _SAFE_METHODS if csrf is None else csrf
        )
        token = request.headers.get("X-CSRF-Token") if check_csrf else None
        if check_csrf and token is None:
            raise HTTPException(status_code=403, detail="admin csrf required")
        try:
            session_id = self.admin_store.validate_session(
                cookie, csrf_token=token
            )
        except PolicyDenied:
            raise HTTPException(
                status_code=401, detail="admin session invalid"
            ) from None
        return AdminPrincipal(session_id=session_id)


def register_auth_routes(
    app: FastAPI,
    auth: GatewayAuth,
    *,
    before_role_change: Callable[[UserProfile, Role], Awaitable[None]]
    | None = None,
) -> None:
    """注册与现有湾事通页面兼容的公共和管理员会话路由。"""
    if auth.settings.sso.enabled:
        if not isinstance(auth.public_sessions, SsoSessionService):
            raise AssertionError("SSO 会话类型不匹配。")
        if auth.sso_client is None:
            raise AssertionError("SSO 客户端未初始化。")
        register_sso_routes(
            app,
            settings=auth.settings.sso,
            sessions=auth.public_sessions,
            client=auth.sso_client,
            trusted_proxies=auth.settings.trusted_proxies,
            on_identity=(
                auth.users.record_identity if auth.users is not None else None
            ),
        )

    @app.post("/api/public/session", response_model=PublicSessionResponse)
    def public_session(
        request: Request,
        response: Response,
        body: PublicSessionRequest | None = None,
    ) -> PublicSessionResponse:
        del body
        auth.verify_origin(request)
        _reject_query_parameters(request)
        try:
            issue = auth.public_sessions.bootstrap(
                request.cookies.get(auth.public_sessions.cookie_name)
            )
        except PolicyDenied:
            raise HTTPException(
                status_code=401, detail="public login required"
            ) from None
        if auth.public_sessions.set_cookie_on_bootstrap:
            response.set_cookie(
                auth.public_sessions.cookie_name,
                issue.cookie_value,
                httponly=True,
                secure=secure_cookie_for_request(
                    request, trusted_proxies=auth.settings.trusted_proxies
                ),
                samesite=auth.public_sessions.cookie_samesite,
                max_age=issue.expires_in,
                path=auth.public_sessions.cookie_path,
            )
        response.headers["Cache-Control"] = _NO_STORE
        user = None
        if issue.principal.user_id is not None:
            profile = auth.profile_for_principal(issue.principal)
            user = PublicSessionUser(
                user_id=profile.user_id,
                username=profile.username,
                display_name=profile.display_name,
                dept_id=profile.dept_id,
                role=profile.role,
                is_super_admin=profile.is_super_admin,
            )
        return PublicSessionResponse(
            session_id=issue.principal.session_id,
            csrf_token=issue.csrf_token,
            expires_in=issue.expires_in,
            user=user,
            deployment_id=auth.public_sessions.deployment_id,
        )

    _register_console_routes(app, auth)
    _register_user_routes(app, auth, before_role_change=before_role_change)


def _register_console_routes(app: FastAPI, auth: GatewayAuth) -> None:
    """兼容旧管理员口令和员工 SSO 两种管理端会话。"""

    @app.post("/api/v1/console/session")
    def admin_login(
        request: Request,
        response: Response,
        body: _AdminLoginRequest | None = None,
    ) -> dict[str, object]:
        auth.verify_origin(request)
        _reject_query_parameters(request)
        if body is None:
            auth.require_admin(request, csrf=False)
            response.headers["Cache-Control"] = _NO_STORE
            return _sso_admin_session_payload(request, auth)
        client_key = request.client.host if request.client else "unknown"
        try:
            session_id, cookie, csrf = auth.admin_sessions.login(
                body.bootstrap_token, client_key
            )
        except PolicyDenied:
            raise HTTPException(
                status_code=401, detail="admin login failed"
            ) from None
        _set_admin_cookie(request, response, auth, cookie)
        return _admin_session_payload(session_id, csrf, auth)

    @app.get("/api/v1/console/session")
    def admin_resume(request: Request, response: Response) -> dict[str, object]:
        auth.require_admin(request)
        cookie = request.cookies.get(auth.settings.admin_cookie_name)
        if cookie is None:
            response.headers["Cache-Control"] = _NO_STORE
            return {
                "authenticated": True,
                **_sso_admin_session_payload(request, auth),
            }
        session_id, replacement, csrf = auth.admin_sessions.resume(cookie)
        _set_admin_cookie(request, response, auth, replacement)
        return {
            "authenticated": True,
            **_admin_session_payload(session_id, csrf, auth),
        }

    @app.post("/api/v1/console/session:rotate")
    def admin_rotate(request: Request, response: Response) -> dict[str, object]:
        auth.require_admin(request)
        cookie = request.cookies.get(auth.settings.admin_cookie_name)
        if cookie is None:
            response.headers["Cache-Control"] = _NO_STORE
            return _sso_admin_session_payload(request, auth)
        csrf = request.headers["X-CSRF-Token"]
        session_id, replacement, next_csrf = auth.admin_sessions.rotate(
            cookie, csrf
        )
        _set_admin_cookie(request, response, auth, replacement)
        return _admin_session_payload(session_id, next_csrf, auth)

    @app.delete("/api/v1/console/session", status_code=204)
    def admin_logout(request: Request, response: Response) -> Response:
        auth.require_admin(request)
        cookie = request.cookies.get(auth.settings.admin_cookie_name)
        if cookie is not None:
            auth.admin_store.revoke_session(cookie)
        else:
            response.delete_cookie(
                auth.public_sessions.cookie_name,
                path=auth.public_sessions.cookie_path,
            )
        response.delete_cookie(
            auth.settings.admin_cookie_name, path=auth.settings.root_path
        )
        response.headers["Cache-Control"] = _NO_STORE
        response.status_code = 204
        return response


def _register_user_routes(
    app: FastAPI,
    auth: GatewayAuth,
    *,
    before_role_change: Callable[[UserProfile, Role], Awaitable[None]] | None,
) -> None:
    """注册角色管理；先撤后台执行权限，再提交新的业务角色。"""
    role_change_lock = asyncio.Lock()

    @app.get("/api/ops/users")
    def list_users(request: Request, response: Response) -> dict[str, object]:
        auth.require_admin(request)
        actor = auth.require_profile(request)
        if auth.users is None:
            raise AssertionError("用户资料库未初始化。")
        response.headers["Cache-Control"] = _NO_STORE
        items: list[dict[str, object]] = []
        for profile in auth.users.list_users():
            item: dict[str, object] = dict(profile.as_dict())
            item["editable_roles"] = list(editable_roles(actor, profile))
            item["role_edit_note"] = role_edit_note(actor, profile)
            items.append(item)
        return {"items": items}

    @app.patch("/api/ops/users/{user_id}")
    async def set_user_role(
        user_id: str,
        body: _RoleUpdateRequest,
        request: Request,
        response: Response,
    ) -> dict[str, str | bool | None]:
        auth.require_admin(request)
        actor = auth.require_profile(request)
        if auth.users is None:
            raise AssertionError("用户资料库未初始化。")
        try:
            async with role_change_lock:
                target = auth.users.get(user_id)
                if target is None:
                    raise KeyError(user_id)
                # 校验通过后才撤工作区权限，防止未授权请求干扰其他员工任务。
                if body.role not in editable_roles(actor, target):
                    raise RoleChangeDeniedError(
                        role_edit_note(actor, target)
                        or "仅超级管理员可以授予管理员角色。"
                    )
                if before_role_change is not None:
                    await before_role_change(target, body.role)
                profile = auth.users.set_role(
                    user_id, body.role, actor_user_id=actor.user_id
                )
        except NativeHttpError as error:
            raise HTTPException(
                status_code=503,
                detail="后台任务权限同步暂不可用，角色尚未保存，请重试。",
            ) from error
        except RoleChangeDeniedError as error:
            raise HTTPException(status_code=403, detail=str(error)) from None
        except KeyError:
            raise HTTPException(
                status_code=404, detail="user not found"
            ) from None
        response.headers["Cache-Control"] = _NO_STORE
        return profile.as_dict()


def _initialize_admin_schema(connections: SqliteConnectionFactory) -> None:
    """只初始化认证拥有的会话表，不运行旧 Product 全量迁移。"""
    with connections.transaction(write=True) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS console_sessions ("
            "session_id TEXT PRIMARY KEY CHECK(session_id GLOB 'sess_*'), "
            "token_hash TEXT NOT NULL UNIQUE, "
            "csrf_hash TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, "
            "rotated_at TEXT, "
            "revoked_at TEXT)"
        )
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(console_sessions)")
        }
        if columns != {
            "session_id",
            "token_hash",
            "csrf_hash",
            "created_at",
            "expires_at",
            "rotated_at",
            "revoked_at",
        }:
            raise ValueError("网关管理员会话表结构不匹配。")


def _authentication_key(bootstrap_token: str) -> MasterKey:
    """与既有 Console Session 使用同一派生域，但数据库独立。"""
    value = hashlib.sha256(
        b"rag-console-auth-key-v1\x00" + bootstrap_token.encode()
    ).digest()
    return MasterKey(
        value=value,
        key_id=f"sha256:{hashlib.sha256(value).hexdigest()}",
    )


def _reject_query_parameters(request: Request) -> None:
    if request.query_params:
        raise HTTPException(status_code=400, detail="query parameters denied")


def _set_admin_cookie(
    request: Request, response: Response, auth: GatewayAuth, value: str
) -> None:
    response.set_cookie(
        auth.settings.admin_cookie_name,
        value,
        httponly=True,
        secure=secure_cookie_for_request(
            request, trusted_proxies=auth.settings.trusted_proxies
        ),
        samesite="lax",
        max_age=auth.admin_sessions.ttl_seconds,
        path=auth.settings.root_path,
    )
    response.headers["Cache-Control"] = _NO_STORE


def _sso_admin_session_payload(
    request: Request, auth: GatewayAuth
) -> dict[str, object]:
    """管理端复用员工会话，不另发绕过角色变更的管理员 Cookie。"""
    auth.require_admin(request, csrf=False)
    cookie = request.cookies.get(auth.public_sessions.cookie_name)
    issue = auth.public_sessions.bootstrap(cookie)
    return {
        "session_id": issue.principal.session_id,
        "csrf_token": issue.csrf_token,
        "expires_in": issue.expires_in,
    }


def _admin_session_payload(
    session_id: str, csrf: str, auth: GatewayAuth
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "csrf_token": csrf,
        "expires_in": auth.admin_sessions.ttl_seconds,
    }


__all__ = ["AdminPrincipal", "GatewayAuth", "register_auth_routes"]
