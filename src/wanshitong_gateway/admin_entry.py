"""把当前管理员的既有SSO会话一次性交接到固定内网后台。"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from wanshitong_gateway.compat.adapters.stores.sqlite_connection import (
    SqliteConnectionFactory,
)
from wanshitong_gateway.compat.core.errors import PolicyDenied
from wanshitong_gateway.compat.product.http_security import (
    effective_request_scheme,
)
from wanshitong_gateway.compat.wanshitong.public_session import (
    PublicSessionIssue,
)
from wanshitong_gateway.compat.wanshitong.sso_session import SsoSessionService
from wanshitong_gateway.compat.wanshitong.sso_settings import SsoEntry

if TYPE_CHECKING:
    from wanshitong_gateway.auth import GatewayAuth

_HANDOFF_TTL_SECONDS = 60
_TICKET_BYTES = 32
_TICKET_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


class _AdminHandoffs:
    """只落原AES-GCM加密Cookie及票据摘要，事务消费不依赖进程内状态。"""

    def __init__(self, auth: GatewayAuth) -> None:
        self._connections = SqliteConnectionFactory(auth.settings.database_path)
        self._deployment_id = auth.settings.deployment_id
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS gateway_admin_handoffs ("
                "ticket_hash TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, "
                "target_origin TEXT NOT NULL, cookie_value TEXT NOT NULL, "
                "expires_at INTEGER NOT NULL)"
            )

    def issue(
        self, *, cookie_value: str, target_origin: str, expires_at: int
    ) -> str:
        """签发单次短票据，过期记录在同一次写事务内清理。"""
        ticket = secrets.token_urlsafe(_TICKET_BYTES)
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM gateway_admin_handoffs WHERE expires_at<=?",
                (int(time.time()),),
            )
            connection.execute(
                "INSERT INTO gateway_admin_handoffs "
                "(ticket_hash,deployment_id,target_origin,"
                "cookie_value,expires_at) "
                "VALUES (?,?,?,?,?)",
                (
                    hashlib.sha256(ticket.encode()).hexdigest(),
                    self._deployment_id,
                    target_origin,
                    cookie_value,
                    expires_at,
                ),
            )
        return ticket

    def consume(self, ticket: str, *, target_origin: str) -> str | None:
        """原子删除当前部署和目标的票据，过期或重放不返回会话密文。"""
        ticket_hash = hashlib.sha256(ticket.encode()).hexdigest()
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT cookie_value,expires_at FROM gateway_admin_handoffs "
                "WHERE ticket_hash=? AND deployment_id=? AND target_origin=?",
                (ticket_hash, self._deployment_id, target_origin),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM gateway_admin_handoffs WHERE ticket_hash=?",
                (ticket_hash,),
            )
            if row["expires_at"] <= int(time.time()):
                return None
            return str(row["cookie_value"])


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status, detail=detail, headers=_PRIVATE_HEADERS
    )


def _request_entry(request: Request, auth: GatewayAuth) -> SsoEntry:
    """沿用SSO的可信代理规则，不以任意Host或转发头选择后台目标。"""
    peer = request.client.host if request.client else ""
    host = request.headers.get("Host", "")
    if peer in auth.settings.trusted_proxies:
        forwarded_host = request.headers.get("X-Forwarded-Host", "")
        if forwarded_host:
            host = forwarded_host
    if not host or any(character in host for character in ",/\\\r\n"):
        raise _error(403, "当前入口未登记，请从湾小度页面进入管理后台。")
    scheme = effective_request_scheme(
        request, trusted_proxies=auth.settings.trusted_proxies
    )
    try:
        return auth.settings.sso.entry_for_origin(f"{scheme}://{host}")
    except ValueError:
        raise _error(
            403, "当前入口未登记，请从湾小度页面进入管理后台。"
        ) from None


def _admin_issue(
    auth: GatewayAuth, sessions: SsoSessionService, cookie_value: str | None
) -> PublicSessionIssue:
    """恢复原有会话并实时检查角色，不重新签发或延长员工会话。"""
    try:
        issue = sessions.bootstrap(cookie_value)
    except PolicyDenied:
        raise _error(401, "登录状态已失效，请重新登录湾小度。") from None
    if auth.profile_for_principal(issue.principal).role != "admin":
        raise _error(403, "仅管理员可以进入管理后台。")
    return issue


def _completed_redirect(
    auth: GatewayAuth,
    sessions: SsoSessionService,
    target: SsoEntry,
    issue: PublicSessionIssue,
) -> Response:
    """写回同一Cookie及剩余TTL，并清除会遮盖员工角色的旧控制台Cookie。"""
    response = RedirectResponse(
        target.origin + auth.settings.root_path + "/admin/overview",
        status_code=303,
        headers=_PRIVATE_HEADERS,
    )
    response.delete_cookie(
        auth.settings.admin_cookie_name, path=auth.settings.root_path
    )
    response.delete_cookie(
        sessions.cookie_name, path=sessions.legacy_cookie_path
    )
    response.set_cookie(
        sessions.cookie_name,
        issue.cookie_value,
        httponly=True,
        secure=target.origin.startswith("https://"),
        samesite=sessions.cookie_samesite,
        max_age=issue.expires_in,
        path=sessions.cookie_path,
    )
    return response


def register_admin_entry_routes(app: FastAPI, auth: GatewayAuth) -> None:
    """注册管理员内网导航及60秒单次交接，不接受控制台独立身份代替员工。"""
    if not auth.settings.sso.enabled:
        return
    sessions = auth.public_sessions
    if not isinstance(sessions, SsoSessionService):
        raise AssertionError("SSO 会话类型不匹配。")
    entries = auth.settings.sso.entries
    target = next(
        (entry for entry in entries if entry.entry_id == "internal"), None
    )
    if target is None:
        if len(entries) != 1:
            raise ValueError("管理后台需要已登记internal入口或唯一SSO入口。")
        target = entries[0]
    handoffs = _AdminHandoffs(auth)

    @app.get("/api/admin/entry")
    def admin_entry(request: Request) -> Response:
        """只从当前真实员工身份签票，当前已在内网时直接进入概览。"""
        entry = _request_entry(request, auth)
        if request.query_params:
            raise _error(400, "管理后台入口不接受额外跳转参数。")
        try:
            principal = auth.require_public(request, csrf=False)
        except HTTPException:
            raise _error(401, "请先登录湾小度再进入管理后台。") from None
        if auth.profile_for_principal(principal).role != "admin":
            raise _error(403, "仅管理员可以进入管理后台。")
        cookie = request.cookies.get(sessions.cookie_name)
        issue = _admin_issue(auth, sessions, cookie)
        if entry.origin == target.origin:
            return _completed_redirect(auth, sessions, target, issue)
        now = int(time.time())
        expires_at = min(now + _HANDOFF_TTL_SECONDS, principal.expires_at)
        if expires_at <= now:
            raise _error(401, "登录状态已失效，请重新登录湾小度。")
        ticket = handoffs.issue(
            cookie_value=issue.cookie_value,
            target_origin=target.origin,
            expires_at=expires_at,
        )
        return RedirectResponse(
            target.origin
            + auth.settings.root_path
            + "/api/admin/entry/complete?ticket="
            + ticket,
            status_code=302,
            headers=_PRIVATE_HEADERS,
        )

    @app.get("/api/admin/entry/complete")
    def admin_entry_complete(request: Request) -> Response:
        """固定内网单次消费票据，原会话或最新管理员角色失效则拒绝进入。"""
        entry = _request_entry(request, auth)
        if entry.origin != target.origin:
            raise _error(403, "请通过指定内网入口完成管理后台跳转。")
        parameters = request.query_params.multi_items()
        if (
            len(parameters) != 1
            or parameters[0][0] != "ticket"
            or _TICKET_PATTERN.fullmatch(parameters[0][1]) is None
        ):
            raise _error(400, "管理后台跳转链接无效，请重新从湾小度进入。")
        cookie_value = handoffs.consume(
            parameters[0][1], target_origin=target.origin
        )
        if cookie_value is None:
            raise _error(
                401, "后台跳转链接已过期或已使用，请重新从湾小度进入。"
            )
        issue = _admin_issue(auth, sessions, cookie_value)
        return _completed_redirect(auth, sessions, target, issue)


__all__ = ["register_admin_entry_routes"]
