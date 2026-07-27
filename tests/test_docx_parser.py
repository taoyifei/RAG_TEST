import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Inches
from PIL import Image

from rag_app.contracts import ElementKind, OcrState
from rag_app.parsers.docx import DocxParser, DocxParserLimits, UnsafeDocxError


def _write_test_image(path: Path) -> None:
    image = Image.new("RGB", (80, 40), color="white")
    image.save(path, format="PNG")


def test_docx_parser_preserves_order_and_locators(tmp_path: Path) -> None:
    image_path = tmp_path / "evidence.png"
    _write_test_image(image_path)
    docx_path = tmp_path / "sample.docx"
    document = Document()
    document.add_heading("总则", level=1)
    document.add_paragraph("正文证据")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "字段"
    table.cell(0, 1).text = "值"
    table.cell(1, 0).text = "状态"
    table.cell(1, 1).text = "有效"
    document.add_picture(str(image_path), width=Inches(1))
    document.save(docx_path)

    elements = DocxParser().parse(docx_path, display_path="资料/sample.docx")

    assert [element.kind for element in elements] == [
        ElementKind.HEADING,
        ElementKind.PARAGRAPH,
        ElementKind.TABLE,
        ElementKind.IMAGE,
    ]
    assert elements[1].locator.heading_path == ("总则",)
    assert elements[1].locator.paragraph_index == 1
    assert elements[2].text == "字段 | 值\n状态 | 有效"
    assert elements[2].locator.table_index == 1
    assert elements[3].locator.image_index == 1
    assert elements[3].media_type == "image/png"
    assert elements[3].ocr_state == OcrState.PENDING
    assert elements[3].binary_data is not None


def test_docx_parser_rejects_zip_bomb_metadata(tmp_path: Path) -> None:
    docx_path = tmp_path / "oversized.docx"
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "x" * 5000)

    parser = DocxParser(
        DocxParserLimits(
            max_file_bytes=20_000,
            max_uncompressed_bytes=1000,
            max_entry_bytes=10_000,
            max_entries=10,
            max_compression_ratio=20,
            timeout_seconds=5.0,
        )
    )

    with pytest.raises(UnsafeDocxError, match="解压总量"):
        parser.parse(docx_path, display_path="oversized.docx")


def test_docx_parser_rejects_path_traversal(tmp_path: Path) -> None:
    docx_path = tmp_path / "traversal.docx"
    with zipfile.ZipFile(docx_path, "w") as archive:
        archive.writestr("../outside", "bad")

    with pytest.raises(UnsafeDocxError, match="非法归档路径"):
        DocxParser().parse(docx_path, display_path="traversal.docx")


def test_zone_identifier_is_not_a_docx_input(tmp_path: Path) -> None:
    marker = tmp_path / "sample.docx:Zone.Identifier"
    marker.write_text("[ZoneTransfer]", encoding="utf-8")

    with pytest.raises(UnsafeDocxError, match=r"Zone\.Identifier"):
        DocxParser().parse(marker, display_path=marker.name)


def test_docx_parser_marks_list_item_without_changing_paragraph_kind(
    tmp_path: Path,
) -> None:
    docx_path = tmp_path / "list.docx"
    document = Document()
    document.add_paragraph("列表证据", style="List Bullet")
    document.save(docx_path)

    elements = DocxParser().parse(docx_path, display_path=docx_path.name)

    assert len(elements) == 1
    assert elements[0].kind == ElementKind.PARAGRAPH
    assert elements[0].list_level == 0
