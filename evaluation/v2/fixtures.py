"""生成无外链、字节稳定且不含私有内容的合成 DOCX。"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass

from rag_app.core.identifiers import canonical_sha256

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels"
    ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""
_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdDocument"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>
"""
_NOTE_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdFootnotes"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
    Target="footnotes.xml"/>
  <Relationship Id="rIdEndnotes"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"
    Target="endnotes.xml"/>
</Relationships>
"""
_FOOTNOTES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/'
    'wordprocessingml/2006/main">\n'
    '  <w:footnote w:id="2"><w:p><w:r><w:t>脚注事实 FOOT-22。'
    "</w:t></w:r></w:p></w:footnote>\n"
    "</w:footnotes>\n"
)
_ENDNOTES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<w:endnotes xmlns:w="http://schemas.openxmlformats.org/'
    'wordprocessingml/2006/main">\n'
    '  <w:endnote w:id="3"><w:p><w:r><w:t>尾注事实 END-33。'
    "</w:t></w:r></w:p></w:endnote>\n"
    "</w:endnotes>\n"
)
_STYLES = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
  </w:style>
</w:styles>
"""
_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:v="urn:schemas-microsoft-com:vml">
  <w:body>{body}<w:sectPr/></w:body>
</w:document>
"""
_PARAGRAPH = "<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """合成文档的稳定 OOXML body 和覆盖标签。"""

    body: str
    coverage_tags: tuple[str, ...]


def _paragraph(text: str) -> str:
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return _PARAGRAPH.format(text=escaped)


def _heading(level: int, text: str) -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


_LONG_TEXT = " ".join(
    (
        "结构化长段落 LONG-1 第1句用于验证跨 Chunk 边界",
        *(
            f"结构化长段落第{index}句用于验证跨 Chunk 边界"
            for index in range(2, 121)
        ),
    )
)
_TABLE_EMPTY = """
<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
<w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>
<w:tc><w:p/></w:tc>
<w:tc><w:p><w:r><w:t>C</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>P00001</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>12.5 kg</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>-3%</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
"""
_TABLE_OMITTED = """
<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
<w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>
<w:tc><w:p><w:r><w:t>中列</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>尾列</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:trPr><w:gridAfter w:val="1"/></w:trPr>
<w:tc><w:p><w:r><w:t>首列</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>OMIT-2026 2026-09-03</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
"""
_TABLE_MERGED = """
<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
<w:tr><w:trPr><w:tblHeader/></w:trPr>
<w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
<w:p><w:r><w:t>产品信息</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>状态</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:trPr><w:tblHeader/></w:trPr>
<w:tc><w:p><w:r><w:t>型号</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>单位</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>日期</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>
<w:p><w:r><w:t>ABC-01</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>20 mm</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>2026-08-31</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>
<w:tc><w:p><w:r><w:t>21 mm</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>2026-09-01</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
"""
_NESTED_TABLE = """
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>外层</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc>
<w:p><w:r><w:t>嵌套值 NEST-7</w:t></w:r></w:p>
</w:tc></w:tr></w:tbl>
</w:tc></w:tr></w:tbl>
"""
_TEXT_BOX = """
<w:p><w:r><w:pict><v:shape><v:textbox><w:txbxContent>
<w:p><w:r><w:t>文本框事实 TX-42</w:t></w:r></w:p>
</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>
"""
_NOTE_REFERENCES = """
<w:p><w:r><w:t>正文注释引用</w:t><w:footnoteReference w:id="2"/>
<w:endnoteReference w:id="3"/></w:r></w:p>
"""

_FIXTURES: dict[str, FixtureSpec] = {
    "shared-bytes": FixtureSpec(
        _paragraph("共享字节事实 P00001 对应蓝色组件。"),
        ("same_bytes_different_document_id", "short_paragraph"),
    ),
    "rename-stable": FixtureSpec(
        _paragraph("RENAME-1 仅修改显示名时版本身份保持稳定。"),
        ("rename_only",),
    ),
    "version-original": FixtureSpec(
        _paragraph("版本事实为旧值 10。"),
        ("content_version_change",),
    ),
    "version-changed": FixtureSpec(
        _paragraph("VERSION-20 版本事实为新值 20。"),
        ("content_version_change", "active_revision"),
    ),
    "same-name-alpha": FixtureSpec(
        _paragraph("同名文档甲的唯一事实 ALPHA-9。"),
        ("same_name_different_content",),
    ),
    "same-name-beta": FixtureSpec(
        _paragraph("同名文档乙的唯一事实 BETA-8。"),
        ("same_name_different_content",),
    ),
    "hierarchy-long": FixtureSpec(
        _paragraph("标题前正文 PREAMBLE-1。")
        + _heading(1, "安装说明")
        + _heading(2, "准备阶段")
        + _paragraph(_LONG_TEXT)
        + (
            '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/>'
            '<w:numId w:val="1"/></w:numPr></w:pPr>'
            '<w:r><w:t>编号列表第一项</w:t></w:r></w:p>'
        )
        + (
            '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/>'
            '<w:numId w:val="2"/></w:numPr></w:pPr>'
            '<w:r><w:t>重启编号第一项</w:t></w:r></w:p>'
        ),
        (
            "long_paragraph_cross_chunk",
            "multilevel_heading",
            "body_before_heading",
            "numbered_list_restart",
        ),
    ),
    "notes-textbox": FixtureSpec(
        _NOTE_REFERENCES + _TEXT_BOX,
        ("footnote_endnote", "text_box"),
    ),
    "table-empty": FixtureSpec(
        _heading(1, "空列参数表") + _TABLE_EMPTY,
        (
            "table_exact_row",
            "table_middle_empty",
            "numeric_unit_negative_percent_date",
        ),
    ),
    "table-omitted": FixtureSpec(
        _heading(1, "省略列参数表") + _TABLE_OMITTED,
        ("table_edge_empty", "table_grid_before_after"),
    ),
    "table-merged": FixtureSpec(
        _heading(1, "合并表头") + _TABLE_MERGED + _NESTED_TABLE,
        (
            "table_grid_span",
            "table_vmerge",
            "table_multirow_header",
            "nested_table",
        ),
    ),
    "identifiers": FixtureSpec(
        _paragraph(
            "标识符包括 GB/T 1234-2025、ABC-01、MiXeD-型号-7 和 P00001。"
        ),
        (
            "identifier_standard",
            "identifier_hyphen",
            "identifier_mixed_language",
            "identifier_nfkc_case",
            "identifier_near_miss",
        ),
    ),
    "scope-similar": FixtureSpec(
        _paragraph(
            "另一个知识库中的相似事实 RED-ONLY-77 对应红色组件。"
        ),
        ("similar_document_other_kb",),
    ),
    "conflict": FixtureSpec(
        _paragraph("冲突来源一声明阈值为 30。")
        + _paragraph("冲突来源二声明阈值为 40。"),
        ("conflicting_evidence",),
    ),
    "negative": FixtureSpec(
        _paragraph("该文档只讨论维护周期，不包含火星或不存在的型号。"),
        (
            "knowledge_base_no_answer",
            "topic_similar_unsupported",
            "wrong_document",
        ),
    ),
}


def fixture_bytes(fixture_id: str) -> bytes:
    """生成指定合成 DOCX 的稳定字节。

    Args:
        fixture_id: 受控 fixture catalog 键。

    Returns:
        时间戳固定且无外部关系的 DOCX 字节。

    Raises:
        KeyError: fixture ID 不在受控目录。

    """
    spec = _FIXTURES[fixture_id]
    entries = {
        "[Content_Types].xml": _CONTENT_TYPES.encode(),
        "_rels/.rels": _ROOT_RELS.encode(),
        "word/document.xml": _DOCUMENT.format(body=spec.body).encode(),
        "word/styles.xml": _STYLES.encode(),
    }
    if fixture_id == "notes-textbox":
        entries.update(
            {
                "word/_rels/document.xml.rels": _NOTE_RELS.encode(),
                "word/footnotes.xml": _FOOTNOTES.encode(),
                "word/endnotes.xml": _ENDNOTES.encode(),
            }
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def fixture_sha256(fixture_id: str) -> str:
    """返回一个合成 DOCX 的裸 SHA-256。

    Args:
        fixture_id: 受控 fixture catalog 键。

    Returns:
        64 位小写十六进制摘要。

    """
    return hashlib.sha256(fixture_bytes(fixture_id)).hexdigest()


def fixture_catalog_sha256() -> str:
    """返回覆盖标签和稳定字节摘要的目录指纹。

    Args:
        无参数；读取模块内固定目录。

    Returns:
        带算法前缀的规范 SHA-256。

    """
    return canonical_sha256(
        {
            fixture_id: {
                "coverage_tags": spec.coverage_tags,
                "content_sha256": fixture_sha256(fixture_id),
            }
            for fixture_id, spec in sorted(_FIXTURES.items())
        }
    )


def fixture_coverage_tags(fixture_id: str) -> tuple[str, ...]:
    """返回 Manifest 必须精确声明的覆盖标签。

    Args:
        fixture_id: 受控 fixture catalog 键。

    Returns:
        固定且不可变的覆盖标签。

    """
    return _FIXTURES[fixture_id].coverage_tags


__all__ = [
    "fixture_bytes",
    "fixture_catalog_sha256",
    "fixture_coverage_tags",
    "fixture_sha256",
]
