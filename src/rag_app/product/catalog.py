"""版本化且禁止任意 Provider、模型和 Base URL 的内置目录。"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict


class CatalogProvider(BaseModel):
    """Provider 的只读产品配置边界。"""

    model_config = ConfigDict(frozen=True)

    provider_type: str
    display_name: str
    operations: tuple[str, ...]
    models: tuple[str, ...]
    regions: tuple[str, ...] = ()
    endpoint_profiles: tuple[str, ...] = ("default",)


CATALOG_VERSION: Final = "2026-09-04.1"
_PROVIDERS: Final = (
    CatalogProvider(
        provider_type="jina",
        display_name="Jina",
        operations=(
            "embedding.document",
            "embedding.query",
            "reranking",
        ),
        models=("jina-embeddings-v5-text-small", "jina-reranker-v3.5"),
    ),
    CatalogProvider(
        provider_type="aliyun-model-studio",
        display_name="阿里云百炼",
        operations=("embedding.document", "embedding.query"),
        models=("qwen3.7-text-embedding",),
        regions=("cn-beijing",),
    ),
)


def provider_catalog() -> dict[str, object]:
    """返回不含密钥或可变端点的内置目录。

    Args:
        无参数；读取进程内只读常量。

    Returns:
        可直接编码为 JSON 的版本化目录。

    """
    return {
        "catalog_version": CATALOG_VERSION,
        "providers": [item.model_dump(mode="json") for item in _PROVIDERS],
    }


def require_provider(provider_type: str) -> CatalogProvider:
    """返回受支持 Provider，否则拒绝任意扩展。

    Args:
        provider_type: 请求中的 Provider 类型。

    Returns:
        匹配的内置目录项。

    Raises:
        ValueError: Provider 不在内置目录中。

    """
    for provider in _PROVIDERS:
        if provider.provider_type == provider_type:
            return provider
    raise ValueError("Provider 不在内置产品目录中。")


def validate_model(
    provider_type: str,
    model: str,
    operation: str,
) -> None:
    """验证 Provider、模型和操作的目录组合。

    Args:
        provider_type: Provider 类型。
        model: 固定模型 ID。
        operation: 固定操作 ID。

    Returns:
        校验通过时无返回值。

    Raises:
        ValueError: 组合不在内置目录中。

    """
    provider = require_provider(provider_type)
    if operation not in provider.operations or model not in provider.models:
        raise ValueError("Provider、模型和操作组合不在内置目录中。")
    is_reranker = "reranker" in model
    if (operation == "reranking") != is_reranker:
        raise ValueError("模型用途与 Provider 操作不匹配。")


__all__ = [
    "CATALOG_VERSION",
    "CatalogProvider",
    "provider_catalog",
    "require_provider",
    "validate_model",
]
