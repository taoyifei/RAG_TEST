"""湾事通匿名公共会话与域隔离主体派生。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from rag_app.core.errors import PolicyDenied
from rag_app.product.crypto import MasterKey

PUBLIC_SESSION_COOKIE = "wanshitong_public_session"
_COOKIE_VERSION = "v1"
_SESSION_ID_PREFIX = "wstsid_"
_OWNER_ID_PREFIX = "wanshitong-public:"
_DEFAULT_TTL_SECONDS = 24 * 60 * 60
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_COOKIE_CHARS = 512
_COOKIE_PARTS = 4
_SESSION_ID_HEX_CHARS = 32
_SESSION_SIGNING_DOMAIN = b"wanshitong/public-session/signing/v1"
_CSRF_DOMAIN = b"wanshitong/public-session/csrf/v1"
_OWNER_DOMAIN = b"wanshitong/public-session/owner/v1"


@dataclass(frozen=True, slots=True)
class PublicSessionPrincipal:
    """已验证匿名 SID 对应的内部主体。"""

    session_id: str
    owner_id: str
    expires_at: int


@dataclass(frozen=True, slots=True, repr=False)
class PublicSessionIssue:
    """仅向 HTTP 边界提供 Cookie、CSRF 与安全会话摘要。"""

    principal: PublicSessionPrincipal
    cookie_value: str
    csrf_token: str
    expires_in: int


class PublicSessionService:
    """签发无登录 SID，并从主密钥独立派生 Cookie、CSRF 与 owner。"""

    def __init__(
        self,
        master_key: MasterKey,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """派生当前公共会话所需的三个互不复用 HMAC key。

        Args:
            master_key: Product Runtime 使用的部署主密钥。
            ttl_seconds: 匿名会话有效期秒数。
            clock: 测试可替换的 Unix 时间函数。

        Raises:
            ValueError: TTL 不在一分钟到七天之间。

        """
        if not _MIN_TTL_SECONDS <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise ValueError("公共会话 TTL 必须在 60 秒到 7 天之间。")
        self._signing_key = _derive_key(master_key, _SESSION_SIGNING_DOMAIN)
        self._csrf_key = _derive_key(master_key, _CSRF_DOMAIN)
        self._owner_key = _derive_key(master_key, _OWNER_DOMAIN)
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def issue_or_resume(self, cookie_value: str | None) -> PublicSessionIssue:
        """恢复仍有效的匿名 SID，或签发一个新的服务端 SID。

        Args:
            cookie_value: 浏览器可能携带的 HttpOnly Cookie。

        Returns:
            同一 SID 的安全摘要，或全新随机 SID 的签发结果。

        """
        if cookie_value is not None:
            try:
                return self._issue_from_cookie(cookie_value)
            except PolicyDenied:
                # Session bootstrap 是唯一允许用新身份替换无效 Cookie 的入口。
                pass
        return self._issue_new()

    def authenticate(
        self, cookie_value: str, csrf_token: str
    ) -> PublicSessionPrincipal:
        """同时验证 HttpOnly Cookie 和页面内存 CSRF。

        Args:
            cookie_value: 请求携带的签名 SID Cookie。
            csrf_token: 页面内存中的 CSRF 值。

        Returns:
            只在服务端使用的匿名 owner 主体。

        Raises:
            PolicyDenied: Cookie、有效期或 CSRF 不匹配。

        """
        issue = self._issue_from_cookie(cookie_value)
        if not hmac.compare_digest(csrf_token, issue.csrf_token):
            raise PolicyDenied(
                "公共会话验证失败。", stage="wanshitong.public.csrf"
            )
        return issue.principal

    def validate_cookie(self, cookie_value: str) -> PublicSessionPrincipal:
        """在流事件发布边界复核签名 SID 与有效期。

        Args:
            cookie_value: 请求开始时绑定的签名 SID Cookie。

        Returns:
            仍有效的同一匿名主体。

        """
        return self._issue_from_cookie(cookie_value).principal

    def _issue_new(self) -> PublicSessionIssue:
        now = int(self._clock())
        session_id = f"{_SESSION_ID_PREFIX}{secrets.token_hex(16)}"
        expires_at = now + self._ttl_seconds
        unsigned = f"{_COOKIE_VERSION}.{session_id}.{expires_at}"
        signature = _digest(self._signing_key, unsigned)
        return self._build_issue(f"{unsigned}.{signature}")

    def _issue_from_cookie(self, cookie_value: str) -> PublicSessionIssue:
        version, session_id, expires_text, signature = _cookie_parts(
            cookie_value
        )
        if version != _COOKIE_VERSION or not _valid_session_id(session_id):
            raise _invalid_session()
        try:
            expires_at = int(expires_text)
        except ValueError:
            raise _invalid_session() from None
        unsigned = f"{version}.{session_id}.{expires_at}"
        expected = _digest(self._signing_key, unsigned)
        if not hmac.compare_digest(signature, expected):
            raise _invalid_session()
        if expires_at <= int(self._clock()):
            raise PolicyDenied(
                "公共会话已过期。", stage="wanshitong.public.session"
            )
        return self._build_issue(cookie_value)

    def _build_issue(self, cookie_value: str) -> PublicSessionIssue:
        _, session_id, expires_text, _ = _cookie_parts(cookie_value)
        expires_at = int(expires_text)
        principal = PublicSessionPrincipal(
            session_id=session_id,
            owner_id=(
                _OWNER_ID_PREFIX + _digest(self._owner_key, session_id)
            ),
            expires_at=expires_at,
        )
        csrf_token = _digest(
            self._csrf_key, f"{session_id}.{expires_at}"
        )
        return PublicSessionIssue(
            principal=principal,
            cookie_value=cookie_value,
            csrf_token=csrf_token,
            expires_in=max(1, expires_at - int(self._clock())),
        )


def _derive_key(master_key: MasterKey, domain: bytes) -> bytes:
    return hmac.new(master_key.value, domain, hashlib.sha256).digest()


def _digest(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _cookie_parts(cookie_value: str) -> tuple[str, str, str, str]:
    if len(cookie_value) > _MAX_COOKIE_CHARS:
        raise _invalid_session()
    parts = cookie_value.split(".")
    if len(parts) != _COOKIE_PARTS:
        raise _invalid_session()
    return parts[0], parts[1], parts[2], parts[3]


def _valid_session_id(value: str) -> bool:
    suffix = value.removeprefix(_SESSION_ID_PREFIX)
    return (
        value.startswith(_SESSION_ID_PREFIX)
        and len(suffix) == _SESSION_ID_HEX_CHARS
        and all(character in "0123456789abcdef" for character in suffix)
    )


def _invalid_session() -> PolicyDenied:
    return PolicyDenied(
        "公共会话无效。", stage="wanshitong.public.session"
    )


__all__ = [
    "PUBLIC_SESSION_COOKIE",
    "PublicSessionIssue",
    "PublicSessionPrincipal",
    "PublicSessionService",
]
