"""P10.5 Product Runtime 的会话、模型服务与检索方案 API。"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.responses import Response as StarletteResponse

from rag_app.api.p09 import create_p09_app
from rag_app.api.product_token_policy import resolve_token_route
from rag_app.api.provider_budget import register_provider_budget_routes
from rag_app.composition.product_runtime import (
    ProductRuntime,
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.core.errors import PolicyDenied
from rag_app.product.auth import SESSION_COOKIE
from rag_app.product.catalog import provider_catalog
from rag_app.product.control_store import validate_connection_metadata
from rag_app.product.http_security import RequestRateLimiter
from rag_app.product.models import (
    ImpactKind,
    ProviderConnectionDraft,
    RetrievalProfileDraft,
)
from rag_app.product.provider_runtime import TransportFactory
from rag_app.product.verification import validation_is_current

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_INTERNAL_QUERY_PREFIX = "internal-query-"
_INTERNAL_ADMIN_PREFIX = "internal-admin-"
_HTTP_TOO_MANY_REQUESTS = 429
_RATE_LIMITS = {
    "provider-test": 5,
    "query": 60,
    "upload": 10,
}
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


@dataclass(frozen=True, slots=True)
class _AuthConfig:
    """Product API 内外部认证桥接配置。"""

    internal_query: str
    internal_admin: str
    legacy_query: str | None
    legacy_admin: str | None


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(_RequestModel):
    """Bootstrap Secret 登录请求。"""

    bootstrap_token: str = Field(min_length=16, max_length=4096)


class CredentialRequest(_RequestModel):
    """创建环境或数据库托管 Credential。"""

    provider_type: Literal["jina", "aliyun-model-studio"]
    source: Literal["environment_managed", "database_encrypted"]
    environment_name: str | None = None
    secret_value: str | None = Field(
        default=None, min_length=1, max_length=4096
    )


class RotateCredentialRequest(_RequestModel):
    """页面托管 Credential 轮换请求。"""

    secret_value: str = Field(min_length=1, max_length=4096)


class ConnectionRequest(_RequestModel):
    """Provider Connection 非 Secret 配置。"""

    display_name: str = Field(min_length=1, max_length=200)
    provider_type: Literal["jina", "aliyun-model-studio"]
    credential_id: str | None = None
    credential: CredentialRequest | None = None
    endpoint_profile: Literal["default"] = "default"
    endpoint_mode: Literal["workspace_host", "beijing_dashscope"] = (
        "workspace_host"
    )
    api_host: str | None = Field(default=None, max_length=300)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=200)
    region: Literal["cn-beijing"] | None = None
    request_budget: int = Field(default=5, ge=1, le=20)
    token_budget: int = Field(default=4096, ge=1, le=1_000_000)


class ConnectionPatchRequest(_RequestModel):
    """版本受控的非 Secret 连接编辑，未知字段一律拒绝。"""

    expected_version: int = Field(gt=0)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=200)
    endpoint_mode: Literal["workspace_host", "beijing_dashscope"] | None = None
    api_host: str | None = Field(default=None, max_length=300)
    region: Literal["cn-beijing"] | None = None
    request_budget: int | None = Field(default=None, ge=1, le=20)
    token_budget: int | None = Field(default=None, ge=1, le=1_000_000)
    enabled: bool | None = None


class ValidationRequest(_RequestModel):
    """单项 Provider 验证请求。"""

    operation: Literal["embedding.document", "embedding.query", "reranking"]
    model: str = Field(min_length=1, max_length=200)
    expected_dimension: int | None = Field(default=None, gt=0)
    request_policy: dict[str, object] = Field(default_factory=dict)


class RetrievalProfileRequest(_RequestModel):
    """知识库级 Retrieval Profile Draft。"""

    primary_connection_id: str
    primary_embedding_model: str = Field(min_length=1, max_length=200)
    primary_dimension: int = Field(gt=0)
    primary_document_policy: dict[str, object] = Field(default_factory=dict)
    primary_query_policy: dict[str, object] = Field(default_factory=dict)
    standby_connection_id: str | None = None
    standby_embedding_model: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    standby_dimension: int | None = Field(default=None, gt=0)
    standby_document_policy: dict[str, object] = Field(default_factory=dict)
    standby_query_policy: dict[str, object] = Field(default_factory=dict)
    reranker_connection_id: str | None = None
    reranker_model: str | None = Field(
        default=None, min_length=1, max_length=200
    )
    failover_enabled: bool = False
    standby_budget: dict[str, object] = Field(default_factory=dict)
    retrieval_policy: dict[str, object] = Field(default_factory=dict)
    evidence_policy: dict[str, object] = Field(default_factory=dict)


class ActivateProfileRequest(_RequestModel):
    """带用户确认结果的 Profile 激活请求。"""

    confirmed_impact: ImpactKind


class AccessTokenRequest(_RequestModel):
    """创建作用域 API Token 请求。"""

    name: str = Field(min_length=1, max_length=200)
    scopes: tuple[
        Literal[
            "query:read",
            "knowledge:read",
            "knowledge:write",
            "system:read",
        ],
        ...,
    ] = Field(min_length=1)
    project_id: str | None = None
    knowledge_base_id: str | None = None
    expires_at: str | None = None


def create_product_app(
    runtime: ProductRuntime,
    *,
    query_token: str | None = None,
    admin_token: str | None = None,
) -> FastAPI:
    """创建 Product API、管理员会话与 React 静态宿主。

    Args:
        runtime: 已完成 migration 和兼容性检查的 Product Runtime。
        query_token: 可选迁移期外部 Query Bearer Token。
        admin_token: 可选迁移期外部 Admin Bearer Token。

    Returns:
        同源提供 API 与 React 的 FastAPI 应用。

    Raises:
        FileNotFoundError: 前端构建目录不完整。

    """
    internal_query = f"{_INTERNAL_QUERY_PREFIX}{secrets.token_urlsafe(32)}"
    internal_admin = f"{_INTERNAL_ADMIN_PREFIX}{secrets.token_urlsafe(32)}"
    app = create_p09_app(
        runtime.p09,
        query_token=internal_query,
        admin_token=internal_admin,
        debug_enabled=runtime.settings.debug_enabled,
    )
    _register_auth_middleware(
        app,
        runtime,
        _AuthConfig(
            internal_query=internal_query,
            internal_admin=internal_admin,
            legacy_query=query_token,
            legacy_admin=admin_token,
        ),
    )
    _register_product_routes(app, runtime)
    _mount_frontend(app, runtime.settings.frontend_dir)
    return app


def create_product_lifespan_app(
    settings: ProductRuntimeSettings,
    *,
    query_token: str | None = None,
    admin_token: str | None = None,
    transport_factory: TransportFactory | None = None,
) -> FastAPI:
    """使用 FastAPI Lifespan 构建并关闭唯一 Product Runtime。

    Args:
        settings: 最小产品启动配置。
        query_token: 可选迁移期 Query Bearer Token。
        admin_token: 可选迁移期 Admin Bearer Token。
        transport_factory: 测试用 Provider MockTransport 工厂。

    Returns:
        延迟到 startup 构造资源的外层 FastAPI 应用。

    """

    @asynccontextmanager
    async def _lifespan(outer: FastAPI) -> AsyncIterator[None]:
        runtime = build_product_runtime(
            settings,
            transport_factory=transport_factory,
        )
        inner = create_product_app(
            runtime,
            query_token=query_token,
            admin_token=admin_token,
        )
        outer.state.product_runtime = runtime
        outer.mount("/", inner)
        try:
            yield
        finally:
            runtime.close()

    return FastAPI(
        title="Universal RAG Product Runtime",
        version="1.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )


def _register_auth_middleware(
    app: FastAPI,
    runtime: ProductRuntime,
    config: _AuthConfig,
) -> None:
    limiter = RequestRateLimiter()

    @app.middleware("http")
    async def _product_auth(
        request: Request,
        call_next: Callable[[Request], Awaitable[StarletteResponse]],
    ) -> StarletteResponse:
        security_error = _request_security_error(request, runtime, limiter)
        if security_error is not None:
            return _apply_security_headers(request, security_error, runtime)
        auth_error = _authenticate_request(request, runtime, config)
        if auth_error is not None:
            return _apply_security_headers(request, auth_error, runtime)
        response = await call_next(request)
        return _apply_security_headers(request, response, runtime)


def _request_security_error(
    request: Request,
    runtime: ProductRuntime,
    limiter: RequestRateLimiter,
) -> StarletteResponse | None:
    hostname = request.url.hostname
    peer = request.client.host if request.client else ""
    loopback_request = _is_loopback(hostname) and (
        _is_loopback(peer) or runtime.settings.trust_loopback_host_proxy
    )
    if not loopback_request and _effective_scheme(request, runtime) != "https":
        return _policy_error(400, "TLS_REQUIRED", "非本机访问必须使用 HTTPS。")
    if request.method not in _SAFE_METHODS:
        origin = request.headers.get("Origin")
        if (
            origin is not None
            and origin.rstrip("/") not in runtime.settings.trusted_origins
        ):
            return _policy_error(403, "ORIGIN_DENIED", "请求来源不受信任。")
    bucket = _rate_limit_bucket(request.url.path, request.method)
    if bucket is None:
        return None
    if limiter.allow(peer or "unknown", bucket, limit=_RATE_LIMITS[bucket]):
        return None
    response = _policy_error(
        _HTTP_TOO_MANY_REQUESTS,
        "RATE_LIMITED",
        "请求过于频繁。",
    )
    response.headers["Retry-After"] = "60"
    return response


def _rate_limit_bucket(path: str, method: str) -> str | None:
    if method == "POST" and path.endswith(":validate"):
        return "provider-test"
    if method == "POST" and path.endswith((":search", ":answer")):
        return "query"
    if method == "POST" and "/documents" in path:
        return "upload"
    return None


def _effective_scheme(request: Request, runtime: ProductRuntime) -> str:
    peer = request.client.host if request.client else ""
    if peer in runtime.settings.trusted_proxies:
        forwarded = request.headers.get("X-Forwarded-Proto", "")
        if forwarded in {"http", "https"}:
            return forwarded
    return request.url.scheme


def _apply_security_headers(
    request: Request,
    response: StarletteResponse,
    runtime: ProductRuntime,
) -> StarletteResponse:
    response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if _effective_scheme(request, runtime) == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


def _authenticate_request(
    request: Request,
    runtime: ProductRuntime,
    config: _AuthConfig,
) -> StarletteResponse | None:
    """验证 Cookie 或外部 Token，并注入进程内 P09 Token。"""
    path = request.url.path
    if not path.startswith("/api/v1") or (
        path == "/api/v1/console/session" and request.method == "POST"
    ):
        return None
    query_route = path.endswith((":search", ":answer"))
    expected = config.internal_query if query_route else config.internal_admin
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return _authenticate_session(request, runtime, cookie, expected)
    token = _bearer_token(request.headers.get("Authorization"))
    if token is None:
        return _auth_error(401, "AUTHENTICATION_REQUIRED")
    legacy = config.legacy_query if query_route else config.legacy_admin
    if legacy is not None and hmac.compare_digest(token, legacy):
        _replace_authorization(request, expected)
        return None
    try:
        route = resolve_token_route(request, runtime)
        principal = runtime.auth.authorize_access_token(
            token,
            required_scope=route.scope,
            project_id=route.project_id,
            knowledge_base_id=route.knowledge_base_id,
        )
    except PolicyDenied:
        return _auth_error(403, "TOKEN_DENIED")
    request.state.product_principal = "external_token"
    request.state.access_token_id = principal.token_id
    _replace_authorization(request, expected)
    return None


def _authenticate_session(
    request: Request,
    runtime: ProductRuntime,
    cookie: str,
    expected: str,
) -> StarletteResponse | None:
    """验证管理员会话与写操作 CSRF。"""
    csrf = None
    if request.method not in _SAFE_METHODS:
        csrf = request.headers.get("X-CSRF-Token")
        if csrf is None:
            return _auth_error(403, "CSRF_REQUIRED")
    try:
        runtime.auth.validate_session(cookie, csrf_token=csrf)
    except PolicyDenied:
        return _auth_error(401, "CONSOLE_SESSION_REQUIRED")
    request.state.product_principal = "admin_session"
    _replace_authorization(request, expected)
    return None


def _register_product_routes(app: FastAPI, runtime: ProductRuntime) -> None:
    _register_session_routes(app, runtime)
    _register_provider_routes(app, runtime)
    _register_profile_routes(app, runtime)
    _register_access_token_routes(app, runtime)
    register_provider_budget_routes(app, runtime)


def _register_session_routes(app: FastAPI, runtime: ProductRuntime) -> None:
    @app.post("/api/v1/console/session", tags=["console-session"])
    def _login(
        body: SessionRequest,
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        client_key = request.client.host if request.client else "unknown"
        session_id, token, csrf = runtime.sessions.login(
            body.bootstrap_token,
            client_key,
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=not _is_loopback(request.url.hostname),
            samesite="lax",
            max_age=runtime.sessions.ttl_seconds,
            path="/",
        )
        return {
            "session_id": session_id,
            "csrf_token": csrf,
            "expires_in": runtime.sessions.ttl_seconds,
        }

    @app.get("/api/v1/console/session", tags=["console-session"])
    def _session(request: Request, response: Response) -> dict[str, object]:
        token = request.cookies.get(SESSION_COOKIE)
        if token is None:
            raise PolicyDenied("管理员会话无效。", stage="console.resume")
        session_id, replacement, csrf = runtime.sessions.resume(token)
        response.set_cookie(
            SESSION_COOKIE,
            replacement,
            httponly=True,
            secure=not _is_loopback(request.url.hostname),
            samesite="lax",
            max_age=runtime.sessions.ttl_seconds,
            path="/",
        )
        return {
            "authenticated": True,
            "session_id": session_id,
            "csrf_token": csrf,
            "expires_in": runtime.sessions.ttl_seconds,
        }

    @app.post("/api/v1/console/session:rotate", tags=["console-session"])
    def _rotate_session(
        request: Request, response: Response
    ) -> dict[str, object]:
        token = request.cookies.get(SESSION_COOKIE)
        csrf = request.headers.get("X-CSRF-Token")
        if token is None or csrf is None:
            raise PolicyDenied("管理员会话验证失败。", stage="console.rotate")
        session_id, replacement, next_csrf = runtime.sessions.rotate(
            token, csrf
        )
        response.set_cookie(
            SESSION_COOKIE,
            replacement,
            httponly=True,
            secure=not _is_loopback(request.url.hostname),
            samesite="lax",
            max_age=runtime.sessions.ttl_seconds,
            path="/",
        )
        return {
            "session_id": session_id,
            "csrf_token": next_csrf,
            "expires_in": runtime.sessions.ttl_seconds,
        }

    @app.delete("/api/v1/console/session", tags=["console-session"])
    def _logout(request: Request, response: Response) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            runtime.auth.revoke_session(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.status_code = 204
        return response


def _register_provider_routes(app: FastAPI, runtime: ProductRuntime) -> None:
    @app.get("/api/v1/provider-catalog", tags=["model-services"])
    def _catalog() -> dict[str, object]:
        return provider_catalog()

    @app.get("/api/v1/provider-credentials", tags=["model-services"])
    def _credentials() -> dict[str, object]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in runtime.credentials.list()
            ]
        }

    @app.post(
        "/api/v1/provider-credentials",
        tags=["model-services"],
        status_code=201,
    )
    def _create_credential(body: CredentialRequest) -> dict[str, object]:
        if body.source == "environment_managed":
            if body.environment_name is None or body.secret_value is not None:
                raise ValueError("环境托管只接受 environment_name。")
            credential = runtime.credentials.create_environment(
                body.provider_type,
                body.environment_name,
            )
        else:
            if body.secret_value is None or body.environment_name is not None:
                raise ValueError("页面托管只接受 secret_value。")
            credential = runtime.credentials.create_encrypted(
                body.provider_type,
                body.secret_value,
            )
        return credential.model_dump(mode="json")

    @app.post(
        "/api/v1/provider-credentials/{credential_id}:rotate",
        tags=["model-services"],
    )
    def _rotate_credential(
        credential_id: str,
        body: RotateCredentialRequest,
    ) -> dict[str, object]:
        credential = runtime.credentials.rotate(
            credential_id, body.secret_value
        )
        runtime.providers.invalidate_credential(credential_id)
        runtime.profiles.invalidate()
        return credential.model_dump(mode="json")

    @app.get("/api/v1/provider-connections", tags=["model-services"])
    def _connections() -> dict[str, object]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in runtime.control.list_connections()
            ]
        }

    @app.post(
        "/api/v1/provider-connections",
        tags=["model-services"],
        status_code=201,
    )
    def _create_connection(body: ConnectionRequest) -> dict[str, object]:
        if (body.credential_id is None) == (body.credential is None):
            raise ValueError("请选择已有凭据或提供新凭据。")
        draft = validate_connection_metadata(
            ProviderConnectionDraft(
                **body.model_dump(exclude={"credential", "credential_id"}),
                credential_id=body.credential_id or "pending-new-credential",
            )
        )
        if body.credential is None:
            return runtime.control.create_connection(draft).model_dump(
                mode="json"
            )
        if body.credential.provider_type != body.provider_type:
            raise ValueError("Credential 与 Provider 类型不匹配。")
        credential = _create_credential(body.credential)
        credential_id = str(credential["credential_id"])
        try:
            connection = runtime.control.create_connection(
                draft.model_copy(update={"credential_id": credential_id})
            )
        except Exception:
            # 只补偿本次调用新建且未被引用的凭据；保留已有共享凭据。
            runtime.credentials.remove_new_orphan(credential_id)
            raise
        return connection.model_dump(mode="json")

    @app.patch(
        "/api/v1/provider-connections/{connection_id}", tags=["model-services"]
    )
    def _update_connection(
        connection_id: str, body: ConnectionPatchRequest
    ) -> dict[str, object]:
        connection = runtime.control.update_connection(
            connection_id,
            expected_version=body.expected_version,
            changes=body.model_dump(
                exclude_unset=True, exclude={"expected_version"}
            ),
        )
        runtime.providers.invalidate_connection(connection_id)
        runtime.profiles.invalidate()
        return connection.model_dump(mode="json")

    @app.post(
        "/api/v1/provider-connections/{connection_id}:validate",
        tags=["model-services"],
    )
    def _validate_connection(
        connection_id: str,
        body: ValidationRequest,
    ) -> dict[str, object]:
        result = runtime.providers.validate(connection_id, **body.model_dump())
        return result.model_dump(mode="json")

    @app.get(
        "/api/v1/provider-connections/{connection_id}/validations",
        tags=["model-services"],
    )
    def _validations(connection_id: str) -> dict[str, object]:
        connection = runtime.control.get_connection(connection_id)
        credential = runtime.credentials.get(connection.credential_id)
        return {
            "items": [
                {
                    **item.model_dump(mode="json"),
                    "is_current": validation_is_current(
                        item, connection, credential.key_version
                    ),
                }
                for item in runtime.control.list_validations(connection_id)
            ]
        }

    @app.get("/api/v1/provider-usage/daily", tags=["model-services"])
    def _daily_provider_usage() -> dict[str, object]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in runtime.control.list_daily_provider_usage()
            ]
        }


def _register_profile_routes(app: FastAPI, runtime: ProductRuntime) -> None:
    @app.get(
        "/api/v1/knowledge-bases/{knowledge_base_id}/retrieval-profiles",
        tags=["retrieval-profiles"],
    )
    def _profiles(knowledge_base_id: str) -> dict[str, object]:
        return {
            "items": [
                {
                    **item.model_dump(mode="json"),
                    "effective_serving_fingerprint": (
                        runtime.profiles.serving_contract(item)[2]
                    ),
                }
                for item in runtime.control.list_profiles(knowledge_base_id)
            ]
        }

    @app.post(
        "/api/v1/knowledge-bases/{knowledge_base_id}/retrieval-profiles",
        tags=["retrieval-profiles"],
        status_code=201,
    )
    def _create_profile(
        knowledge_base_id: str,
        body: RetrievalProfileRequest,
    ) -> dict[str, object]:
        try:
            profile = runtime.control.create_profile(
                RetrievalProfileDraft(
                    knowledge_base_id=knowledge_base_id, **body.model_dump()
                )
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return {
            **profile.model_dump(mode="json"),
            "effective_serving_fingerprint": runtime.profiles.serving_contract(
                profile
            )[2],
        }

    @app.get(
        "/api/v1/retrieval-profiles/{profile_revision_id}:preview",
        tags=["retrieval-profiles"],
    )
    def _preview_profile(profile_revision_id: str) -> dict[str, object]:
        return runtime.control.preview_impact(profile_revision_id).model_dump(
            mode="json"
        )

    @app.post(
        "/api/v1/retrieval-profiles/{profile_revision_id}:activate",
        tags=["retrieval-profiles"],
    )
    def _activate_profile(
        profile_revision_id: str,
        body: ActivateProfileRequest,
    ) -> dict[str, object]:
        profile = runtime.control.activate_profile(
            profile_revision_id,
            confirmed_impact=body.confirmed_impact,
        )
        if profile.activation_job_id is not None and profile.status == "draft":
            runtime.jobs.submit(profile.activation_job_id)
        return {
            **profile.model_dump(mode="json"),
            "effective_serving_fingerprint": runtime.profiles.serving_contract(
                profile
            )[2],
        }


def _register_access_token_routes(
    app: FastAPI, runtime: ProductRuntime
) -> None:
    @app.get("/api/v1/access-tokens", tags=["access-tokens"])
    def _tokens() -> dict[str, object]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in runtime.auth.list_access_tokens()
            ]
        }

    @app.post("/api/v1/access-tokens", tags=["access-tokens"], status_code=201)
    def _create_token(body: AccessTokenRequest) -> dict[str, object]:
        return runtime.auth.create_access_token(**body.model_dump()).model_dump(
            mode="json"
        )

    @app.post(
        "/api/v1/access-tokens/{token_id}:revoke",
        tags=["access-tokens"],
    )
    def _revoke_token(token_id: str) -> dict[str, object]:
        return runtime.auth.revoke_access_token(token_id).model_dump(
            mode="json"
        )


def _mount_frontend(app: FastAPI, frontend_dir: Path) -> None:
    root = frontend_dir.resolve()
    index = root / "index.html"
    assets = root / "assets"
    if not index.is_file() or not assets.is_dir():
        raise FileNotFoundError("产品前端构建目录缺少 index.html 或 assets。")
    app.mount("/assets", StaticFiles(directory=assets), name="product-assets")

    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    @app.get("/{ui_path:path}", include_in_schema=False)
    def _spa(ui_path: str) -> FileResponse:
        if ui_path in {"live", "ready"} or ui_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(index, headers={"Cache-Control": "no-store"})


def _replace_authorization(request: Request, token: str) -> None:
    headers = MutableHeaders(scope=request.scope)
    headers["Authorization"] = f"Bearer {token}"


def _bearer_token(header: str | None) -> str | None:
    if header is None:
        return None
    scheme, separator, token = header.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        return None
    return token


def _auth_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": "身份验证失败。",
                "stage": "http.auth",
                "retryable": False,
                "trace_id": "",
                "details": {},
            }
        },
    )


def _policy_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "stage": "http.security",
                "retryable": status_code == _HTTP_TOO_MANY_REQUESTS,
                "trace_id": "",
                "details": {},
            }
        },
    )


def _is_loopback(hostname: str | None) -> bool:
    return hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
        "testclient",
        "testserver",
    }


__all__ = [
    "create_product_app",
    "create_product_lifespan_app",
]
