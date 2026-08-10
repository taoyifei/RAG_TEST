"""同源普通问答 UI 的短期签名会话。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

__all__ = [
    "UI_SESSION_COOKIE_NAME",
    "UiSessionGrant",
    "UiSessionManager",
    "require_same_origin",
]

UI_SESSION_COOKIE_NAME = "rag_ui_session"
_DOMAIN = b"rag-ui-session-v1"
_SESSION_VERSION = 1


@dataclass(frozen=True, slots=True)
class UiSessionGrant:
    """一次性返回给页面的签名 Cookie 与 CSRF 凭据。"""

    cookie_value: str
    csrf_token: str
    expires_at: datetime


class UiSessionManager:
    """签发并校验不含 Query Token 的无状态 UI 会话。"""

    def __init__(
        self,
        query_token: str,
        *,
        ttl_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """派生独立签名密钥并保存有界会话时钟。

        Args:
            query_token: 仅在服务端存在的普通查询密钥。
            ttl_seconds: UI 会话固定存活秒数。
            clock: 可选的带时区时钟，供确定性测试注入。

        Raises:
            ValueError: Query Token 为空或 TTL 不在固定边界内。

        """
        if not query_token or not 60 <= ttl_seconds <= 3600:
            raise ValueError("UI session token 或 TTL 无效。")
        self._signing_key = hmac.new(
            query_token.encode("utf-8"),
            _DOMAIN,
            hashlib.sha256,
        ).digest()
        self._ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(self) -> UiSessionGrant:
        """创建一个短期签名会话和仅返回一次的 CSRF token。

        Returns:
            Cookie 值、页面内存 CSRF token 和到期时间。

        """
        now = self._now()
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        csrf_token = secrets.token_urlsafe(32)
        payload = {
            "csrf_sha256": hashlib.sha256(
                csrf_token.encode("utf-8")
            ).hexdigest(),
            "exp": int(expires_at.timestamp()),
            "sid": secrets.token_urlsafe(32),
            "v": _SESSION_VERSION,
        }
        encoded = _encode_payload(payload)
        signature = hmac.new(
            self._signing_key,
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return UiSessionGrant(
            cookie_value=f"{encoded}.{_encode_bytes(signature)}",
            csrf_token=csrf_token,
            expires_at=expires_at,
        )

    def verify(self, cookie_value: str | None, csrf_token: str | None) -> None:
        """校验签名、时限和当前请求的 CSRF token。

        Args:
            cookie_value: HttpOnly Cookie 中的无状态会话值。
            csrf_token: 页面内存通过请求头提交的 CSRF token。

        Raises:
            HTTPException: Cookie 无效返回 401，CSRF 无效返回 403。

        """
        payload = self._verified_payload(cookie_value)
        if csrf_token is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="csrf validation failed",
            )
        supplied = hashlib.sha256(csrf_token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(supplied, payload["csrf_sha256"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="csrf validation failed",
            )

    def _verified_payload(self, cookie_value: str | None) -> dict[str, object]:
        if cookie_value is None:
            _unauthorized_session()
        try:
            encoded, supplied_signature = cookie_value.rsplit(".", maxsplit=1)
            expected_signature = hmac.new(
                self._signing_key,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
            decoded_signature = _decode_bytes(supplied_signature)
            payload = json.loads(_decode_bytes(encoded))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            _unauthorized_session()
        if not hmac.compare_digest(decoded_signature, expected_signature):
            _unauthorized_session()
        if (
            not isinstance(payload, dict)
            or set(payload) != {"csrf_sha256", "exp", "sid", "v"}
            or payload.get("v") != _SESSION_VERSION
            or not isinstance(payload.get("sid"), str)
            or not isinstance(payload.get("csrf_sha256"), str)
            or not isinstance(payload.get("exp"), int)
        ):
            _unauthorized_session()
        csrf_sha256 = payload["csrf_sha256"]
        if (
            len(csrf_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in csrf_sha256
            )
            or payload["exp"] <= int(self._now().timestamp())
        ):
            _unauthorized_session()
        return payload

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UI session clock 必须带时区。")
        return value.astimezone(UTC)


def require_same_origin(request: Request) -> None:
    """要求请求 Origin 与 Host、scheme 精确同源。

    Args:
        request: 当前 Starlette 请求。

    Raises:
        HTTPException: Origin 缺失、畸形或与 Host 不一致。

    """
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if origin is None or host is None:
        _forbidden_origin()
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc != host
        or parsed.scheme != request.url.scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        _forbidden_origin()


def _encode_payload(payload: dict[str, object]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return _encode_bytes(serialized)


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )


def _unauthorized_session() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="ui session invalid",
    )


def _forbidden_origin() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="origin validation failed",
    )
