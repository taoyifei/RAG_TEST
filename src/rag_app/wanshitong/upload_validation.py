"""湾事通 DOCX-only 上传及相对路径安全校验。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from rag_app.core.models import DocumentRef, ParseContext, ParseSource
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import ParserPort
from rag_app.wanshitong.document_metadata import (
    DOCX_ONLY_MESSAGE,
    normalize_source_relative_path,
)
from rag_app.wanshitong.errors import AdminFacadeError

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_MAX_HTTP_UPLOAD_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ValidatedDocxUpload:
    """已通过产品入口与完整 OOXML 预检的上传。"""

    content: bytes
    display_name: str
    relative_path: str
    media_type: str = DOCX_MEDIA_TYPE


def validate_relative_path(value: str) -> tuple[str, str]:
    """规范化并验证浏览器提供的 DOCX 相对路径。

    Args:
        value: `webkitRelativePath` 或单文件 basename。

    Returns:
        规范化安全相对路径及 basename。

    Raises:
        AdminFacadeError: 路径包含绝对位置、穿越或控制字符。

    """
    return normalize_source_relative_path(value)


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


__all__ = [
    "DOCX_MEDIA_TYPE",
    "DOCX_ONLY_MESSAGE",
    "ValidatedDocxUpload",
    "read_and_validate_docx",
    "validate_relative_path",
]
