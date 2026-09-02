"""组件描述符与格式中立能力声明。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, StrictInt

from rag_app.core._base import FrozenModel


class ComponentKind(StrEnum):
    """Registry 支持的组件职责。"""

    PARSER = "parser"
    CHUNKER = "chunker"
    EMBEDDING = "embedding"
    EMBEDDING_ROUTER = "embedding_router"
    RERANKER = "reranker"
    VECTOR_STORE = "vector_store"
    LEXICAL_STORE = "lexical_store"
    METADATA_STORE = "metadata_store"
    BLOB_STORE = "blob_store"
    GENERATOR = "generator"
    TRACE_SINK = "trace_sink"


class ProviderMode(StrEnum):
    """组件执行位置与网络边界。"""

    DETERMINISTIC = "deterministic"
    LOCAL = "local"
    REMOTE = "remote"
    LEGACY = "legacy"


class ComponentCapabilities(FrozenModel):
    """组合阶段使用的最小能力集合。"""

    supports_batch: bool = False
    permits_network: bool = False
    formats: tuple[str, ...] = ()
    dimensions: tuple[StrictInt, ...] = ()
    roles: tuple[str, ...] = ()


class ComponentDescriptor(FrozenModel):
    """不含 secret 的可审计组件身份。"""

    kind: ComponentKind
    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    version: str = Field(min_length=1, max_length=120)
    mode: ProviderMode
    source: str = Field(default="builtin", min_length=1, max_length=120)
    capabilities: ComponentCapabilities = ComponentCapabilities()
