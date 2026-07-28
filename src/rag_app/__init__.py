"""DOCX RAG 应用的懒加载公共接口。"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag_app.contracts import (
        Chunk,
        Element,
        ElementKind,
        IndexManifest,
        Locator,
        OcrState,
        Parser,
        PipelineSpec,
        SourceRecord,
    )

__all__ = [
    "Chunk",
    "Element",
    "ElementKind",
    "IndexManifest",
    "Locator",
    "OcrState",
    "Parser",
    "PipelineSpec",
    "SourceRecord",
]

_CONTRACT_EXPORTS = frozenset(__all__)


def __getattr__(name: str) -> object:
    """按需导入主应用契约，避免 OCR 隔离环境加载 Python 3.11 模块。

    Args:
        name: 调用方请求的公共属性名。

    Returns:
        `rag_app.contracts` 中对应的公共对象。

    Raises:
        AttributeError: 属性不属于公开契约。

    """
    if name not in _CONTRACT_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module("rag_app.contracts")
    value = getattr(module, name)
    globals()[name] = value
    return value
