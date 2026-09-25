"""按原生 SearchResult 字段映射引用，不猜文件或页码。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def reference_id(
    trace_id: str, index: int, native: Mapping[str, Any]
) -> str:
    """为一次回答中的原生引用生成稳定的产品 ID。"""
    source = json.dumps(
        [
            trace_id,
            index,
            native.get("id"),
            native.get("knowledge_id"),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "ref_" + hashlib.sha256(source.encode()).hexdigest()[:32]


def resource_handle(native: Mapping[str, Any]) -> str | None:
    """只识别原生显式资源句柄，绝不代理任意 URL 或路径。"""
    metadata = native.get("metadata")
    if isinstance(metadata, dict):
        for key in ("resource_ref", "resource_handle", "file_path"):
            value = metadata.get(key)
            if isinstance(value, str) and value.startswith("resource://"):
                return value
    return None


def public_citation(
    native: Mapping[str, Any], *, reference_id: str
) -> dict[str, Any]:
    """按真实标题、片段和定位字段生成可显示引用。"""
    filename = native.get("knowledge_filename")
    title = native.get("knowledge_title")
    name = (
        filename
        if isinstance(filename, str) and filename
        else title
        if isinstance(title, str) and title
        else "未命名资料"
    )
    result: dict[str, Any] = {
        "reference_id": reference_id,
        "document_name": name,
        "document_title": name,
        "quote": native.get("content", "")
        if isinstance(native.get("content"), str)
        else "",
        "source_kind": "weknora",
        "source_available": (
            resource_handle(native) is not None
            or bool(native.get("content"))
        ),
        "original_available": bool(
            native.get("knowledge_id") and native.get("knowledge_filename")
        ),
        "native_chunk_id": native.get("id"),
        "native_knowledge_id": native.get("knowledge_id"),
        "native_reference": dict(native),
    }
    metadata = native.get("metadata")
    if isinstance(metadata, dict):
        for key in ("page_number", "page", "page_index"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                result["locator"] = f"第 {value.strip()} 页"
                break
    return result
