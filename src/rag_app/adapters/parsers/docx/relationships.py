"""规范化 OPC Part URI 和 relationship target。"""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath

from rag_app.core.errors import InvalidDocument


def validate_archive_path(name: str) -> None:
    """拒绝绝对路径、反斜线与目录逃逸。

    Args:
        name: ZIP 条目名。

    Returns:
        无返回值。

    """
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or name.startswith("/")
    ):
        raise InvalidDocument(
            "DOCX ZIP 包含非法归档路径。",
            stage="docx-ooxml-v4.package",
        )


def normalize_absolute_part(part_uri: str) -> str:
    """规范化 content type 中的绝对 PartName。

    Args:
        part_uri: content types 声明的绝对 PartName。

    Returns:
        规范化 Part URI。

    """
    if not part_uri.startswith("/") or "\\" in part_uri:
        raise InvalidDocument(
            "DOCX content type PartName 无效。",
            stage="docx-ooxml-v4.package",
        )
    normalized = posixpath.normpath(part_uri)
    if normalized == "/" or normalized.startswith("/../"):
        raise InvalidDocument(
            "DOCX content type PartName 逃逸 package。",
            stage="docx-ooxml-v4.package",
        )
    return normalized


def relationship_source(relationship_name: str) -> str:
    """由 `.rels` Part 路径恢复其关系源 Part URI。

    Args:
        relationship_name: relationship ZIP 条目名。

    Returns:
        关系源 Part URI，根关系返回 `/`。

    """
    if relationship_name == "_rels/.rels":
        return "/"
    path = PurePosixPath(relationship_name)
    if path.parent.name != "_rels" or not path.name.endswith(".rels"):
        raise InvalidDocument(
            "DOCX relationship Part 路径无效。",
            stage="docx-ooxml-v4.relationship",
        )
    source_name = path.name.removesuffix(".rels")
    return f"/{path.parent.parent / source_name}"


def resolve_relationship_target(
    source_part_uri: str,
    target: str,
) -> str:
    """相对关系源解析内部 target，并拒绝 package 逃逸。

    Args:
        source_part_uri: 关系源 Part URI。
        target: relationship 的原始内部 target。

    Returns:
        规范化绝对 Part URI。

    """
    if not target or "\\" in target or target.startswith("//"):
        raise InvalidDocument(
            "DOCX relationship target 无效。",
            stage="docx-ooxml-v4.relationship",
        )
    if target.startswith("/"):
        candidate = target
    else:
        base = (
            "/"
            if source_part_uri == "/"
            else posixpath.dirname(source_part_uri)
        )
        candidate = posixpath.join(base, target)
    normalized = posixpath.normpath(candidate)
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if normalized == "/" or normalized.startswith("/../"):
        raise InvalidDocument(
            "DOCX relationship target 逃逸 package。",
            stage="docx-ooxml-v4.relationship",
        )
    return normalized


def archive_name(part_uri: str) -> str:
    """把绝对 Part URI 转为 ZIP 条目名。

    Args:
        part_uri: 绝对 Part URI。

    Returns:
        不带前导斜线的 ZIP 条目名。

    """
    return part_uri.removeprefix("/")
