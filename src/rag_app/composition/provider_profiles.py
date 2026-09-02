"""P02 固定 Provider Profile 路径与模型目录入口。"""

from __future__ import annotations

import json
from pathlib import Path

from rag_app.composition.profiles import RagProfile, load_profile
from rag_app.core.errors import ConfigurationError

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROFILE_DIRECTORY = _REPOSITORY_ROOT / "configs" / "profiles"
PROVIDER_CATALOG = PROFILE_DIRECTORY / "catalog.json"


def load_named_provider_profile(name: str) -> RagProfile:
    """按安全文件名加载内置 Provider Profile。

    Args:
        name: 不含目录的 JSON 文件名。

    Returns:
        已严格验证的 Profile。

    Raises:
        ConfigurationError: 文件名包含路径或不是 JSON。

    """
    candidate = Path(name)
    if candidate.name != name or candidate.suffix != ".json":
        raise ConfigurationError(
            "Provider Profile 名必须是不含路径的 JSON 文件名。",
            stage="composition.provider_profile",
        )
    return load_profile(PROFILE_DIRECTORY / candidate)


def load_provider_catalog() -> dict[str, object]:
    """读取不含价格和 secret 的已核对 Provider 目录。

    Args:
        无参数；读取仓库内固定目录。

    Returns:
        JSON object 形式的 Provider 目录。

    Raises:
        ConfigurationError: 文件不是 UTF-8 JSON object。

    """
    try:
        payload = json.loads(PROVIDER_CATALOG.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            "Provider catalog 无法读取。",
            stage="composition.provider_catalog",
            details={"error_type": type(error).__name__},
        ) from None
    if not isinstance(payload, dict):
        raise ConfigurationError(
            "Provider catalog 顶层必须是 object。",
            stage="composition.provider_catalog",
        )
    return {str(key): value for key, value in payload.items()}


__all__ = [
    "PROFILE_DIRECTORY",
    "PROVIDER_CATALOG",
    "load_named_provider_profile",
    "load_provider_catalog",
]
