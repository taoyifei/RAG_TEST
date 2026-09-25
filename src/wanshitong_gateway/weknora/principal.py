"""为已验证湾事通用户签发 WeKnora 外部主体。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable

_TOKEN_TTL_SECONDS = 120
_MAX_SUBJECT_BYTES = 128
_CONTROL_CHARACTER_LIMIT = 0x20
_DELETE_CHARACTER = 0x7F


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class ExternalPrincipalSigner:
    """签发仅供原生 API 校验的短期 HS256 JWT。"""

    def __init__(
        self,
        *,
        secret: str,
        tenant_id: int,
        deployment_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secret.strip() or tenant_id <= 0 or not deployment_id:
            raise ValueError("外部主体签名配置无效。")
        self._secret = secret.strip().encode()
        self._tenant_id = tenant_id
        self._deployment_id = deployment_id
        self._clock = clock

    def subject(self, user_id: str) -> str:
        """构造跨部署稳定且不使用显示姓名的主体 ID。"""
        subject = f"rdms:{self._deployment_id}:{user_id.strip()}"
        if (
            not user_id.strip()
            or len(subject.encode()) > _MAX_SUBJECT_BYTES
            or any(
                ord(character) < _CONTROL_CHARACTER_LIMIT
                or ord(character) == _DELETE_CHARACTER
                for character in subject
            )
        ):
            raise ValueError("外部主体 ID 无效。")
        return subject

    def sign(self, user_id: str) -> str:
        """签发固定 audience、workspace 和短有效期的用户 Token。"""
        now = int(self._clock())
        header = _base64url(b'{"alg":"HS256","typ":"JWT"}')
        claims = _base64url(
            json.dumps(
                {
                    "aud": "weknora",
                    "exp": now + _TOKEN_TTL_SECONDS,
                    "iat": now,
                    "sub": self.subject(user_id),
                    "tenant_id": self._tenant_id,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = _base64url(
            hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        )
        return f"{header}.{claims}.{signature}"
