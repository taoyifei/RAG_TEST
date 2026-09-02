from __future__ import annotations

# ruff: noqa: E501
import io
import zipfile
from pathlib import Path

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""
_STYLES = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
  </w:style>
</w:styles>
"""
_DOCUMENT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <w:body>{blocks}<w:sectPr/></w:body>
</w:document>
"""
HEADING = (
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    "<w:r><w:t>安装说明</w:t></w:r></w:p>"
)
PARAGRAPH = (
    "<w:p><w:r><w:t>第一步</w:t><w:tab/><w:t>准备环境</w:t>"
    "<w:br/><w:t>完成检查</w:t></w:r></w:p>"
)
LIST = (
    '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/>'
    "</w:numPr></w:pPr><w:r><w:t>列表项目 1</w:t></w:r></w:p>"
)
TABLE = (
    "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
)
CONTENT_CONTROL = (
    "<w:sdt><w:sdtPr><w:tag w:val=\"ordinary\"/></w:sdtPr><w:sdtContent>"
    "<w:p><w:r><w:t>受控正文</w:t></w:r></w:p></w:sdtContent></w:sdt>"
)
IMAGE = (
    '<w:p><w:r><w:drawing><a:blip r:embed="rIdImage"/>'
    "</w:drawing></w:r></w:p>"
)


def build_docx(
    blocks: str,
    *,
    relationships: str = "",
    extra_entries: dict[str, bytes] | None = None,
    unsafe_entry: bool = False,
) -> bytes:
    document = _DOCUMENT_TEMPLATE.format(blocks=blocks)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", _STYLES)
        if relationships:
            archive.writestr(
                "word/_rels/document.xml.rels",
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<Relationships xmlns=\"http://schemas.openxmlformats.org/"
                "package/2006/relationships\">"
                f"{relationships}</Relationships>",
            )
        for name, content in (extra_entries or {}).items():
            archive.writestr(name, content)
        if unsafe_entry:
            archive.writestr("../escape.txt", b"blocked")
    return buffer.getvalue()


def write_fixture_set(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    image_relationship = (
        '<Relationship Id="rIdImage" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/image" Target="media/pixel.png"/>'
    )
    external_relationship = (
        '<Relationship Id="rIdExternal" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/hyperlink" Target="https://example.invalid/" '
        'TargetMode="External"/>'
    )
    fixtures = {
        "simple-heading-paragraph.docx": build_docx(HEADING + PARAGRAPH),
        "simple-list.docx": build_docx(LIST),
        "simple-table.docx": build_docx(TABLE),
        "simple-image.docx": build_docx(
            IMAGE,
            relationships=image_relationship,
            extra_entries={"word/media/pixel.png": b"synthetic-png"},
        ),
        "content-control.docx": build_docx(CONTENT_CONTROL),
        "unsafe-path.docx": build_docx(PARAGRAPH, unsafe_entry=True),
        "external-relationship.docx": build_docx(
            PARAGRAPH,
            relationships=external_relationship,
        ),
    }
    for name, content in fixtures.items():
        (directory / name).write_bytes(content)
