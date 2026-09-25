"""独立湾事通网关的身份配置与数据目录。"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from rag_app.wanshitong.sso_settings import SsoSettings

_DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True, slots=True)
class GatewayAuthSettings:
    """网关认证所需的部署隔离配置。

    `database_path` 由网关统一数据目录派生；认证只创建自己的会话表，
    其余映射与 Trace 表由各自 Store 初始化。
    """

    data_dir: Path
    master_key_file: Path
    bootstrap_token_file: Path
    deployment_id: str
    trusted_origins: tuple[str, ...]
    trusted_proxies: frozenset[str]
    sso: SsoSettings
    root_path: str = "/kb"

    @property
    def database_path(self) -> Path:
        """返回仅供本候选网关使用的 SQLite 路径。"""
        return self.data_dir / "wanshitong-gateway.sqlite3"

    @property
    def admin_cookie_name(self) -> str:
        """按部署 ID 隔离管理员 Cookie，避免同主机端口互相覆盖。"""
        return f"wst_gateway_admin_{self.deployment_id}"

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> GatewayAuthSettings:
        """读取显式候选目录与已有湾事通认证配置。

        Args:
            environment: 可选环境映射；默认读取当前进程环境。

        Returns:
            经校验的独立网关认证设置。

        Raises:
            ValueError: 缺少部署隔离、可信 Origin 或 Secret 路径。

        """
        source = os.environ if environment is None else environment
        root_path = source.get("RAG_ROOT_PATH", "/kb").strip()
        if root_path != "/kb":
            raise ValueError("独立湾事通网关要求 RAG_ROOT_PATH 精确为 /kb。")
        data_dir = _required_absolute_path(
            source, "WANSHITONG_GATEWAY_DATA_DIR"
        )
        master_key_file = _required_absolute_path(source, "RAG_MASTER_KEY_FILE")
        bootstrap_token_file = _required_absolute_path(
            source, "RAG_ADMIN_BOOTSTRAP_TOKEN_FILE"
        )
        deployment_id = source.get(
            "WANSHITONG_GATEWAY_DEPLOYMENT_ID",
            source.get("RAG_WANSHITONG_SSO_DEPLOYMENT_ID", ""),
        ).strip()
        if not _DEPLOYMENT_ID_PATTERN.fullmatch(deployment_id):
            raise ValueError("WANSHITONG_GATEWAY_DEPLOYMENT_ID 格式无效。")
        sso = SsoSettings.from_environment(source, root_path=root_path)
        if sso.enabled and sso.deployment_id != deployment_id:
            raise ValueError("网关与 SSO deployment ID 必须一致。")
        trusted_origins = _trusted_origins(
            source.get("RAG_TRUSTED_ORIGINS", "")
        )
        if sso.enabled and any(
            entry.origin not in trusted_origins for entry in sso.entries
        ):
            raise ValueError("SSO 入口必须列入 RAG_TRUSTED_ORIGINS。")
        trusted_proxies = _trusted_proxies(
            source.get("RAG_TRUSTED_PROXIES", "")
        )
        return cls(
            data_dir=data_dir,
            master_key_file=master_key_file,
            bootstrap_token_file=bootstrap_token_file,
            deployment_id=deployment_id,
            trusted_origins=trusted_origins,
            trusted_proxies=trusted_proxies,
            sso=sso,
            root_path=root_path,
        )


def _required_absolute_path(source: Mapping[str, str], key: str) -> Path:
    value = source.get(key, "").strip()
    path = Path(value)
    if not value or not path.is_absolute():
        raise ValueError(f"{key} 必须是显式绝对路径。")
    if path.is_symlink():
        raise ValueError(f"{key} 禁止 symlink。")
    return path


def _trusted_origins(raw: str) -> tuple[str, ...]:
    origins: list[str] = []
    for item in (part.strip().rstrip("/") for part in raw.split(",")):
        if not item:
            continue
        parsed = urlsplit(item)
        try:
            _port = parsed.port
        except ValueError:
            raise ValueError("RAG_TRUSTED_ORIGINS 端口无效。") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname == "*"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("RAG_TRUSTED_ORIGINS 包含不安全 Origin。")
        origins.append(item)
    if not origins:
        raise ValueError("RAG_TRUSTED_ORIGINS 至少需要一个完整 Origin。")
    return tuple(dict.fromkeys(origins))


def _trusted_proxies(raw: str) -> frozenset[str]:
    proxies: set[str] = set()
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        try:
            proxies.add(str(ipaddress.ip_address(item)))
        except ValueError:
            raise ValueError("RAG_TRUSTED_PROXIES 只接受明确 IP。") from None
    return frozenset(proxies)


__all__ = ["GatewayAuthSettings"]
