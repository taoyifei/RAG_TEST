"""将模板上传转换为只含目录提示的 DOCX，正文不进入检索索引。"""

from __future__ import annotations

import io
import unicodedata

from docx import Document

from rag_app.core.document_formats import extension_of
from rag_app.wanshitong.errors import AdminFacadeError


def is_template_source(source_relative_path: str) -> bool:
    """目录或文件名标记为模板时，将其视为只可提示存在的资料。"""
    normalized = unicodedata.normalize("NFKC", source_relative_path)
    return any("模板" in segment for segment in normalized.split("/"))


def searchable_upload_content(
    content: bytes,
    *,
    source_relative_path: str,
    document_title: str,
) -> bytes:
    """普通文档保持原样，模板仅保留可引用的目录项。"""
    if not is_template_source(source_relative_path):
        return content
    if extension_of(source_relative_path) != ".docx":
        raise AdminFacadeError(
            "TEMPLATE_FORMAT_UNSUPPORTED",
            "当前模板目录仅接受 DOCX；其他格式暂不入库。",
            status_code=415,
            stage="wanshitong.document.template",
        )
    document = Document()
    document.add_paragraph(
        f"模板目录项：{document_title}（模板）。"
        "模板正文未入库；具体填写项、示例及要求请参考原始模板。"
    )
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


__all__ = ["is_template_source", "searchable_upload_content"]
