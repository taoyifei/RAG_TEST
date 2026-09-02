"""生成 DOCX v4 固定语料、摘要清单与规范化快照。"""

from __future__ import annotations

# ruff: noqa: E501
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document

from rag_app.core.errors import RagError
from rag_app.core.models import canonical_document_ir_json
from tests.adapters.parsers.docx.fixtures import (
    build_package as _build_minimal_package,
)
from tests.adapters.parsers.docx.fixtures import parse_package

_ROOT = Path(__file__).resolve().parent
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PNG_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""


def _python_docx_styles() -> str:
    """从 python-docx 基础文档取得真实 styles Part。"""
    output = io.BytesIO()
    Document().save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        return archive.read("word/styles.xml").decode("utf-8")


_BASE_STYLES = _python_docx_styles()


def build_package(blocks: str, **options: object) -> bytes:
    """以 python-docx styles 为基础安全 patch 复杂 OOXML。"""
    options.setdefault("styles", _BASE_STYLES)
    base_relationships = [
        _relationship("rIdBaseStyles", "styles", "styles.xml")
    ]
    if options.get("numbering") is not None:
        base_relationships.append(
            _relationship(
                "rIdBaseNumbering",
                "numbering",
                "numbering.xml",
            )
        )
    existing = options.get("document_relationships")
    if isinstance(existing, str):
        options["document_relationships"] = existing.replace(
            "</Relationships>",
            "\n" + "\n".join(base_relationships) + "\n</Relationships>",
        )
    else:
        options["document_relationships"] = _relationships(
            *base_relationships
        )
    return _build_minimal_package(blocks, **options)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class FixtureCase:
    """一个可重复生成与验证的 DOCX 夹具。"""

    name: str
    content: bytes
    policy: dict[str, object] = field(default_factory=dict)


def _relationships(*items: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Relationships xmlns="{_REL_NS}">\n'
        + "\n".join(items)
        + "\n</Relationships>\n"
    )


def _relationship(
    relationship_id: str,
    kind: str,
    target: str,
    *,
    external: bool = False,
) -> str:
    mode = ' TargetMode="External"' if external else ""
    return (
        f'<Relationship Id="{relationship_id}" '
        f'Type="{_OFFICE_REL}/{kind}" Target="{target}"{mode}/>'
    )


def _paragraph(text: str, properties: str = "") -> str:
    return (
        f"<w:p>{properties}<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _numbering() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:numbering xmlns:w="{_WORD_NS}">
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:suff w:val="space"/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%1.%2)"/><w:suff w:val="tab"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="7"><w:abstractNumId w:val="1"/></w:num>
  <w:num w:numId="8"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/></w:lvlOverride></w:num>
</w:numbering>
"""


def _list_item(text: str, num_id: int, level: int) -> str:
    properties = (
        "<w:pPr><w:numPr>"
        f'<w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/>'
        "</w:numPr></w:pPr>"
    )
    return _paragraph(text, properties)


def _image_package(blocks: str) -> bytes:
    relationships = _relationships(
        _relationship("rIdImage", "image", "media/image1.png")
    )
    return build_package(
        blocks,
        document_relationships=relationships,
        extra_entries={"word/media/image1.png": b"fixed-image-payload"},
        content_types=_PNG_CONTENT_TYPES,
    )


def _cases() -> tuple[FixtureCase, ...]:
    styles = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{_WORD_NS}">
  <w:style w:type="paragraph" w:styleId="OutlineBase"><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Custom"><w:name w:val="业务标题"/><w:basedOn w:val="OutlineBase"/></w:style>
</w:styles>
"""
    headings = build_package(
        _paragraph(
            "继承标题",
            '<w:pPr><w:pStyle w:val="Custom"/></w:pPr>',
        ),
        styles=styles,
    )
    multilevel = build_package(
        _list_item("一级", 7, 0) + _list_item("二级", 7, 1),
        numbering=_numbering(),
    )
    restart = build_package(
        _list_item("一", 7, 0)
        + _list_item("二", 7, 0)
        + _list_item("重启", 8, 0),
        numbering=_numbering(),
    )
    links = build_package(
        '<w:p><w:bookmarkStart w:id="1" w:name="target"/><w:r><w:t>目标</w:t></w:r></w:p>'
        '<w:p><w:hyperlink r:id="rIdLink"><w:r><w:t>外链</w:t></w:r></w:hyperlink>'
        '<w:fldSimple w:instr=" REF target "><w:r><w:t>引用结果</w:t></w:r></w:fldSimple></w:p>',
        document_relationships=_relationships(
            _relationship(
                "rIdLink",
                "hyperlink",
                "https://example.invalid/no-fetch",
                external=True,
            )
        ),
    )
    breaks = build_package(
        '<w:p><w:r><w:t>A</w:t><w:tab/><w:noBreakHyphen/><w:softHyphen/><w:br w:type="page"/></w:r>'
        '<w:r><w:rPr><w:vanish/></w:rPr><w:t>隐藏</w:t></w:r></w:p>'
    )
    basic_table = build_package(
        '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>'
        '<w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    )
    merged_table = build_package(
        '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>'
        '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>合并</w:t></w:r></w:p></w:tc><w:tc><w:p/></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr></w:tbl>'
    )
    omitted_grid = build_package(
        '<w:tbl><w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>'
        '<w:tc><w:p><w:r><w:t>省略网格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    )
    nested_image = _image_package(
        '<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc>'
        + _list_item("单元格列表", 7, 0)
        + '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="嵌套图"/><a:graphic><a:graphicData><a:blip r:embed="rIdImage"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
        + '<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>内表</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        + "</w:tc></w:tr></w:tbl>"
    )
    stories_rels = _relationships(
        _relationship("rIdH", "header", "header1.xml"),
        _relationship("rIdF", "footer", "footer1.xml"),
    )
    stories = build_package(
        _paragraph("正文")
        + '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/><w:footerReference w:type="default" r:id="rIdF"/></w:sectPr>',
        document_relationships=stories_rels,
        extra_entries={
            "word/header1.xml": f'<w:hdr xmlns:w="{_WORD_NS}">{_paragraph("页眉")}</w:hdr>',
            "word/footer1.xml": f'<w:ftr xmlns:w="{_WORD_NS}">{_paragraph("页脚")}</w:ftr>',
        },
    )
    notes = build_package(
        '<w:p><w:r><w:t>正文</w:t><w:footnoteReference w:id="2"/><w:endnoteReference w:id="3"/></w:r></w:p>',
        document_relationships=_relationships(
            _relationship("rIdFoot", "footnotes", "footnotes.xml"),
            _relationship("rIdEnd", "endnotes", "endnotes.xml"),
        ),
        extra_entries={
            "word/footnotes.xml": f'<w:footnotes xmlns:w="{_WORD_NS}"><w:footnote w:id="2">{_paragraph("脚注")}</w:footnote></w:footnotes>',
            "word/endnotes.xml": f'<w:endnotes xmlns:w="{_WORD_NS}"><w:endnote w:id="3">{_paragraph("尾注")}</w:endnote></w:endnotes>',
        },
    )
    comments = build_package(
        '<w:p><w:r><w:t>正文</w:t><w:commentReference w:id="4"/></w:r></w:p>',
        document_relationships=_relationships(
            _relationship("rIdComments", "comments", "comments.xml")
        ),
        extra_entries={
            "word/comments.xml": f'<w:comments xmlns:w="{_WORD_NS}"><w:comment w:id="4" w:author="审核人">{_paragraph("批注")}</w:comment></w:comments>'
        },
    )
    revisions = build_package(
        '<w:p><w:r><w:t>保留</w:t></w:r><w:del w:author="甲"><w:r><w:delText>删除</w:delText></w:r></w:del><w:ins w:author="乙"><w:r><w:t>新增</w:t></w:r></w:ins></w:p>'
    )
    image_variants = _image_package(
        '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="内嵌"/><a:graphic><a:graphicData><a:blip r:embed="rIdImage"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
        '<w:r><w:drawing><wp:anchor><wp:docPr id="2" name="浮动"/><a:graphic><a:graphicData><a:blip r:embed="rIdImage"/></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>'
        '<w:r><w:pict><v:shape><v:imagedata r:id="rIdImage" title="VML"/></v:shape></w:pict></w:r></w:p>'
    )
    controls = build_package(
        '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/></w:docPartObj></w:sdtPr><w:sdtContent>'
        + _paragraph("目录项")
        + "</w:sdtContent></w:sdt>"
        '<w:sdt><w:sdtPr><w:tag w:val="evidence"/></w:sdtPr><w:sdtContent>'
        + _paragraph("正式内容")
        + "</w:sdtContent></w:sdt>"
    )
    textbox = build_package(
        '<w:p><w:r><w:pict><v:shape><v:textbox><w:txbxContent>'
        + _paragraph("文本框正文")
        + "</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"
    )
    unsupported = build_package(
        '<w:p xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><w:r><w:t>fallback</w:t></w:r><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath><w:object><w:r><w:t>对象</w:t></w:r></w:object></w:p>'
    )
    external = build_package(
        '<w:p><w:hyperlink r:id="rIdExternal"><w:r><w:t>仅显示文字</w:t></w:r></w:hyperlink></w:p>',
        document_relationships=_relationships(
            _relationship(
                "rIdExternal",
                "hyperlink",
                "https://example.invalid/private",
                external=True,
            )
        ),
    )
    malformed = build_package(
        _paragraph("关系损坏"),
        document_relationships=_relationships(
            _relationship("rIdMissing", "styles", "missing.xml")
        ),
    )
    security = build_package(_paragraph("资源上限"))
    return (
        FixtureCase("01-headings-custom-outline.docx", headings),
        FixtureCase("02-numbering-multilevel.docx", multilevel),
        FixtureCase("03-numbering-restart-override.docx", restart),
        FixtureCase("04-hyperlinks-bookmarks-fields.docx", links),
        FixtureCase("05-breaks-tabs-hidden.docx", breaks),
        FixtureCase("06-table-basic.docx", basic_table),
        FixtureCase("07-table-gridspan-vmerge.docx", merged_table),
        FixtureCase("08-table-omitted-grid.docx", omitted_grid),
        FixtureCase("09-table-nested-list-image.docx", nested_image),
        FixtureCase("10-sections-headers-footers.docx", stories, {"headers_footers": "parse"}),
        FixtureCase("11-footnotes-endnotes.docx", notes, {"footnotes_endnotes": "parse"}),
        FixtureCase("12-comments.docx", comments, {"comments": "include"}),
        FixtureCase("13-tracked-changes.docx", revisions),
        FixtureCase("14-images-inline-anchor-vml.docx", image_variants, {"images": "extract"}),
        FixtureCase("15-content-controls-toc.docx", controls),
        FixtureCase("16-textbox.docx", textbox),
        FixtureCase("17-unsupported-math-ole-smartart.docx", unsupported),
        FixtureCase("18-external-relations.docx", external),
        FixtureCase("19-malformed-relationships.docx", malformed),
        FixtureCase("20-security-limits.docx", security, {"max_entries": 2}),
    )


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def main() -> None:
    """重新生成全部二进制夹具、快照与 SHA-256 manifest。"""
    expected_root = _ROOT / "expected"
    expected_root.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    for case in _cases():
        fixture_path = _ROOT / case.name
        fixture_path.write_bytes(case.content)
        expected_dir = expected_root / case.name.removesuffix(".docx")
        expected_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = parse_package(
                case.content,
                name=case.name,
                **case.policy,
            )
            ir_payload = json.loads(
                canonical_document_ir_json(
                    result.document_ir,
                    include_content=True,
                )
            )
            report_payload = result.report.model_dump(
                mode="json",
                exclude={"elapsed_seconds"},
            )
            status = "parsed"
        except RagError as error:
            failure = {
                "status": "rejected",
                "error": {"code": error.code, "stage": error.stage},
            }
            ir_payload = failure
            report_payload = failure
            status = "rejected"
        (_ROOT / "expected" / case.name.removesuffix(".docx") / "expected_ir.json").write_bytes(
            _json_bytes(ir_payload)
        )
        (_ROOT / "expected" / case.name.removesuffix(".docx") / "expected_report.json").write_bytes(
            _json_bytes(report_payload)
        )
        manifest.append(
            {
                "name": case.name,
                "sha256": hashlib.sha256(case.content).hexdigest(),
                "status": status,
                "policy": case.policy,
            }
        )
    (_ROOT / "manifest.json").write_bytes(_json_bytes(manifest))


if __name__ == "__main__":
    main()
