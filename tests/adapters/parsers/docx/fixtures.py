from __future__ import annotations

# ruff: noqa: E501
import io
import zipfile

from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.core.models import ParseResult, ParseSource
from rag_app.core.policies import ParsingPolicy

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
</Types>
"""
_ROOT_RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdOfficeDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"
 xmlns:v="urn:schemas-microsoft-com:vml">
  <w:body>{blocks}<w:sectPr/></w:body>
</w:document>
"""
_STYLES = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
  </w:style>
</w:styles>
"""


def build_package(  # noqa: PLR0913
    blocks: str,
    *,
    styles: str = _STYLES,
    numbering: str | None = None,
    document_relationships: str | None = None,
    extra_entries: dict[str, bytes | str] | None = None,
    content_types: str = _CONTENT_TYPES,
    root_relationships: str = _ROOT_RELATIONSHIPS,
) -> bytes:
    """构造最小且安全的合成 DOCX package。

    Args:
        blocks: 按顺序放入正文的 OOXML block。
        styles: 可选样式 Part XML。
        numbering: 可选编号 Part XML。
        document_relationships: 可选主文档关系 XML。
        extra_entries: 可选附加 package 条目。
        content_types: 可选 OPC content types XML。
        root_relationships: 可选 OPC 根关系 XML。

    Returns:
        内存中的 DOCX ZIP 字节。

    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        _write_entry(archive, "[Content_Types].xml", content_types)
        _write_entry(archive, "_rels/.rels", root_relationships)
        _write_entry(
            archive,
            "word/document.xml", _DOCUMENT.format(blocks=blocks)
        )
        _write_entry(archive, "word/styles.xml", styles)
        if numbering is not None:
            _write_entry(archive, "word/numbering.xml", numbering)
        if document_relationships is not None:
            _write_entry(
                archive,
                "word/_rels/document.xml.rels",
                document_relationships,
            )
        for name, content in (extra_entries or {}).items():
            _write_entry(archive, name, content)
    return buffer.getvalue()


def _write_entry(
    archive: zipfile.ZipFile,
    name: str,
    content: bytes | str,
) -> None:
    """用固定元数据写入可重复的 ZIP 条目。"""
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    archive.writestr(info, content)


def policy(**updates: object) -> ParsingPolicy:
    """创建带稳定逻辑身份的解析策略。

    Args:
        **updates: 覆盖默认策略的字段。

    Returns:
        可直接用于合成夹具的解析策略。

    """
    values: dict[str, object] = {
        "metadata": (
            ("project_id", f"prj_{'1' * 32}"),
            ("knowledge_base_id", f"kb_{'2' * 32}"),
            ("document_id", f"doc_{'3' * 32}"),
        )
    }
    values.update(updates)
    return ParsingPolicy.model_validate(values)


def source(content: bytes, name: str = "sample.docx") -> ParseSource:
    """创建不依赖文件系统的受控解析源。

    Args:
        content: DOCX package 字节。
        name: 仅用于显示和扩展名判断的名称。

    Returns:
        内存 ParseSource。

    """
    return ParseSource(
        media_type="application/octet-stream",
        display_name=name,
        extension="." + name.rsplit(".", maxsplit=1)[-1],
        content=content,
    )


def parse_package(
    content: bytes,
    *,
    name: str = "sample.docx",
    **policy_updates: object,
) -> ParseResult:
    """用 v4 parser 解析一个合成 package。

    Args:
        content: DOCX package 字节。
        name: 显示名。
        **policy_updates: 策略覆盖值。

    Returns:
        冻结的 Document IR 与 ParseReport。

    """
    return DocxOoxmlV4Parser().parse(
        source(content, name),
        policy(**policy_updates),
    )
