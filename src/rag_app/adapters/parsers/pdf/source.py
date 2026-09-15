"""PDF 本地签名、可读性、页数和文本层结构检查。"""

from __future__ import annotations

import hashlib
import io

from pypdf import PageObject, PdfReader

from rag_app.core.errors import InvalidDocument
from rag_app.core.models import ParseContext, ParseSource, PdfSourceInspection
from rag_app.core.policies import ParsingPolicy

_PDF_MEDIA_TYPES = frozenset({"application/pdf", "application/octet-stream"})


def inspect_pdf_source(
    source: ParseSource,
    policy: ParsingPolicy,
    context: ParseContext,
) -> PdfSourceInspection:
    """只检查 PDF，不把本地提取文字作为正文来源。

    Args:
        source: 上传的原始 PDF 字节。
        policy: 当前资源上限。
        context: 取消检查与逻辑文档身份。

    Returns:
        真实物理页数和逐页原生文本层标志。

    Raises:
        InvalidDocument: 输入损坏、加密、过大或不是 PDF。

    """
    if source.extension.casefold() != ".pdf":
        raise InvalidDocument(
            "PDF parser 仅接受 .pdf 扩展名。",
            stage="pdf.inspect.input",
        )
    if source.media_type.casefold() not in _PDF_MEDIA_TYPES:
        raise InvalidDocument(
            "PDF Content-Type 与文件格式不匹配。",
            stage="pdf.inspect.input",
        )
    if len(source.content) > policy.max_file_bytes:
        raise InvalidDocument(
            "PDF 文件大小超过 ParsingPolicy 限制。",
            stage="pdf.inspect.resource",
        )
    if not source.content.startswith(b"%PDF-"):
        raise InvalidDocument(
            "上传内容不是有效的 PDF 文件签名。",
            stage="pdf.inspect.signature",
        )
    if context.cancel_check is not None:
        context.cancel_check()
    reader = _pdf_reader(source.content)
    if bool(reader.is_encrypted):
        raise InvalidDocument(
            "加密 PDF 暂不支持解析。",
            stage="pdf.inspect.encrypted",
            code="PDF_ENCRYPTED",
        )
    try:
        page_count = len(reader.pages)
    except Exception as error:  # pypdf 会按损坏位置抛出不同异常。
        raise InvalidDocument(
            "PDF 页目录损坏或不可读取。",
            stage="pdf.inspect.structure",
            code="PDF_DAMAGED",
            details={"error_type": type(error).__name__},
        ) from None
    if page_count <= 0:
        raise InvalidDocument(
            "PDF 不包含可解析的物理页面。",
            stage="pdf.inspect.pages",
            code="PDF_EMPTY",
        )
    if page_count > policy.max_pdf_pages:
        raise InvalidDocument(
            "PDF 页数超过 ParsingPolicy 限制。",
            stage="pdf.inspect.resource",
            code="PDF_PAGE_LIMIT_EXCEEDED",
            details={
                "page_count": page_count,
                "max_pdf_pages": policy.max_pdf_pages,
            },
        )
    native_text_layer: list[bool] = []
    for page in reader.pages:
        if context.cancel_check is not None:
            context.cancel_check()
        native_text_layer.append(_has_native_text_operators(page))
    return PdfSourceInspection(
        source_sha256=hashlib.sha256(source.content).hexdigest(),
        page_count=page_count,
        native_text_layer=tuple(native_text_layer),
    )


def _pdf_reader(content: bytes) -> PdfReader:
    try:
        return PdfReader(io.BytesIO(content), strict=True)
    except Exception as error:
        raise InvalidDocument(
            "PDF 文件损坏或不可读取。",
            stage="pdf.inspect.structure",
            code="PDF_DAMAGED",
            details={"error_type": type(error).__name__},
        ) from None


def _has_native_text_operators(page: PageObject) -> bool:
    """只检查文本绘制操作，不解码或提取 PDF 正文。

    Args:
        page: pypdf 物理页面对象。

    Returns:
        页面内容流包含文本绘制操作时为 True。

    """
    try:
        contents = page.get_contents()
        if contents is None:
            return False
        return any(
            operator in {b"Tj", b"TJ", b"'", b'"'}
            for _operands, operator in contents.operations
        )
    except Exception:
        return False


__all__ = ["inspect_pdf_source"]
