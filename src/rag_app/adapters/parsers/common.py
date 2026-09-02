"""DOCX adapters 共享的格式签名和最小文本规范化。"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from lxml import etree

from rag_app.core.errors import InvalidDocument

_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_DOCUMENT_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document.main+xml"
)
_SUPPORTED_IMAGE_EXTENSIONS = frozenset({".emf", ".jpeg", ".jpg", ".png"})


@dataclass(frozen=True, slots=True)
class DocxPackageAudit:
    """不含正文的 DOCX package 特征计数。"""

    external_relationships: int
    revision_insertions: int
    revision_deletions: int
    hidden_text_markers: int
    comments_parts: int
    header_footer_parts: int
    footnote_endnote_parts: int
    unsupported_media_relationships: int


def normalize_document_text(value: str) -> str:
    """只统一换行符，不做检索改写。

    Args:
        value: Parser 提取的有效可见文本。

    Returns:
        仅将 CRLF/CR 统一为 LF 的文本。

    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


def inspect_docx_package(content: bytes) -> DocxPackageAudit:
    """校验 DOCX package signature/content type 并汇总特征。

    Args:
        content: 待检查的受控输入字节。

    Returns:
        后续策略判断所需的非敏感计数。

    Raises:
        InvalidDocument: 输入不是合法 DOCX package 或主文档类型不符。

    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = tuple(archive.namelist())
            if "[Content_Types].xml" not in names:
                raise InvalidDocument(
                    "DOCX 缺少 package content types。",
                    stage="parser.signature",
                )
            content_types = _safe_xml(archive.read("[Content_Types].xml"))
            overrides = content_types.findall(
                f"{{{_CONTENT_TYPES_NAMESPACE}}}Override"
            )
            valid_main = any(
                item.get("PartName") == "/word/document.xml"
                and item.get("ContentType") == _DOCUMENT_CONTENT_TYPE
                for item in overrides
            )
            if not valid_main:
                raise InvalidDocument(
                    "DOCX 主文档 content type 不匹配。",
                    stage="parser.signature",
                )
            xml_payloads = {
                name: archive.read(name)
                for name in names
                if name.startswith("word/")
                and name.endswith((".xml", ".rels"))
            }
    except (zipfile.BadZipFile, KeyError, OSError) as error:
        raise InvalidDocument(
            "输入不是有效 DOCX package。",
            stage="parser.signature",
            details={"error_type": type(error).__name__},
        ) from None

    joined = b"\n".join(xml_payloads.values())
    unsupported_media = 0
    for name, payload in xml_payloads.items():
        if not name.endswith(".rels"):
            continue
        root = _safe_xml(payload)
        for relationship in root:
            relationship_type = relationship.get("Type") or ""
            target = relationship.get("Target") or ""
            if (
                relationship_type.endswith("/image")
                and relationship.get("TargetMode") != "External"
                and PurePosixPath(target).suffix.casefold()
                not in _SUPPORTED_IMAGE_EXTENSIONS
            ):
                unsupported_media += 1
    return DocxPackageAudit(
        external_relationships=sum(
            payload.count(b'TargetMode="External"')
            + payload.count(b"TargetMode='External'")
            for name, payload in xml_payloads.items()
            if name.endswith(".rels")
        ),
        revision_insertions=joined.count(b"<w:ins"),
        revision_deletions=joined.count(b"<w:del"),
        hidden_text_markers=joined.count(b"<w:vanish"),
        comments_parts=int("word/comments.xml" in xml_payloads),
        header_footer_parts=sum(
            name.startswith(("word/header", "word/footer"))
            for name in xml_payloads
        ),
        footnote_endnote_parts=sum(
            name in {"word/footnotes.xml", "word/endnotes.xml"}
            for name in xml_payloads
        ),
        unsupported_media_relationships=unsupported_media,
    )


def _safe_xml(content: bytes) -> etree._Element:
    parser = etree.XMLParser(
        load_dtd=False,
        no_network=True,
        recover=False,
        resolve_entities=False,
    )
    try:
        return etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError:
        raise InvalidDocument(
            "DOCX package XML 结构无效。",
            stage="parser.signature",
        ) from None
