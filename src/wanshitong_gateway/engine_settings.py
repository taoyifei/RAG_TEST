"""独立网关的原生服务地址与公共发布范围。"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_MAX_KB_ID_LENGTH = 128
_MAX_EMAIL_LENGTH = 254


@dataclass(frozen=True, slots=True)
class EngineSettings:
    """只存网关必须知道的原生租户及已发布 KB ID。"""

    base_url: str
    tenant_id: int
    public_kb_ids: tuple[str, ...]
    api_key_file: Path
    external_signing_key_file: Path
    admin_email: str
    admin_password_file: Path

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> EngineSettings:
        """从候选专用环境读取原生连接设置，缺项立即失败。"""
        source = os.environ if environment is None else environment
        base_url = source.get("WEKNORA_BASE_URL", "").strip().rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("WEKNORA_BASE_URL 必须是无路径的固定服务地址。")
        try:
            tenant_id = int(source.get("WEKNORA_TENANT_ID", ""))
        except ValueError as error:
            raise ValueError("WEKNORA_TENANT_ID 必须为正整数。") from error
        if tenant_id <= 0:
            raise ValueError("WEKNORA_TENANT_ID 必须为正整数。")
        kb_ids = tuple(
            dict.fromkeys(
                item.strip()
                for item in source.get("WEKNORA_PUBLIC_KB_IDS", "").split(",")
                if item.strip()
            )
        )
        if not kb_ids or any(
            "/" in item or "\\" in item or len(item) > _MAX_KB_ID_LENGTH
            for item in kb_ids
        ):
            raise ValueError("WEKNORA_PUBLIC_KB_IDS 必须是非空 ID 列表。")
        api_key_file = _absolute_file(
            source.get("WEKNORA_API_KEY_FILE", ""),
            "WEKNORA_API_KEY_FILE",
        )
        signing_key_file = _absolute_file(
            source.get("WEKNORA_EXTERNAL_SIGNING_KEY_FILE", ""),
            "WEKNORA_EXTERNAL_SIGNING_KEY_FILE",
        )
        admin_email = source.get("WEKNORA_ADMIN_EMAIL", "").strip()
        if (
            not admin_email
            or "@" not in admin_email
            or len(admin_email) > _MAX_EMAIL_LENGTH
        ):
            raise ValueError("WEKNORA_ADMIN_EMAIL 必须是候选专属管理员邮箱。")
        admin_password_file = _absolute_file(
            source.get("WEKNORA_ADMIN_PASSWORD_FILE", ""),
            "WEKNORA_ADMIN_PASSWORD_FILE",
        )
        return cls(
            base_url=base_url,
            tenant_id=tenant_id,
            public_kb_ids=kb_ids,
            api_key_file=api_key_file,
            external_signing_key_file=signing_key_file,
            admin_email=admin_email,
            admin_password_file=admin_password_file,
        )


def _absolute_file(raw: str, name: str) -> Path:
    path = Path(raw.strip())
    if not raw.strip() or not path.is_absolute():
        raise ValueError(f"{name} 必须是显式绝对路径。")
    return path


def load_server_secret(path: Path) -> str:
    """读取候选部署私有 Secret 文件，禁止符号链接和宽权限。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Secret 必须是非 symlink 普通文件。")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("Secret 文件不得授予组或其他用户权限。")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("Secret 文件不能为空。")
    return value
