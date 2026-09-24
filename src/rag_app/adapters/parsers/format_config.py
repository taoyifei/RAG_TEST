"""多格式候选部署开关，保持 Core 格式契约为纯数据。"""

from __future__ import annotations

import os

from rag_app.core.document_formats import EXTRA_FORMATS

_ENV_NAME = "RAG_WK_DOCUMENT_FORMATS"


def enabled_upload_extensions() -> frozenset[str]:
    """读取受控候选格式集合；未配置时保持产品 DOCX-only。"""
    raw = os.environ.get(_ENV_NAME, "")
    requested = frozenset(
        f".{item.strip().lower().lstrip('.')}"
        for item in raw.split(",")
        if item.strip()
    )
    unknown = requested - EXTRA_FORMATS
    if unknown:
        raise ValueError(f"{_ENV_NAME} 包含未支持格式：{sorted(unknown)}")
    return frozenset({".docx"}) | requested
