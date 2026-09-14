"""版本化 Provider 能力目录与自定义模型协议边界。"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from rag_app.adapters.providers.aliyun_models import (
    ALIYUN_GROUNDED_CHAT_MODELS,
)

_MAX_CUSTOM_MODEL_LENGTH = 200
_CONTROL_CODEPOINT_LIMIT = 32
_DELETE_CODEPOINT = 127


class CatalogProvider(BaseModel):
    """Provider 的只读产品配置边界。"""

    model_config = ConfigDict(frozen=True)

    provider_type: str
    display_name: str
    operations: tuple[str, ...]
    models: tuple[str, ...]
    operation_models: dict[str, tuple[str, ...]]
    regions: tuple[str, ...] = ()
    endpoint_profiles: tuple[str, ...] = ("default",)


CATALOG_VERSION: Final = "2026-09-14.2"
CAPABILITY_CATALOG_VERSION: Final = "2026-09-14.2"
# Provider 选项可以独立扩展；只有实际 Embedding 请求合同变化时才升级它。
# 保留该版本可避免新增 Provider 让既有 Jina/百炼 Profile 误触发重建索引。
EMBEDDING_CONTRACT_VERSION: Final = "2026-09-14.1"
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
        operation_models={
            "embedding.document": ("jina-embeddings-v5-text-small",),
            "embedding.query": ("jina-embeddings-v5-text-small",),
            "reranking": ("jina-reranker-v3.5",),
        },
    ),
    CatalogProvider(
        provider_type="aliyun-model-studio",
        display_name="阿里云百炼",
        operations=(
            "embedding.document",
            "embedding.query",
            "generation",
            "query.interpret",
            "query.rewrite",
            "image.ocr",
        ),
        models=(
            "qwen3.7-text-embedding",
            *ALIYUN_GROUNDED_CHAT_MODELS,
            "qwen3.5-ocr",
        ),
        operation_models={
            "embedding.document": ("qwen3.7-text-embedding",),
            "embedding.query": ("qwen3.7-text-embedding",),
            "generation": ALIYUN_GROUNDED_CHAT_MODELS,
            "query.interpret": ALIYUN_GROUNDED_CHAT_MODELS,
            "query.rewrite": ALIYUN_GROUNDED_CHAT_MODELS,
            "image.ocr": ("qwen3.5-ocr",),
        },
        regions=("cn-beijing",),
    ),
    CatalogProvider(
        provider_type="openai-compatible",
        display_name="OpenAI-compatible",
        operations=(
            "embedding.document",
            "embedding.query",
            "reranking",
            "generation",
            "query.interpret",
            "query.rewrite",
        ),
        models=(),
        operation_models={},
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
        "capability_catalog_version": CAPABILITY_CATALOG_VERSION,
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
    if operation not in provider.operations:
        raise ValueError("Provider、模型和操作组合不在内置目录中。")
    if provider_type == "openai-compatible":
        if (
            not model
            or model != model.strip()
            or len(model) > _MAX_CUSTOM_MODEL_LENGTH
            or any(
                ord(character) < _CONTROL_CODEPOINT_LIMIT
                or ord(character) == _DELETE_CODEPOINT
                for character in model
            )
        ):
            raise ValueError("自定义模型 ID 必须为 1 到 200 个可见字符。")
        return
    if model not in provider.models:
        raise ValueError("Provider、模型和操作组合不在内置目录中。")
    if model not in provider.operation_models.get(operation, ()):
        raise ValueError("模型用途与 Provider 操作不匹配。")


__all__ = [
    "CAPABILITY_CATALOG_VERSION",
    "CATALOG_VERSION",
    "EMBEDDING_CONTRACT_VERSION",
    "CatalogProvider",
    "provider_catalog",
    "require_provider",
    "validate_model",
]
