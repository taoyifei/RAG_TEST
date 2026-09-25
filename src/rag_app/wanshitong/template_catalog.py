"""识别模板来源并保持上传原件的字节身份。"""

from __future__ import annotations

import io
import unicodedata

from docx import Document

from rag_app.core.document_formats import extension_of
from rag_app.wanshitong.errors import AdminFacadeError


def is_template_source(source_relative_path: str) -> bool:
    """判断目录或文件名是否标记为模板。"""
    normalized = unicodedata.normalize("NFKC", source_relative_path)
    return any("模板" in segment for segment in normalized.split("/"))


def searchable_upload_content(
    content: bytes,
    *,
    source_relative_path: str,
    document_title: str,
) -> bytes:
    """保留原件供安全 Parser 建立真实来源和填写指引。

    Args:
        content: 原始上传字节。
        source_relative_path: 受信目录中的相对路径。
        document_title: 兼容既有调用的显示标题；不参与内容改写。

    Returns:
        原始上传字节对象。

    Raises:
        AdminFacadeError: 模板格式暂不支持。

    """
    del document_title
    validate_template_source(source_relative_path)
    return content


def validate_template_source(source_relative_path: str) -> None:
    """上传入队前拒绝尚不支持的模板格式。"""
    if is_template_source(source_relative_path) and (
        extension_of(source_relative_path) != ".docx"
    ):
        raise AdminFacadeError(
            "TEMPLATE_FORMAT_UNSUPPORTED",
            "当前模板目录仅接受 DOCX；其他格式暂不入库。",
            status_code=415,
            stage="wanshitong.document.template",
        )


def legacy_template_catalog_content(document_title: str) -> bytes:
    """仅为无法解析的旧 `.doc` 模板制作存在性目录项。

    Args:
        document_title: 已由受信旧 DOC 路径确定的标题。

    Returns:
        不包含旧 DOC 原件正文的新 DOCX 字节。

    """
    document = Document()
    document.add_paragraph(
        f"模板目录项：{document_title}（模板）。"
        "模板正文未入库；具体填写项、示例及要求请参考原始模板。"
    )
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


__all__ = [
    "is_template_source",
    "legacy_template_catalog_content",
    "searchable_upload_content",
    "validate_template_source",
]
