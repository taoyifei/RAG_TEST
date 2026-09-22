"""RDMS `/sso/validate` 的单次、无重试服务端客户端。"""

from __future__ import annotations

import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import httpx

from rag_app.wanshitong.sso_session import SsoIdentity

_TICKET_HEX_CHARS = 32
_MAX_SECRET_CHARS = 4096
_MAX_ID_CHARS = 128
_MAX_DISPLAY_CHARS = 256
_MAX_ROLES = 64
_MAX_PERMISSIONS = 256
_HTTP_OK = 200
_HTTP_BAD_REQUEST = 400


class SsoValidationError(RuntimeError):
    """不携带 client secret、ticket 或 IdP 原始响应的安全错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class SsoClient:
    """按 v1.3 合同同步换票，任何失败都由新登录流恢复。"""

    def __init__(
        self,
        *,
        validate_url: str,
        client_id: str,
        client_secret: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """保存固定后端地址与只在进程内存在的 client 凭据。

        Args:
            validate_url: KB 主机可达的完整 validate URL。
            client_id: RDMS 已登记的 SP 标识。
            client_secret: 由受限文件读取的明文 Secret。
            transport: 仅供离线合同测试注入的 HTTP Transport。

        """
        if not client_secret or len(client_secret) > _MAX_SECRET_CHARS:
            raise ValueError("SSO client secret 长度无效。")
        self._validate_url = validate_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport

    def __repr__(self) -> str:
        """返回不含 client secret 的诊断表示。"""
        return (
            "SsoClient(validate_url="
            f"{self._validate_url!r}, client_id={self._client_id!r})"
        )

    def validate(
        self,
        *,
        ticket: str,
        service: str,
        state: str,
    ) -> SsoIdentity:
        """立即调用一次 validate 并核对完整响应。

        Args:
            ticket: callback 解析一次后得到的原 32 位 hex 票据。
            service: entry 时冻结的精确 callback URL。
            state: KB pending Cookie 中冻结的随机 state。

        Returns:
            可写入本地加密会话的受信任用户 claims。

        Raises:
            SsoValidationError: 网络、HTTP 状态或响应合同不符合要求。

        """
        if not _valid_ticket(ticket):
            raise SsoValidationError(
                "AUTH_CALLBACK_INVALID",
                "登录回调参数无效，请重新登录。",
                status_code=400,
            )
        timeout = httpx.Timeout(timeout=5.0, connect=2.0)
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.post(
                    self._validate_url,
                    json={
                        "ticket": ticket,
                        "service": service,
                        "clientId": self._client_id,
                        "clientSecret": self._client_secret,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.TimeoutException as error:
            raise SsoValidationError(
                "AUTH_SERVICE_TIMEOUT",
                "认证服务响应超时，请稍后重新登录。",
                status_code=503,
            ) from error
        except httpx.RequestError as error:
            raise SsoValidationError(
                "AUTH_SERVICE_UNAVAILABLE",
                "认证服务暂不可用，请稍后重新登录。",
                status_code=503,
            ) from error

        if response.status_code != _HTTP_OK:
            raise _http_error(response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise _invalid_response() from error
        if not isinstance(payload, dict) or payload.get("code") != _HTTP_OK:
            raise _invalid_response()
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _invalid_response()
        typed_data = cast(Mapping[str, object], data)
        if typed_data.get("service") != service:
            raise _invalid_response()
        if typed_data.get("state") != state:
            raise _invalid_response()
        return SsoIdentity(
            user_id=_identifier(typed_data, "userId"),
            username=_optional_text(typed_data, "username"),
            nick_name=_optional_text(typed_data, "nickName"),
            dept_id=_optional_identifier(typed_data, "deptId"),
            roles=_string_claims(typed_data, "roles", maximum=_MAX_ROLES),
            permissions=_string_claims(
                typed_data, "permissions", maximum=_MAX_PERMISSIONS
            ),
        )


def load_client_secret(path: Path) -> str:
    """从 0600、非 symlink 普通文件读取 SSO client secret。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("SSO client secret 必须是现有非 symlink 普通文件。")
    if stat.S_IMODE(path.stat().st_mode) != stat.S_IRUSR | stat.S_IWUSR:
        raise ValueError("SSO client secret 文件权限必须严格为 0600。")
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value or len(value) > _MAX_SECRET_CHARS or "\n" in value:
        raise ValueError("SSO client secret 文件内容无效。")
    return value


def _valid_ticket(ticket: str) -> bool:
    return len(ticket) == _TICKET_HEX_CHARS and all(
        character in "0123456789abcdef" for character in ticket
    )


def _identifier(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _invalid_response()
    normalized = str(value).strip()
    if not normalized or len(normalized) > _MAX_ID_CHARS:
        raise _invalid_response()
    return normalized


def _optional_identifier(
    payload: Mapping[str, object], key: str
) -> str | None:
    if payload.get(key) is None:
        return None
    return _identifier(payload, key)


def _optional_text(
    payload: Mapping[str, object], key: str
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid_response()
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > _MAX_DISPLAY_CHARS:
        raise _invalid_response()
    return normalized


def _string_claims(
    payload: Mapping[str, object],
    key: str,
    *,
    maximum: int,
) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _invalid_response()
    if len(value) > maximum:
        raise _invalid_response()
    claims: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > _MAX_DISPLAY_CHARS
        ):
            raise _invalid_response()
        claims.append(item)
    return tuple(claims)


def _http_error(status_code: int) -> SsoValidationError:
    if status_code == _HTTP_BAD_REQUEST:
        return SsoValidationError(
            "AUTH_REQUEST_INVALID",
            "当前配置无法完成登录，请联系管理员。",
            status_code=status_code,
        )
    if status_code in {401, 403, 429}:
        return SsoValidationError(
            "AUTH_LOGIN_EXPIRED",
            "登录已失效，请重新登录。",
            status_code=status_code,
        )
    return SsoValidationError(
        "AUTH_SERVICE_UNAVAILABLE",
        "认证服务暂不可用，请稍后重新登录。",
        status_code=503,
    )


def _invalid_response() -> SsoValidationError:
    return SsoValidationError(
        "AUTH_RESPONSE_INVALID",
        "认证响应无效，请联系管理员。",
        status_code=502,
    )


__all__ = [
    "SsoClient",
    "SsoValidationError",
    "load_client_secret",
]
