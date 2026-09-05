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
    """解析受信任 HTTPS Origin，不猜测或切换主机。

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
        raise ValueError(
            "请从当前北京业务空间复制API Host，或明确选择北京DashScope模式。"
        )
    if not value.isascii() or any(char.isspace() for char in value):
        raise ValueError("API Host 不能包含空白或非 ASCII 字符。")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(char in value for char in "?#%\\")
        or parsed.netloc.lower() not in (host, host + ":443")
    ):
        raise ValueError(
            "API Host 必须是无认证信息、查询参数或路径的 HTTPS Origin。"
        )
    if config.endpoint_mode == "workspace_host":
        allowed = _DNS_HOST.fullmatch(host) is not None
    else:
        allowed = host == "dashscope.aliyuncs.com"
    if not allowed:
        raise ValueError("API Host 不在所选北京端点模式的受信任范围。")
    return f"https://{host}"
