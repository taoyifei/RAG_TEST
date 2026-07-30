"""只读取 DOCX 白名单内容的安全解析器。"""

from __future__ import annotations

import hashlib
import mimetypes
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from lxml import etree

from rag_app.contracts import Element, ElementKind, Locator, OcrState

__all__ = [
    "DocxParseAudit",
    "DocxParser",
    "DocxParserLimits",
    "UnsafeDocxError",
]

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_DRAWING_NAMESPACE = "http://schemas.openxmlformats.org/drawingml/2006/main"
_VML_NAMESPACE = "urn:schemas-microsoft-com:vml"
_NAMESPACES = {
    "a": _DRAWING_NAMESPACE,
    "r": _REL_NAMESPACE,
    "v": _VML_NAMESPACE,
    "w": _WORD_NAMESPACE,
}
_SUPPORTED_MEDIA = {
    ".emf": "image/emf",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}


class UnsafeDocxError(ValueError):
    """输入不满足 DOCX 安全边界。"""


@dataclass(frozen=True, slots=True)
class DocxParserLimits:
    """DOCX 解析资源边界。"""

    max_file_bytes: int = 128 * 1024 * 1024
    max_uncompressed_bytes: int = 512 * 1024 * 1024
    max_entry_bytes: int = 64 * 1024 * 1024
    max_entries: int = 10_000
    max_compression_ratio: float = 200.0
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class DocxParseAudit:
    """一次 DOCX 结构边界审计的非敏感计数。"""

    toc_controls_skipped: int
    ordinary_controls_parsed: int
    unsupported_nodes: int
    unsupported_content_with_evidence: int


@dataclass(slots=True)
class _AuditAccumulator:
    toc_controls_skipped: int = 0
    ordinary_controls_parsed: int = 0
    unsupported_nodes: int = 0
    unsupported_content_with_evidence: int = 0

    def freeze(self) -> DocxParseAudit:
        """生成不可变的公开审计快照。

        Args:
            无参数；复制当前累计计数。

        Returns:
            不含正文和文件名的结构审计计数。

        """
        return DocxParseAudit(
            toc_controls_skipped=self.toc_controls_skipped,
            ordinary_controls_parsed=self.ordinary_controls_parsed,
            unsupported_nodes=self.unsupported_nodes,
            unsupported_content_with_evidence=(
                self.unsupported_content_with_evidence
            ),
        )


@dataclass(frozen=True, slots=True)
class _ElementContext:
    display_path: str
    headings: tuple[str, ...]
    heading_index: int | None
    doc_sha256: str
    list_level: int | None = None


class DocxParser:
    """提取 DOCX 标题、段落、表格和受支持图片。"""

    version = "docx-parser-v3"

    def __init__(self, limits: DocxParserLimits | None = None) -> None:
        """初始化解析器。

        Args:
            limits: 可选的文件、解压量和耗时边界。

        Returns:
            无返回值。

        """
        self._limits = limits or DocxParserLimits()

    def parse(self, path: Path, *, display_path: str) -> list[Element]:
        """安全解析一个 DOCX 文件。

        Args:
            path: 本地 DOCX 文件。
            display_path: 写入 Locator 的展示路径。

        Returns:
            按正文顺序排列的标题、段落、表格和图片。

        Raises:
            UnsafeDocxError: 文件类型、归档结构或资源用量不安全。

        """
        elements, _ = self.parse_with_audit(path, display_path=display_path)
        return elements

    def parse_with_audit(
        self,
        path: Path,
        *,
        display_path: str,
    ) -> tuple[list[Element], DocxParseAudit]:
        """安全解析 DOCX 并返回不含正文的结构计数。

        Args:
            path: 本地 DOCX 文件。
            display_path: 写入 Locator 的展示路径。

        Returns:
            按正文顺序排列的元素和结构边界审计计数。

        Raises:
            UnsafeDocxError: 文件、归档或正文结构不满足安全边界。

        """
        started_at = time.monotonic()
        self._validate_input_path(path)
        doc_sha256 = _sha256(path.read_bytes())
        try:
            with zipfile.ZipFile(path) as archive:
                self._validate_archive(archive)
                return self._parse_archive(
                    archive,
                    display_path=display_path,
                    doc_sha256=doc_sha256,
                    started_at=started_at,
                )
        except zipfile.BadZipFile as error:
            raise UnsafeDocxError("DOCX 不是有效的 ZIP 归档。") from error

    def _validate_input_path(self, path: Path) -> None:
        if "Zone.Identifier" in path.name:
            raise UnsafeDocxError("Zone.Identifier 不是 DOCX 输入。")
        if path.suffix.lower() != ".docx":
            raise UnsafeDocxError("仅支持 .docx 文件。")
        if not path.is_file():
            raise UnsafeDocxError("DOCX 输入文件不存在。")
        if path.stat().st_size > self._limits.max_file_bytes:
            raise UnsafeDocxError("DOCX 文件大小超过限制。")

    def _validate_archive(self, archive: zipfile.ZipFile) -> None:
        """在读取正文前验证归档资源与结构安全边界。

        Args:
            archive: 已打开但尚未解析正文的 DOCX ZIP 归档。

        Returns:
            无返回值。

        Raises:
            UnsafeDocxError: 路径、加密状态、资源用量或必要条目不安全。

        """
        entries = archive.infolist()
        if len(entries) > self._limits.max_entries:
            raise UnsafeDocxError("ZIP 条目数超过限制。")
        total_size = 0
        for entry in entries:
            _validate_archive_path(entry.filename)
            if entry.flag_bits & 0x1:
                raise UnsafeDocxError("不接受加密 ZIP 条目。")
            if entry.file_size > self._limits.max_entry_bytes:
                raise UnsafeDocxError("ZIP 单条目解压量超过限制。")
            total_size += entry.file_size
            if total_size > self._limits.max_uncompressed_bytes:
                raise UnsafeDocxError("ZIP 解压总量超过限制。")
            if entry.file_size and not entry.compress_size:
                raise UnsafeDocxError("ZIP 条目压缩大小异常。")
            if entry.compress_size:
                ratio = entry.file_size / entry.compress_size
                if ratio > self._limits.max_compression_ratio:
                    raise UnsafeDocxError("ZIP 条目压缩比超过限制。")
        required = {"[Content_Types].xml", "word/document.xml"}
        if not required.issubset(archive.namelist()):
            raise UnsafeDocxError("DOCX 缺少必要的 Open XML 条目。")

    def _parse_archive(
        self,
        archive: zipfile.ZipFile,
        *,
        display_path: str,
        doc_sha256: str,
        started_at: float,
    ) -> tuple[list[Element], DocxParseAudit]:
        """按正文顺序提取元素并汇总结构边界审计。

        Args:
            archive: 已通过资源边界校验的 DOCX ZIP 归档。
            display_path: 写入元素定位器的展示路径。
            doc_sha256: 当前文档内容的稳定摘要。
            started_at: 用于执行解析超时检查的单调时钟起点。

        Returns:
            正文元素列表及不含正文内容的结构审计计数。

        Raises:
            UnsafeDocxError: 正文缺失、结构含不可安全忽略的证据，
                或解析超过时限。

        """
        document_root = _parse_xml(archive.read("word/document.xml"))
        relationships = _read_relationships(archive)
        heading_styles = _read_heading_styles(archive)
        body = document_root.find(f"{{{_WORD_NAMESPACE}}}body")
        if body is None:
            raise UnsafeDocxError("DOCX 正文结构缺失。")

        elements: list[Element] = []
        headings: list[str] = []
        heading_index = 0
        current_heading_index: int | None = None
        paragraph_index = 0
        table_index = 0
        image_index = 0
        audit = _AuditAccumulator()
        for child in _iter_blocks(body, audit):
            self._check_timeout(started_at)
            local_name = etree.QName(child).localname
            if local_name == "p":
                text = _paragraph_text(child)
                heading_level = _heading_level(child, heading_styles)
                if text and heading_level is not None:
                    heading_index += 1
                    current_heading_index = heading_index
                    headings = _updated_headings(headings, heading_level, text)
                    context = _ElementContext(
                        display_path=display_path,
                        headings=tuple(headings),
                        heading_index=current_heading_index,
                        doc_sha256=doc_sha256,
                    )
                    elements.append(
                        _text_element(
                            kind=ElementKind.HEADING,
                            text=text,
                            context=context,
                        )
                    )
                elif text:
                    paragraph_index += 1
                    context = _ElementContext(
                        display_path=display_path,
                        headings=tuple(headings),
                        heading_index=current_heading_index,
                        doc_sha256=doc_sha256,
                        list_level=_list_level(child),
                    )
                    elements.append(
                        _text_element(
                            kind=ElementKind.PARAGRAPH,
                            text=text,
                            context=context,
                            paragraph_index=paragraph_index,
                        )
                    )
                for relationship_id in _image_relationship_ids(child):
                    image_index += 1
                    context = _ElementContext(
                        display_path=display_path,
                        headings=tuple(headings),
                        heading_index=current_heading_index,
                        doc_sha256=doc_sha256,
                    )
                    image = _image_element(
                        archive=archive,
                        relationship_id=relationship_id,
                        relationships=relationships,
                        context=context,
                        image_index=image_index,
                    )
                    if image is not None:
                        elements.append(image)
            else:
                table_index += 1
                context = _ElementContext(
                    display_path=display_path,
                    headings=tuple(headings),
                    heading_index=current_heading_index,
                    doc_sha256=doc_sha256,
                )
                table_text = _table_text(child)
                if table_text:
                    elements.append(
                        _text_element(
                            kind=ElementKind.TABLE,
                            text=table_text,
                            context=context,
                            table_index=table_index,
                        )
                    )
                for relationship_id in _image_relationship_ids(child):
                    image_index += 1
                    image = _image_element(
                        archive=archive,
                        relationship_id=relationship_id,
                        relationships=relationships,
                        context=context,
                        image_index=image_index,
                    )
                    if image is not None:
                        elements.append(image)
        return elements, audit.freeze()

    def _check_timeout(self, started_at: float) -> None:
        if time.monotonic() - started_at > self._limits.timeout_seconds:
            raise UnsafeDocxError("DOCX 解析耗时超过限制。")


def _iter_blocks(
    container: etree._Element,
    audit: _AuditAccumulator,
) -> Iterator[etree._Element]:
    """递归展开受支持的正文块并记录结构边界决策。

    该迭代会原地更新审计计数；目录控件被忽略，含可索引证据的未知
    结构会被拒绝。

    Args:
        container: 待遍历的正文或内容控件容器。
        audit: 本次解析共享的可变审计累加器。

    Returns:
        按文档顺序产生段落和表格的迭代器。

    Raises:
        UnsafeDocxError: 未知结构中包含不可安全忽略的可索引证据。

    """
    for child in container:
        local_name = etree.QName(child).localname
        if local_name in {"p", "tbl"}:
            yield child
            continue
        if local_name == "sdt":
            if _is_toc_control(child):
                audit.toc_controls_skipped += 1
                continue
            content = child.find(f"./{{{_WORD_NAMESPACE}}}sdtContent")
            if content is None:
                _skip_or_reject_unknown(child, audit)
                continue
            audit.ordinary_controls_parsed += 1
            yield from _iter_blocks(content, audit)
            continue
        _skip_or_reject_unknown(child, audit)


def _is_toc_control(control: etree._Element) -> bool:
    return any(
        value.strip().casefold() == "table of contents"
        for value in _xpath_strings(
            control,
            "./w:sdtPr//w:docPartGallery/@w:val",
        )
    )


def _skip_or_reject_unknown(
    node: etree._Element,
    audit: _AuditAccumulator,
) -> None:
    if _contains_indexable_evidence(node):
        audit.unsupported_content_with_evidence += 1
        raise UnsafeDocxError("不支持的 DOCX 正文结构包含可索引证据。")
    audit.unsupported_nodes += 1


def _contains_indexable_evidence(node: etree._Element) -> bool:
    if any(value.strip() for value in _xpath_strings(node, ".//w:t/text()")):
        return True
    if _image_relationship_ids(node):
        return True
    return bool(node.xpath(".//w:tbl", namespaces=_NAMESPACES))


def _validate_archive_path(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise UnsafeDocxError(f"非法归档路径: {name!r}。")


def _parse_xml(content: bytes) -> etree._Element:
    parser = etree.XMLParser(
        load_dtd=False,
        no_network=True,
        recover=False,
        resolve_entities=False,
    )
    try:
        return etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as error:
        raise UnsafeDocxError("DOCX XML 结构无效。") from error


def _read_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    """读取指向归档内部受支持媒体目录的关系。

    外部关系和非媒体关系不会进入返回映射。

    Args:
        archive: 已通过资源边界校验的 DOCX ZIP 归档。

    Returns:
        关系标识到归档内部媒体路径的映射。

    Raises:
        UnsafeDocxError: 关系 XML 或内部媒体路径不满足安全边界。

    """
    path = "word/_rels/document.xml.rels"
    if path not in archive.namelist():
        return {}
    root = _parse_xml(archive.read(path))
    relationships: dict[str, str] = {}
    for relation in root.findall(f"{{{_PACKAGE_REL_NAMESPACE}}}Relationship"):
        if relation.get("TargetMode") == "External":
            continue
        relationship_id = relation.get("Id")
        target = relation.get("Target")
        if relationship_id and target and target.startswith("media/"):
            _validate_archive_path(target)
            relationships[relationship_id] = f"word/{target}"
    return relationships


def _read_heading_styles(archive: zipfile.ZipFile) -> dict[str, int]:
    path = "word/styles.xml"
    if path not in archive.namelist():
        return {}
    root = _parse_xml(archive.read(path))
    styles: dict[str, int] = {}
    for style in root.findall(f".//{{{_WORD_NAMESPACE}}}style"):
        style_id = style.get(f"{{{_WORD_NAMESPACE}}}styleId")
        name_node = style.find(f"{{{_WORD_NAMESPACE}}}name")
        name = "" if name_node is None else (
            name_node.get(f"{{{_WORD_NAMESPACE}}}val") or ""
        )
        level = _style_heading_level(style_id or "", name, style)
        if style_id and level is not None:
            styles[style_id] = level
    return styles


def _style_heading_level(
    style_id: str,
    name: str,
    style: etree._Element,
) -> int | None:
    """从样式名称或大纲级别推断受限的标题层级。

    Args:
        style_id: Word 样式标识。
        name: Word 样式显示名称。
        style: 样式对应的 Open XML 节点。

    Returns:
        一到九级标题层级；无法识别时返回 `None`。

    """
    normalized = f"{style_id} {name}".lower().replace(" ", "")
    for prefix in ("heading", "标题"):
        if prefix in normalized:
            suffix = normalized.rsplit(prefix, maxsplit=1)[-1]
            if suffix.isdigit():
                return max(1, min(9, int(suffix)))
    outline = style.find(f".//{{{_WORD_NAMESPACE}}}outlineLvl")
    if outline is not None:
        value = outline.get(f"{{{_WORD_NAMESPACE}}}val")
        if value is not None and value.isdigit():
            return max(1, min(9, int(value) + 1))
    return None


def _heading_level(
    paragraph: etree._Element,
    heading_styles: dict[str, int],
) -> int | None:
    style = paragraph.find(
        f"./{{{_WORD_NAMESPACE}}}pPr/{{{_WORD_NAMESPACE}}}pStyle"
    )
    if style is None:
        return None
    style_id = style.get(f"{{{_WORD_NAMESPACE}}}val")
    return None if style_id is None else heading_styles.get(style_id)


def _list_level(paragraph: etree._Element) -> int | None:
    """从编号属性或列表样式推断段落列表层级。

    Args:
        paragraph: 待判定的 Word 段落节点。

    Returns:
        零到八级列表层级；普通段落返回 `None`。

    """
    properties = paragraph.find(f"./{{{_WORD_NAMESPACE}}}pPr")
    if properties is None:
        return None
    level = properties.find(
        f"./{{{_WORD_NAMESPACE}}}numPr/{{{_WORD_NAMESPACE}}}ilvl"
    )
    if level is not None:
        value = level.get(f"{{{_WORD_NAMESPACE}}}val")
        if value is not None and value.isdigit():
            return min(8, int(value))
    style = properties.find(f"./{{{_WORD_NAMESPACE}}}pStyle")
    if style is None:
        return None
    style_id = style.get(f"{{{_WORD_NAMESPACE}}}val") or ""
    normalized = style_id.lower().replace(" ", "")
    if normalized.startswith("list") or "列表" in normalized:
        return 0
    return None


def _paragraph_text(paragraph: etree._Element) -> str:
    """提取段落文本并保留制表符与显式换行语义。

    Args:
        paragraph: 待提取的 Word 段落节点。

    Returns:
        合并后的段落文本；仅含空白时返回空字符串。

    """
    parts: list[str] = []
    for node in paragraph.iter():
        qualified_name = etree.QName(node)
        if qualified_name.namespace != _WORD_NAMESPACE:
            continue
        if qualified_name.localname == "t" and node.text is not None:
            parts.append(node.text)
        elif qualified_name.localname == "tab":
            parts.append("\t")
        elif qualified_name.localname in {"br", "cr"}:
            parts.append("\n")
    text = "".join(parts)
    if not text.strip():
        return ""
    return text.strip(" ")


def _table_text(table: etree._Element) -> str:
    rows: list[str] = []
    row_path = f"./{{{_WORD_NAMESPACE}}}tr"
    cell_path = f"./{{{_WORD_NAMESPACE}}}tc"
    for row in table.findall(row_path):
        cells = [
            " ".join(_xpath_strings(cell, ".//w:t/text()")).strip()
            for cell in row.findall(cell_path)
        ]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _updated_headings(
    headings: list[str],
    level: int,
    text: str,
) -> list[str]:
    updated = headings[: level - 1]
    while len(updated) < level - 1:
        updated.append("")
    updated.append(text)
    return updated


def _text_element(
    *,
    kind: ElementKind,
    text: str,
    context: _ElementContext,
    paragraph_index: int | None = None,
    table_index: int | None = None,
) -> Element:
    content_sha256 = _sha256(text.encode("utf-8"))
    locator = Locator(
        file_path=context.display_path,
        heading_path=tuple(item for item in context.headings if item),
        heading_index=context.heading_index,
        paragraph_index=paragraph_index,
        table_index=table_index,
        fragment=text[:240],
    )
    element_id = _element_id(
        context.doc_sha256,
        kind,
        locator,
        content_sha256,
    )
    return Element(
        element_id=element_id,
        kind=kind,
        text=text,
        locator=locator,
        content_sha256=content_sha256,
        list_level=context.list_level,
    )


def _image_relationship_ids(paragraph: etree._Element) -> list[str]:
    return _xpath_strings(
        paragraph,
        ".//a:blip/@r:embed | .//v:imagedata/@r:id",
    )


def _xpath_strings(element: etree._Element, expression: str) -> list[str]:
    result = element.xpath(expression, namespaces=_NAMESPACES)
    return [str(item) for item in cast(list[object], result)]


def _image_element(
    *,
    archive: zipfile.ZipFile,
    relationship_id: str,
    relationships: dict[str, str],
    context: _ElementContext,
    image_index: int,
) -> Element | None:
    """将可接受的内嵌媒体构造成待 OCR 的图片元素。

    Args:
        archive: 已通过资源边界校验的 DOCX ZIP 归档。
        relationship_id: 图片节点引用的关系标识。
        relationships: 内部媒体关系映射。
        context: 当前标题路径和文档摘要上下文。
        image_index: 图片在正文遍历中的一基序号。

    Returns:
        带二进制内容和稳定定位器的图片元素；关系缺失或媒体类型
        不受支持时返回 `None`。

    """
    media_path = relationships.get(relationship_id)
    if media_path is None or media_path not in archive.namelist():
        return None
    extension = Path(media_path).suffix.lower()
    media_type = _SUPPORTED_MEDIA.get(extension)
    if media_type is None:
        guessed_type, _ = mimetypes.guess_type(media_path)
        if guessed_type not in {"image/jpeg", "image/png"}:
            return None
        media_type = guessed_type
    binary_data = archive.read(media_path)
    content_sha256 = _sha256(binary_data)
    media_name = PurePosixPath(media_path).name
    locator = Locator(
        file_path=context.display_path,
        heading_path=tuple(item for item in context.headings if item),
        heading_index=context.heading_index,
        image_index=image_index,
        fragment=media_name,
    )
    element_id = _element_id(
        context.doc_sha256,
        ElementKind.IMAGE,
        locator,
        content_sha256,
    )
    return Element(
        element_id=element_id,
        kind=ElementKind.IMAGE,
        text="",
        locator=locator,
        content_sha256=content_sha256,
        media_type=media_type,
        media_name=media_name,
        binary_data=binary_data,
        ocr_state=OcrState.PENDING,
    )


def _element_id(
    doc_sha256: str,
    kind: ElementKind,
    locator: Locator,
    content_sha256: str,
) -> str:
    payload = "\x1e".join(
        (doc_sha256, kind.value, locator.logical_key(), content_sha256)
    )
    return f"element_{_sha256(payload.encode('utf-8'))[:32]}"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
