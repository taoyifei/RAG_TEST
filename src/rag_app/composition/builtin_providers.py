"""P02 真实 Provider 的可信显式 Registry 注册。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel

from rag_app.adapters.providers import (
    AliyunQwen37EmbeddingAdapter,
    AliyunQwen37EmbeddingConfig,
    JinaEmbeddingConfig,
    JinaRerankerConfig,
    JinaRerankerV35Adapter,
    JinaV5TextEmbeddingAdapter,
)
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.models.common import JsonObject


class _ProviderRegistry(Protocol):
    """真实 Provider 注册所需的最小可信 Registry 视图。"""

    def register_embedding(
        self,
        name: str,
        factory: Callable[[JsonObject], object],
        *,
        descriptor: ComponentDescriptor,
        config_model: type[BaseModel],
    ) -> None:
        """注册一个 Embedding factory。

        Args:
            name: 可信注册名。
            factory: 从脱敏配置创建组件的工厂。
            descriptor: 静态能力描述。
            config_model: 严格配置模型类型。

        Returns:
            无返回值。

        """
        ...

    def register_reranker(
        self,
        name: str,
        factory: Callable[[JsonObject], object],
        *,
        descriptor: ComponentDescriptor,
        config_model: type[BaseModel],
    ) -> None:
        """注册一个 Reranker factory。

        Args:
            name: 可信注册名。
            factory: 从脱敏配置创建组件的工厂。
            descriptor: 静态能力描述。
            config_model: 严格配置模型类型。

        Returns:
            无返回值。

        """
        ...


def register_builtin_provider_components(registry: _ProviderRegistry) -> None:
    """注册固定 Jina/Qwen3.7 adapters，构造时不发网络。

    Args:
        registry: 可信代码创建的显式 Registry。

    Returns:
        无返回值。

    """
    registry.register_embedding(
        "jina-embedding",
        lambda config: JinaV5TextEmbeddingAdapter(
            JinaEmbeddingConfig.model_validate(dict(config))
        ),
        descriptor=ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name="jina-embedding",
            version="jina-embeddings-v5-text-small",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
                dimensions=(1024,),
                roles=("document", "query"),
            ),
        ),
        config_model=JinaEmbeddingConfig,
    )
    registry.register_embedding(
        "aliyun-qwen37-embedding",
        lambda config: AliyunQwen37EmbeddingAdapter(
            AliyunQwen37EmbeddingConfig.model_validate(dict(config))
        ),
        descriptor=ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name="aliyun-qwen37-embedding",
            version="qwen3.7-text-embedding:dashscope-native:cn-beijing",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
                dimensions=(1024,),
                roles=("document", "query"),
            ),
        ),
        config_model=AliyunQwen37EmbeddingConfig,
    )
    registry.register_reranker(
        "jina-reranker",
        lambda config: JinaRerankerV35Adapter(
            JinaRerankerConfig.model_validate(dict(config))
        ),
        descriptor=ComponentDescriptor(
            kind=ComponentKind.RERANKER,
            name="jina-reranker",
            version="jina-reranker-v3.5",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                permits_network=True,
            ),
        ),
        config_model=JinaRerankerConfig,
    )


__all__ = ["register_builtin_provider_components"]
