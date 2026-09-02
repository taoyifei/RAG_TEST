"""组件描述符与格式中立能力声明。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, TypeAlias

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


CapabilitySupport: TypeAlias = bool | Literal["partial"]


class ParserCapabilities(FrozenModel):
    """Parser 对格式、结构和 IR schema 的诚实能力声明。"""

    supported_extensions: tuple[str, ...]
    supported_media_types: tuple[str, ...]
    supports_tables: CapabilitySupport = False
    supports_images: CapabilitySupport = False
    supports_numbering: CapabilitySupport = False
    supports_headers_footers: CapabilitySupport = False
    supports_footnotes: CapabilitySupport = False
    supports_revisions: CapabilitySupport = False
    supports_comments: CapabilitySupport = False
    supports_text_boxes: CapabilitySupport = False
    schema_version: str = Field(default="1", pattern=r"^1$")
