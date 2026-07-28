"""PaddleOCR 内部服务与客户端的懒加载公共接口。"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag_app.ocr.client import OcrClient
    from rag_app.ocr.models import (
        OcrEngineResult,
        OcrLine,
        OcrRequest,
        OcrResponse,
    )

__all__ = [
    "DEFAULT_OCR_REVISION",
    "OcrClient",
    "OcrEngineResult",
    "OcrLine",
    "OcrRequest",
    "OcrResponse",
]

_MODEL_EXPORTS = frozenset(
    {
        "DEFAULT_OCR_REVISION",
        "OcrEngineResult",
        "OcrLine",
        "OcrRequest",
        "OcrResponse",
    }
)


def __getattr__(name: str) -> object:
    """按需导入 OCR 公共对象。

    Args:
        name: 调用方请求的公共属性名。

    Returns:
        OCR 客户端或模型契约对象。

    Raises:
        AttributeError: 属性不属于公开 OCR 接口。

    """
    if name == "OcrClient":
        module_name = "rag_app.ocr.client"
    elif name in _MODEL_EXPORTS:
        module_name = "rag_app.ocr.models"
    else:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
