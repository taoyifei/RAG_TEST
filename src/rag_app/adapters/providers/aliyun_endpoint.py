"""百炼北京 Native 端点与不透明工作空间标识的独立校验。"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

NATIVE_EMBEDDING_PATH = (
    "/api/v1/services/embeddings/text-embedding/text-embedding"
)
_DNS_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cn-beijing\.maas\.aliyuncs\.com"
)


class AliyunEndpointError(ValueError):
    """携带稳定原因码且不复制实际 Host 的本地输入错误。"""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class AliyunEndpointConfig(BaseModel):
    """不含 Secret 的端点配置，禁止通过 ID 前缀推导可用性。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint_mode: Literal["workspace_host", "beijing_dashscope"] = (
        "workspace_host"
    )
    transport: Literal["dashscope-native"] = "dashscope-native"
    region: Literal["cn-beijing"] = "cn-beijing"
    workspace_id: str
    api_host: str | None = None

    @field_validator("workspace_id")
    @classmethod
    def _workspace_shape(cls, value: str) -> str:
        # 仅移除外侧普通空格；控制字符即使位于边界也拒绝。
        value = value.strip(" ")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value, flags=re.ASCII) is None:
            raise ValueError("Workspace ID 必须为 1 到 200 个 ASCII 安全字符。")
        return value


def resolve_endpoint(config: AliyunEndpointConfig) -> str:
    """规范受信任裸主机或 HTTPS Origin，不猜测或切换主机。

    Args:
        config: 已通过工作空间形状检查的非敏感配置。

    Returns:
        不含路径的规范化 HTTPS Origin。

    Raises:
        ValueError: Host 缺失或不在当前模式的严格范围。

    """
    if config.endpoint_mode == "beijing_dashscope" and config.api_host is None:
        return "https://dashscope.aliyuncs.com"
    value = config.api_host or ""
    if not value:
        raise AliyunEndpointError(
            "ALIYUN_API_HOST_REQUIRED",
            "请从当前北京业务空间复制API Host，或明确选择北京DashScope模式。",
        )
    if not value.isascii() or any(char.isspace() for char in value):
        raise AliyunEndpointError(
            "ALIYUN_API_HOST_FORMAT_INVALID",
            "API Host 不能包含空白或非 ASCII 字符。",
        )
    # 在 URL 解析之前拒绝解析器可能规范化掉的分隔符和控制字符。
    if any(char in value for char in "?#%\\") or not value.isprintable():
        raise AliyunEndpointError(
            "ALIYUN_API_HOST_FORMAT_INVALID", "API Host 含禁止的分隔符。"
        )
    candidate = value if "://" in value else "https://" + value
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise AliyunEndpointError(
            "ALIYUN_API_HOST_FORMAT_INVALID", "API Host 格式无效。"
        ) from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.netloc.lower() not in (host, host + ":443")
    ):
        raise AliyunEndpointError(
            "ALIYUN_API_HOST_FORMAT_INVALID",
            "API Host 必须是无认证信息、查询参数或路径的 HTTPS Origin。",
        )
    if config.endpoint_mode == "workspace_host":
        allowed = _DNS_HOST.fullmatch(host) is not None
    else:
        allowed = host == "dashscope.aliyuncs.com"
    if not allowed:
        raise AliyunEndpointError(
            "ALIYUN_API_HOST_NOT_ALLOWED",
            "API Host 不在所选北京端点模式的受信任范围。",
        )
    return f"https://{host}"
