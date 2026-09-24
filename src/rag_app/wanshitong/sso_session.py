"""湾事通 SSO pending 与无表用户会话。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from cryptography.exceptions import InvalidTag

from rag_app.core.errors import PolicyDenied
from rag_app.core.identifiers import canonical_json
from rag_app.product.crypto import MasterKey, SecretAad, SecretCipher
from rag_app.wanshitong.public_session import (
    PublicSessionIssue,
    PublicSessionPrincipal,
)

_COOKIE_VERSION = "v1"
_PENDING_TTL_SECONDS = 300
_SESSION_TTL_SECONDS = 2 * 60 * 60
_MAX_PENDING_COOKIE_CHARS = 2048
_MAX_SESSION_COOKIE_CHARS = 3500
_COOKIE_PARTS = 3
_SESSION_ID_PREFIX = "wstsid_"
_CSRF_DOMAIN = b"wanshitong/sso-session/csrf/v1"
_AAD_PROVIDER = "wanshitong-sso"


@dataclass(frozen=True, slots=True)
class SsoPending:
    """一次浏览器登录流程的服务端权威状态。"""

    state: str
    service: str
    return_to: str
    origin_id: str
    issued_at: int
    expires_at: int


@dataclass(frozen=True, slots=True, repr=False)
class SsoPendingIssue:
    """待写入短时 Cookie 的 pending。"""

    pending: SsoPending
    cookie_value: str


@dataclass(frozen=True, slots=True)
class SsoIdentity:
    """IdP validate 后已核对 service/state 的用户 claims。"""

    user_id: str
    username: str | None
    nick_name: str | None
    dept_id: str | None
    roles: tuple[str, ...]
    permissions: tuple[str, ...]


class SsoSessionService:
    """复用部署主密钥加密 pending 与两小时本地用户会话。"""

    def __init__(
        self,
        master_key: MasterKey,
        *,
        deployment_id: str,
        base_path: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """创建部署隔离的 Cookie 编解码器。

        Args:
            master_key: 已通过权限检查的 Product 部署主密钥。
            deployment_id: 候选或正式环境内稳定的非 Secret 标识。
            base_path: 已登记 SSO 回调使用的固定前缀。
            clock: 测试可替换的 Unix 时间函数。

        """
        self._cipher = SecretCipher(master_key)
        self._deployment_id = deployment_id
        self._base_path = base_path
        self._clock = clock
        self._csrf_key = hmac.new(
            master_key.value,
            _CSRF_DOMAIN + b"\x00" + deployment_id.encode("ascii"),
            hashlib.sha256,
        ).digest()

    @property
    def cookie_name(self) -> str:
        """返回不与同 host 其它部署冲突的用户 Cookie 名。"""
        return f"kb_user_session_{self._deployment_id}"

    @property
    def pending_cookie_name(self) -> str:
        """返回不会与 RDMS `sso_pending` 冲突的 Cookie 名。"""
        return f"kb_sso_pending_{self._deployment_id}"

    @property
    def cookie_path(self) -> str:
        """用户会话同时覆盖公共根入口与原有 KB 前缀。"""
        return "/"

    @property
    def legacy_cookie_path(self) -> str:
        """旧版用户 Cookie 的路径，用于切换和退出时清除。"""
        return self._base_path

    @property
    def pending_cookie_path(self) -> str:
        """Pending 仅发送给 KB 的 SSO 端点。"""
        return f"{self._base_path}/sso"

    @property
    def cookie_samesite(self) -> Literal["lax"]:
        """允许 IdP 顶层导航回 callback 时携带 Cookie。"""
        return "lax"

    @property
    def set_cookie_on_bootstrap(self) -> bool:
        """固定 TTL 会话不在普通 bootstrap 时滑动续期。"""
        return False

    @property
    def deployment_id(self) -> str:
        """返回多标签页通知使用的稳定命名空间。"""
        return self._deployment_id

    def issue_pending(
        self,
        *,
        service: str,
        return_to: str,
        origin_id: str,
    ) -> SsoPendingIssue:
        """生成 32 字节随机 state 与五分钟加密 pending。"""
        now = int(self._clock())
        pending = SsoPending(
            state=secrets.token_hex(32),
            service=service,
            return_to=return_to,
            origin_id=origin_id,
            issued_at=now,
            expires_at=now + _PENDING_TTL_SECONDS,
        )
        cookie = self._encode(
            "pending",
            {
                "exp": pending.expires_at,
                "iat": pending.issued_at,
                "origin_id": pending.origin_id,
                "return_to": pending.return_to,
                "service": pending.service,
                "state": pending.state,
            },
        )
        if len(cookie) > _MAX_PENDING_COOKIE_CHARS:
            raise ValueError("SSO pending Cookie 超过安全上限。")
        return SsoPendingIssue(pending=pending, cookie_value=cookie)

    def read_pending(self, cookie_value: str) -> SsoPending:
        """解密并验证 pending 的字段与有效期。"""
        payload = self._decode("pending", cookie_value)
        pending = SsoPending(
            state=_required_text(payload, "state"),
            service=_required_text(payload, "service"),
            return_to=_required_text(payload, "return_to"),
            origin_id=_required_text(payload, "origin_id"),
            issued_at=_required_integer(payload, "iat"),
            expires_at=_required_integer(payload, "exp"),
        )
        if pending.expires_at <= int(self._clock()):
            raise PolicyDenied(
                "登录流程已过期，请重新进入。",
                stage="wanshitong.sso.pending",
            )
        if pending.issued_at > pending.expires_at:
            raise _invalid_cookie("pending")
        return pending

    def issue_user(self, identity: SsoIdentity) -> PublicSessionIssue:
        """只把公共会话需要的身份与显示名加密进两小时 Cookie。"""
        now = int(self._clock())
        # RDMS 的角色、权限和部门声明不参与公共会话授权，不写入浏览器 Cookie。
        principal = PublicSessionPrincipal(
            session_id=f"{_SESSION_ID_PREFIX}{secrets.token_hex(16)}",
            owner_id=f"rdms:{identity.user_id}",
            expires_at=now + _SESSION_TTL_SECONDS,
            user_id=identity.user_id,
            username=identity.username if identity.nick_name is None else None,
            nick_name=identity.nick_name,
        )
        payload: dict[str, object] = {
            "aud": self._deployment_id,
            "dept_id": None,
            "exp": principal.expires_at,
            "iat": now,
            "iss": "rdms",
            "nick_name": principal.nick_name,
            "permissions": [],
            "roles": [],
            "sid": principal.session_id,
            "user_id": principal.user_id,
            "username": principal.username,
            "v": 1,
        }
        cookie = self._encode("user", payload)
        if len(cookie) > _MAX_SESSION_COOKIE_CHARS:
            # 极端显示名仍可能超过浏览器限制；保留用户 ID 完成登录。
            principal = replace(principal, username=None, nick_name=None)
            payload["nick_name"] = None
            payload["username"] = None
            cookie = self._encode("user", payload)
        if len(cookie) > _MAX_SESSION_COOKIE_CHARS:
            raise ValueError("SSO 用户 claims 超过 3500 bytes Cookie 上限。")
        return self._build_issue(principal, cookie)

    def bootstrap(self, cookie_value: str | None) -> PublicSessionIssue:
        """SSO 模式只恢复已有用户，不允许匿名降级。"""
        if cookie_value is None:
            raise PolicyDenied(
                "需要登录后访问湾事通。", stage="wanshitong.sso.session"
            )
        principal = self.validate_cookie(cookie_value)
        return self._build_issue(principal, cookie_value)

    def authenticate(
        self, cookie_value: str, csrf_token: str
    ) -> PublicSessionPrincipal:
        """同时验证用户 Cookie、固定 TTL 与 CSRF。"""
        issue = self.bootstrap(cookie_value)
        if not hmac.compare_digest(csrf_token, issue.csrf_token):
            raise PolicyDenied(
                "用户会话验证失败。", stage="wanshitong.sso.csrf"
            )
        return issue.principal

    def validate_cookie(self, cookie_value: str) -> PublicSessionPrincipal:
        """在 GET 与流发布边界复核用户 Cookie。"""
        if len(cookie_value) > _MAX_SESSION_COOKIE_CHARS:
            raise _invalid_cookie("user")
        payload = self._decode("user", cookie_value)
        if payload.get("v") != 1:
            raise _invalid_cookie("user")
        if payload.get("iss") != "rdms":
            raise _invalid_cookie("user")
        if payload.get("aud") != self._deployment_id:
            raise _invalid_cookie("user")
        principal = PublicSessionPrincipal(
            session_id=_required_text(payload, "sid"),
            owner_id=f"rdms:{_required_text(payload, 'user_id')}",
            expires_at=_required_integer(payload, "exp"),
            user_id=_required_text(payload, "user_id"),
            username=_optional_text(payload, "username"),
            nick_name=_optional_text(payload, "nick_name"),
            dept_id=_optional_text(payload, "dept_id"),
            roles=_string_tuple(payload, "roles"),
            permissions=_string_tuple(payload, "permissions"),
        )
        if principal.expires_at <= int(self._clock()):
            raise PolicyDenied(
                "用户会话已过期。", stage="wanshitong.sso.session"
            )
        return principal

    def _build_issue(
        self,
        principal: PublicSessionPrincipal,
        cookie_value: str,
    ) -> PublicSessionIssue:
        csrf = hmac.new(
            self._csrf_key,
            f"{principal.session_id}.{principal.expires_at}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return PublicSessionIssue(
            principal=principal,
            cookie_value=cookie_value,
            csrf_token=csrf,
            expires_in=max(1, principal.expires_at - int(self._clock())),
        )

    def _encode(self, kind: str, payload: Mapping[str, object]) -> str:
        ciphertext, nonce = self._cipher.encrypt(
            canonical_json(dict(payload)), aad=self._aad(kind)
        )
        return f"{_COOKIE_VERSION}.{nonce}.{ciphertext}"

    def _decode(self, kind: str, cookie_value: str) -> dict[str, object]:
        parts = cookie_value.split(".")
        if len(parts) != _COOKIE_PARTS or parts[0] != _COOKIE_VERSION:
            raise _invalid_cookie(kind)
        try:
            _require_canonical_base64(parts[1])
            _require_canonical_base64(parts[2])
            plaintext = self._cipher.decrypt(
                parts[2], parts[1], aad=self._aad(kind)
            )
            payload = json.loads(plaintext)
        except (InvalidTag, ValueError, binascii.Error, json.JSONDecodeError):
            raise _invalid_cookie(kind) from None
        if not isinstance(payload, dict):
            raise _invalid_cookie(kind)
        return cast(dict[str, object], payload)

    def _aad(self, kind: str) -> SecretAad:
        return SecretAad(
            credential_id=f"wanshitong-sso:{self._deployment_id}",
            provider_type=_AAD_PROVIDER,
            field_name=kind,
            key_version=1,
        )


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid_cookie("claims")
    return value


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid_cookie("claims")
    return value


def _required_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid_cookie("claims")
    return value


def _string_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _invalid_cookie("claims")
    if not all(isinstance(item, str) and item for item in value):
        raise _invalid_cookie("claims")
    return tuple(cast(Sequence[str], value))


def _invalid_cookie(kind: str) -> PolicyDenied:
    return PolicyDenied("SSO 会话无效。", stage=f"wanshitong.sso.{kind}")


def _require_canonical_base64(value: str) -> None:
    decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(decoded).decode("ascii") != value:
        raise ValueError("非规范 Base64url。")


__all__ = [
    "SsoIdentity",
    "SsoPending",
    "SsoPendingIssue",
    "SsoSessionService",
]
