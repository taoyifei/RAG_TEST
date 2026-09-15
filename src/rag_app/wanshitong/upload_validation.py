"""湾事通 DOCX-only 上传及相对路径安全校验。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from fastapi import Request

from rag_app.core.models import DocumentRef, ParseContext, ParseSource
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import ParserPort
from rag_app.wanshitong.errors import AdminFacadeError

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document"
)
DOCX_ONLY_MESSAGE = (
    "当前湾事通 Demo 仅开放 DOCX 文档。PDF、旧 DOC、Excel 和 ZIP "
    "将在后续版本接入。"
)
_MAX_HTTP_UPLOAD_BYTES = 32 * 1024 * 1024
_MAX_RELATIVE_PATH_CHARS = 4096
_MAX_PATH_SEGMENT_CHARS = 255
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class ValidatedDocxUpload:
    """已通过产品入口与完整 OOXML 预检的上传。"""

    content: bytes
    display_name: str
    relative_path: str
    media_type: str = DOCX_MEDIA_TYPE


def validate_relative_path(value: str) -> tuple[str, str]:
    """验证并原样保留浏览器提供的 POSIX 相对路径。

    Args:
        value: `webkitRelativePath` 或单文件 basename。

    Returns:
        原始安全相对路径及 basename。

    Raises:
        AdminFacadeError: 路径包含绝对位置、穿越或控制字符。

    """
    if not value or len(value) > _MAX_RELATIVE_PATH_CHARS:
        raise _relative_path_error()
    if "\\" in value or value.startswith("/") or _WINDOWS_DRIVE.match(value):
        raise _relative_path_error()
    if unicodedata.normalize("NFC", value) != value:
        raise _relative_path_error()
    segments = value.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or len(segment) > _MAX_PATH_SEGMENT_CHARS
        or any(unicodedata.category(char).startswith("C") for char in segment)
        for segment in segments
    ):
        raise _relative_path_error()
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise _relative_path_error()
    display_name = segments[-1]
    if not display_name.casefold().endswith(".docx"):
        raise AdminFacadeError(
            "DOCX_ONLY",
            DOCX_ONLY_MESSAGE,
            status_code=415,
            stage="wanshitong.document.type",
        )
    return value, display_name


async def read_and_validate_docx(
    request: Request,
    *,
    relative_path: str,
    document: DocumentRef,
    parser: ParserPort,
    parsing_policy: ParsingPolicy,
) -> ValidatedDocxUpload:
    """在创建 Universal Job 前完成大小、路径和完整 OOXML 预检。"""
    safe_path, display_name = validate_relative_path(relative_path)
    media_type = request.headers.get("content-type", "").partition(";")[0]
    if media_type.strip().casefold() != DOCX_MEDIA_TYPE:
        raise AdminFacadeError(
            "DOCX_ONLY",
            DOCX_ONLY_MESSAGE,
            status_code=415,
            stage="wanshitong.document.type",
        )
    limit = min(_MAX_HTTP_UPLOAD_BYTES, parsing_policy.max_file_bytes)
    chunks: list[bytes] = []
    observed = 0
    async for chunk in request.stream():
        observed += len(chunk)
        if observed > limit:
            raise AdminFacadeError(
                "UPLOAD_TOO_LARGE",
                "上传超过服务端大小上限。",
                status_code=413,
                stage="wanshitong.document.upload",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise AdminFacadeError(
            "INVALID_DOCUMENT",
            "DOCX 文档不能为空。",
            stage="wanshitong.document.upload",
        )
    parser.parse(
        ParseSource(
            media_type=DOCX_MEDIA_TYPE,
            display_name=display_name,
            extension=".docx",
            content=content,
        ),
        parsing_policy,
        ParseContext(
            document=document.model_copy(update={"display_name": display_name})
        ),
    )
    return ValidatedDocxUpload(
        content=content,
        display_name=display_name,
        relative_path=safe_path,
    )


def _relative_path_error() -> AdminFacadeError:
    return AdminFacadeError(
        "INVALID_RELATIVE_PATH",
        "文档相对路径无效，仅允许安全的目录相对路径。",
        stage="wanshitong.document.path",
    )


__all__ = [
    "DOCX_MEDIA_TYPE",
    "DOCX_ONLY_MESSAGE",
    "ValidatedDocxUpload",
    "read_and_validate_docx",
    "validate_relative_path",
]
