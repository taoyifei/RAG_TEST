"""湾事通上传的受控格式路由、大小与路径校验。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from rag_app.adapters.parsers.format_config import enabled_upload_extensions
from rag_app.core.document_formats import (
    FORMAT_MEDIA_TYPES,
    extension_of,
)
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


def validate_relative_path(
    value: str,
    *,
    allowed_extensions: frozenset[str] | None = None,
) -> tuple[str, str]:
    """规范化并验证浏览器提供的相对路径。

    Args:
        value: `webkitRelativePath` 或单文件 basename。
        allowed_extensions: 显式允许的格式集合。

    Returns:
        规范化安全相对路径及 basename。

    Raises:
        AdminFacadeError: 路径包含绝对位置、穿越或控制字符。

    """
    return normalize_source_relative_path(
        value, allowed_extensions=allowed_extensions
    )


async def read_and_validate_docx(
    request: Request,
    *,
    relative_path: str,
    document: DocumentRef,
    parser: ParserPort,
    parsing_policy: ParsingPolicy,
) -> ValidatedDocxUpload:
    """在创建 Universal Job 前完成大小、路径和完整 OOXML 预检。"""
    return await _read_and_validate_upload(
        request,
        relative_path=relative_path,
        document=document,
        parser=parser,
        parsing_policy=parsing_policy,
        allowed_extensions=frozenset({".docx"}),
    )


async def read_and_validate_upload(
    request: Request,
    *,
    relative_path: str,
    document: DocumentRef,
    parser: ParserPort,
    parsing_policy: ParsingPolicy,
) -> ValidatedDocxUpload:
    """候选格式按显式开关分流，失败时不创建文档 Job。"""
    return await _read_and_validate_upload(
        request,
        relative_path=relative_path,
        document=document,
        parser=parser,
        parsing_policy=parsing_policy,
        allowed_extensions=enabled_upload_extensions(),
    )


async def _read_and_validate_upload(  # noqa: PLR0913
    request: Request,
    *,
    relative_path: str,
    document: DocumentRef,
    parser: ParserPort,
    parsing_policy: ParsingPolicy,
    allowed_extensions: frozenset[str],
) -> ValidatedDocxUpload:
    safe_path, display_name = validate_relative_path(
        relative_path, allowed_extensions=allowed_extensions
    )
    extension = extension_of(display_name)
    expected_media_type = FORMAT_MEDIA_TYPES[extension]
    media_type = request.headers.get("content-type", "").partition(";")[0]
    supplied_media_type = media_type.strip().casefold()
    accepted_media_types = {expected_media_type}
    if extension != ".docx":
        accepted_media_types.add("application/octet-stream")
    if supplied_media_type not in accepted_media_types:
        raise AdminFacadeError(
            "DOCX_ONLY" if extension == ".docx" else "FORMAT_MIME_MISMATCH",
            DOCX_ONLY_MESSAGE
            if extension == ".docx"
            else "上传 Content-Type 与文件格式不匹配。",
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
            "文档不能为空。",
            stage="wanshitong.document.upload",
        )
    await run_in_threadpool(
        parser.parse,
        ParseSource(
            media_type=expected_media_type,
            display_name=display_name,
            extension=extension,
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
        media_type=expected_media_type,
    )


__all__ = [
    "DOCX_MEDIA_TYPE",
    "DOCX_ONLY_MESSAGE",
    "ValidatedDocxUpload",
    "read_and_validate_docx",
    "read_and_validate_upload",
    "validate_relative_path",
]
