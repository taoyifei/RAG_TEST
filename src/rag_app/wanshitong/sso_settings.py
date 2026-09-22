"""湾事通 SSO 的有限入口注册表与启动校验。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

_AUTH_MODE_KEY = "RAG_WANSHITONG_AUTH_MODE"
_DEPLOYMENT_ID_KEY = "RAG_WANSHITONG_SSO_DEPLOYMENT_ID"
_ENTRIES_KEY = "RAG_WANSHITONG_SSO_ENTRIES"
_VALIDATE_URL_KEY = "RAG_WANSHITONG_SSO_VALIDATE_URL"
_CLIENT_ID_KEY = "RAG_WANSHITONG_SSO_CLIENT_ID"
_CLIENT_SECRET_FILE_KEY = (
    "RAG_WANSHITONG_SSO_CLIENT_SECRET_FILE"  # noqa: S105 - 环境变量名。
)
_DEPLOYMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ENTRY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_BASE_PATH = "/kb"
_MAX_CLIENT_ID_CHARS = 64

AuthMode = Literal["anonymous", "sso"]


@dataclass(frozen=True, slots=True)
class SsoEntry:
    """一个已登记浏览器入口及其固定 IdP authorize 地址。"""

    entry_id: str
    origin: str
    authorize_url: str
    callback_url: str


@dataclass(frozen=True, slots=True, repr=False)
class SsoSettings:
    """KB 作为 SP 所需的非 Secret 配置与 Secret 文件位置。"""

    auth_mode: AuthMode = "anonymous"
    deployment_id: str | None = None
    entries: tuple[SsoEntry, ...] = ()
    validate_url: str | None = None
    client_id: str | None = None
    client_secret_file: Path | None = None
    base_path: str = _BASE_PATH

    @property
    def enabled(self) -> bool:
        """仅在显式 SSO 模式下启用真实登录流。"""
        return self.auth_mode == "sso"

    def entry_for_origin(self, origin: str) -> SsoEntry:
        """按逐字符登记的 origin 选择唯一入口。

        Args:
            origin: 已按可信代理规则还原的浏览器 origin。

        Returns:
            与请求精确匹配的入口。

        Raises:
            ValueError: 请求 origin 未登记。

        """
        for entry in self.entries:
            if entry.origin == origin:
                return entry
        raise ValueError("当前浏览器入口未登记 SSO。")

    def entry_by_id(self, entry_id: str) -> SsoEntry:
        """按 pending 中冻结的 ID 读取入口。"""
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        raise ValueError("SSO pending 引用的入口不存在。")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        root_path: str,
    ) -> SsoSettings:
        """从环境变量构建配置，SSO 开启时缺项立即失败。

        Args:
            environment: 进程环境或测试映射。
            root_path: Product 对外路径前缀。

        Returns:
            已完成结构和 URL 校验的设置。

        Raises:
            ValueError: 模式、入口或 client 配置不完整。

        """
        raw_mode = environment.get(_AUTH_MODE_KEY, "anonymous").strip()
        if raw_mode not in {"anonymous", "sso"}:
            raise ValueError(
                "RAG_WANSHITONG_AUTH_MODE 仅支持 anonymous 或 sso。"
            )
        auth_mode = cast(AuthMode, raw_mode)
        if auth_mode == "anonymous":
            return cls(auth_mode=auth_mode)
        if root_path != _BASE_PATH:
            raise ValueError("SSO 模式要求 RAG_ROOT_PATH 精确为 /kb。")

        deployment_id = _required(environment, _DEPLOYMENT_ID_KEY)
        if not _DEPLOYMENT_ID_PATTERN.fullmatch(deployment_id):
            raise ValueError("SSO deployment ID 格式不安全。")
        entries = _parse_entries(_required(environment, _ENTRIES_KEY))
        validate_url = _validate_endpoint(
            _required(environment, _VALIDATE_URL_KEY),
            suffix="/sso/validate",
        )
        client_id = _required(environment, _CLIENT_ID_KEY)
        if len(client_id) > _MAX_CLIENT_ID_CHARS:
            raise ValueError("SSO clientId 不能超过 64 字符。")
        secret_file = Path(
            _required(environment, _CLIENT_SECRET_FILE_KEY)
        )
        return cls(
            auth_mode=auth_mode,
            deployment_id=deployment_id,
            entries=entries,
            validate_url=validate_url,
            client_id=client_id,
            client_secret_file=secret_file,
        )


def _required(environment: Mapping[str, str], key: str) -> str:
    value = environment.get(key, "").strip()
    if not value:
        raise ValueError(f"SSO 模式必须配置 {key}。")
    return value


def _parse_entries(raw_value: str) -> tuple[SsoEntry, ...]:
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError("RAG_WANSHITONG_SSO_ENTRIES 必须是 JSON。") from error
    if not isinstance(payload, list) or not payload:
        raise ValueError("SSO 入口注册表必须是非空 JSON 数组。")
    entries: list[SsoEntry] = []
    for raw_entry in payload:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "id",
            "origin",
            "authorize_url",
        }:
            raise ValueError("每个 SSO 入口必须只含 id/origin/authorize_url。")
        entry_id = raw_entry["id"]
        origin_value = raw_entry["origin"]
        authorize_value = raw_entry["authorize_url"]
        if not all(
            isinstance(value, str)
            for value in (entry_id, origin_value, authorize_value)
        ):
            raise ValueError("SSO 入口字段必须是字符串。")
        if not _ENTRY_ID_PATTERN.fullmatch(entry_id):
            raise ValueError("SSO 入口 ID 格式不安全。")
        origin = _validate_origin(origin_value)
        authorize_url = _validate_endpoint(
            authorize_value, suffix="/sso/authorize"
        )
        entries.append(
            SsoEntry(
                entry_id=entry_id,
                origin=origin,
                authorize_url=authorize_url,
                callback_url=f"{origin}{_BASE_PATH}/sso/callback",
            )
        )
    if len({entry.entry_id for entry in entries}) != len(entries):
        raise ValueError("SSO 入口 ID 不能重复。")
    if len({entry.origin for entry in entries}) != len(entries):
        raise ValueError("SSO 入口 origin 不能重复。")
    return tuple(entries)


def _validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        _port = parsed.port
    except ValueError as error:
        raise ValueError("SSO origin 端口无效。") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("SSO origin 必须是完整且无路径的 HTTP(S) origin。")
    return value.rstrip("/")


def _validate_endpoint(value: str, *, suffix: str) -> str:
    parsed = urlsplit(value)
    try:
        _port = parsed.port
    except ValueError as error:
        raise ValueError("SSO 端点端口无效。") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.endswith(suffix)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"SSO 端点必须是以 {suffix} 结尾的 HTTP(S) URL。")
    return value


__all__ = ["AuthMode", "SsoEntry", "SsoSettings"]
