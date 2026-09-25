"""模板上传保留原件，同时限制尚不支持的模板格式。"""

from __future__ import annotations

import io

import pytest
from docx import Document

from rag_app.wanshitong.errors import AdminFacadeError
from rag_app.wanshitong.template_catalog import (
    is_template_source,
    searchable_upload_content,
)


def _docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("用 SMART 原则填写目标，例如达到指定响应时间。")
    document.add_paragraph("XX 产品")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def test_template_directory_or_filename_keeps_original_docx() -> None:
    original = _docx_bytes()
    for path in (
        "04 开发中心/流程模板/设计说明书.docx",
        "04 开发中心/流程/设计说明书模板.docx",
    ):
        assert is_template_source(path)
        content = searchable_upload_content(
            original,
            source_relative_path=path,
            document_title="设计说明书",
        )
        assert content is original
        document = Document(io.BytesIO(content))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        assert "例如达到指定响应时间" in text
        assert "XX 产品" in text
        assert "模板正文未入库" not in text


def test_non_template_upload_keeps_original_bytes() -> None:
    original = _docx_bytes()
    assert not is_template_source("04 开发中心/工作指引/设计规范.docx")
    assert (
        searchable_upload_content(
            original,
            source_relative_path="04 开发中心/工作指引/设计规范.docx",
            document_title="设计规范",
        )
        is original
    )


def test_unsupported_template_format_still_rejected() -> None:
    with pytest.raises(AdminFacadeError) as error:
        searchable_upload_content(
            b"not-a-docx",
            source_relative_path="04 开发中心/流程模板/计划.xlsx",
            document_title="计划",
        )

    assert error.value.code == "TEMPLATE_FORMAT_UNSUPPORTED"
