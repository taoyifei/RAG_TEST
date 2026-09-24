"""湾事通上传格式的显式候选开关与媒体类型。"""

from __future__ import annotations

from pathlib import PurePosixPath

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
FORMAT_MEDIA_TYPES = {
    ".docx": DOCX_MEDIA_TYPE,
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    ".csv": "text/csv",
}
EXTRA_FORMATS = frozenset(FORMAT_MEDIA_TYPES) - {".docx"}


def extension_of(name: str) -> str:
    """返回末尾扩展名，不从文件名推断真实内容。"""
    return PurePosixPath(name).suffix.casefold()


def media_type_for_extension(extension: str) -> str:
    """返回服务端固定媒体类型，未知扩展名立即失败。"""
    return FORMAT_MEDIA_TYPES[extension]
