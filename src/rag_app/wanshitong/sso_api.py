"""湾事通作为 SP 的 SSO entry、callback 与本地 logout。"""

from __future__ import annotations

import hmac
import logging
from html import escape
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response as StarletteResponse

from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.errors import PolicyDenied
from rag_app.product.http_security import effective_request_scheme
from rag_app.wanshitong.public_session import PUBLIC_SESSION_COOKIE
from rag_app.wanshitong.sso_client import SsoClient, SsoValidationError
from rag_app.wanshitong.sso_session import SsoSessionService
from rag_app.wanshitong.sso_settings import SsoEntry, SsoSettings

SSO_ENTRY_PATH = "/sso/entry"
SSO_CALLBACK_PATH = "/sso/callback"
SSO_LOGOUT_PATH = "/sso/logout"
_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_LOGGER = logging.getLogger(__name__)


def register_sso_routes(
    app: FastAPI,
    *,
    runtime: ProductRuntime,
    settings: SsoSettings,
    sessions: SsoSessionService,
    client: SsoClient,
) -> None:
    """注册只在显式 SSO 模式启用的浏览器端点。

    Args:
        app: 已安装 Product 安全中间件的 FastAPI 应用。
        runtime: 用于读取可信代理边界的 Product Runtime。
        settings: 已 fail-fast 校验的有限入口配置。
        sessions: 部署隔离的 pending 与用户会话服务。
        client: 无重试的 RDMS validate 客户端。

    """

    @app.get(
        SSO_ENTRY_PATH,
        include_in_schema=False,
        response_model=None,
    )
    def _entry(request: Request) -> StarletteResponse:
        return _handle_entry(request, runtime, settings, sessions)

    @app.get(
        SSO_CALLBACK_PATH,
        include_in_schema=False,
        response_model=None,
    )
    def _callback(request: Request) -> StarletteResponse:
        return _handle_callback(
            request, runtime, settings, sessions, client
        )

    @app.get(SSO_LOGOUT_PATH, include_in_schema=False)
    def _logout_confirmation() -> HTMLResponse:
        return _logout_page()

    @app.post(SSO_LOGOUT_PATH, include_in_schema=False)
    def _logout(request: Request) -> JSONResponse:
        return _handle_logout(request, sessions)


def _handle_entry(
    request: Request,
    runtime: ProductRuntime,
    settings: SsoSettings,
    sessions: SsoSessionService,
) -> StarletteResponse:
    try:
        entry = _request_entry(request, runtime, settings)
        return_to = _safe_return_to(
            request.query_params.get("return_to"),
            entry=entry,
            base_path=settings.base_path,
        )
    except ValueError as error:
        return _error_page("AUTH_ENTRY_INVALID", str(error), status_code=400)
    issue = sessions.issue_pending(
        service=entry.callback_url,
        return_to=return_to,
        origin_id=entry.entry_id,
    )
    location = f"{entry.authorize_url}?" + urlencode(
        {
            "service": issue.pending.service,
            "state": issue.pending.state,
        }
    )
    response = RedirectResponse(
        location, status_code=302, headers=_NO_STORE_HEADERS
    )
    response.set_cookie(
        sessions.pending_cookie_name,
        issue.cookie_value,
        httponly=True,
        secure=entry.origin.startswith("https://"),
        samesite="lax",
        max_age=issue.pending.expires_at - issue.pending.issued_at,
        path=sessions.pending_cookie_path,
    )
    return response


def _handle_callback(  # noqa: PLR0911 - 安全回调必须显式失败关闭。
    request: Request,
    runtime: ProductRuntime,
    settings: SsoSettings,
    sessions: SsoSessionService,
    client: SsoClient,
) -> StarletteResponse:
    ticket = _single_query_value(request, "ticket")
    state = _single_query_value(request, "state")
    pending_cookie = request.cookies.get(sessions.pending_cookie_name)
    if ticket is None or state is None or pending_cookie is None:
        return _error_page(
            "AUTH_CALLBACK_INVALID",
            "登录回调缺少必要信息，请重新登录。",
            status_code=400,
        )
    try:
        pending = sessions.read_pending(pending_cookie)
    except PolicyDenied:
        invalid_pending = _error_page(
            "AUTH_PENDING_INVALID",
            "登录流程已失效，请重新登录。",
            status_code=400,
        )
        _delete_pending(invalid_pending, sessions)
        return invalid_pending
    if not hmac.compare_digest(state, pending.state):
        # 错误标签页不能清掉另一标签页刚签发的新 pending。
        return _error_page(
            "AUTH_STATE_INVALID",
            "登录状态不匹配，请从湾事通重新进入。",
            status_code=403,
        )
    try:
        entry = settings.entry_by_id(pending.origin_id)
        current_entry = _request_entry(request, runtime, settings)
    except ValueError as error:
        return _error_page("AUTH_ORIGIN_INVALID", str(error), status_code=400)
    if current_entry != entry or pending.service != entry.callback_url:
        return _error_page(
            "AUTH_ORIGIN_INVALID",
            "登录入口与回调不一致，请重新登录。",
            status_code=403,
        )
    try:
        identity = client.validate(
            ticket=ticket,
            service=pending.service,
            state=pending.state,
        )
        issue = sessions.issue_user(identity)
    except SsoValidationError as error:
        _LOGGER.warning(
            "sso_callback_failed code=%s status=%d origin_id=%s",
            error.code,
            error.status_code,
            pending.origin_id,
        )
        validation_error = _error_page(
            error.code, str(error), status_code=error.status_code
        )
        _delete_pending(validation_error, sessions)
        return validation_error
    except ValueError:
        oversized = _error_page(
            "AUTH_SESSION_TOO_LARGE",
            "账号身份信息超过当前会话上限，请联系管理员。",
            status_code=502,
        )
        _delete_pending(oversized, sessions)
        return oversized

    redirect = RedirectResponse(
        pending.return_to,
        status_code=303,
        headers=_NO_STORE_HEADERS,
    )
    redirect.set_cookie(
        sessions.cookie_name,
        issue.cookie_value,
        httponly=True,
        secure=entry.origin.startswith("https://"),
        samesite=sessions.cookie_samesite,
        max_age=issue.expires_in,
        path=sessions.cookie_path,
    )
    _delete_pending(redirect, sessions)
    redirect.delete_cookie(PUBLIC_SESSION_COOKIE, path="/api/public")
    redirect.delete_cookie(
        PUBLIC_SESSION_COOKIE,
        path=f"{settings.base_path}/api/public",
    )
    _LOGGER.info("sso_callback_succeeded origin_id=%s", pending.origin_id)
    return redirect


def _logout_page() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>退出湾事通</title>"
        "<h1>退出湾事通</h1>"
        "<p>请回到湾事通页面使用退出按钮完成本地退出。</p>",
        headers=_NO_STORE_HEADERS,
    )


def _handle_logout(
    request: Request, sessions: SsoSessionService
) -> JSONResponse:
    cookie = request.cookies.get(sessions.cookie_name)
    csrf = request.headers.get("X-CSRF-Token")
    if cookie is None or csrf is None:
        return _logout_error()
    try:
        sessions.authenticate(cookie, csrf)
    except PolicyDenied:
        return _logout_error()
    response = JSONResponse(
        {"status": "logged_out", "scope": "kb_local"},
        headers=_NO_STORE_HEADERS,
    )
    response.delete_cookie(sessions.cookie_name, path=sessions.cookie_path)
    return response


def _logout_error() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "AUTHENTICATION_REQUIRED"}},
        status_code=401,
        headers=_NO_STORE_HEADERS,
    )


def _request_entry(
    request: Request,
    runtime: ProductRuntime,
    settings: SsoSettings,
) -> SsoEntry:
    peer = request.client.host if request.client else ""
    host = request.headers.get("Host", "")
    if peer in runtime.settings.trusted_proxies:
        forwarded_host = request.headers.get("X-Forwarded-Host", "")
        if forwarded_host:
            host = forwarded_host
    if (
        not host
        or "," in host
        or "/" in host
        or "\\" in host
        or any(character in host for character in "\r\n")
    ):
        raise ValueError("请求 Host 无效。")
    scheme = effective_request_scheme(
        request, trusted_proxies=runtime.settings.trusted_proxies
    )
    return settings.entry_for_origin(f"{scheme}://{host}")


def _safe_return_to(
    value: str | None,
    *,
    entry: SsoEntry,
    base_path: str,
) -> str:
    if value is None or not value:
        return f"{base_path}/"
    if any(character in value for character in "\\\r\n"):
        raise ValueError("return_to 含不安全字符。")
    parsed = urlsplit(value)
    if parsed.fragment:
        raise ValueError("return_to 不能包含 fragment。")
    if parsed.scheme or parsed.netloc:
        if f"{parsed.scheme}://{parsed.netloc}" != entry.origin:
            raise ValueError("return_to 必须与当前入口同源。")
        relative = parsed.path
        if parsed.query:
            relative = f"{relative}?{parsed.query}"
    else:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("return_to 必须是 KB 内相对路径。")
        relative = value
    decoded_path = unquote(urlsplit(relative).path)
    segments = decoded_path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("return_to 不能包含路径跳转。")
    if decoded_path != base_path and not decoded_path.startswith(
        f"{base_path}/"
    ):
        raise ValueError("return_to 必须位于 KB 前缀内。")
    if decoded_path.startswith(f"{base_path}/sso"):
        raise ValueError("return_to 不能指向 SSO 端点。")
    return relative


def _single_query_value(request: Request, key: str) -> str | None:
    values = request.query_params.getlist(key)
    if len(values) != 1 or not values[0]:
        return None
    return values[0]


def _delete_pending(
    response: StarletteResponse, sessions: SsoSessionService
) -> None:
    response.delete_cookie(
        sessions.pending_cookie_name, path=sessions.pending_cookie_path
    )


def _error_page(code: str, message: str, *, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>湾事通登录</title>"
        "<h1>暂时无法完成登录</h1>"
        f"<p>{escape(message)}</p>"
        f"<p>错误代码：{escape(code)}</p>",
        status_code=status_code,
        headers=_NO_STORE_HEADERS,
    )


__all__ = [
    "SSO_CALLBACK_PATH",
    "SSO_ENTRY_PATH",
    "SSO_LOGOUT_PATH",
    "register_sso_routes",
]
