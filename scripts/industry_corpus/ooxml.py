"""安全审计、清洗并保守修复工业 DOCX 的标题结构。"""

from __future__ import annotations

import hashlib
import re
import stat
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from lxml import etree

from rag_app.contracts import ElementKind
from rag_app.parsers.docx import DocxParser, UnsafeDocxError

__all__ = [
    "HeadingDecision",
    "OoxmlAudit",
    "OoxmlPreparationError",
    "clean_docx",
    "heading_candidate",
]

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_NAMESPACES = {"r": _REL_NAMESPACE, "w": _WORD_NAMESPACE}
_HEADING_PATTERN = re.compile(
    r"^\s*([1-9]\d*(?:\.[1-9]\d*){0,2})(?:[.、)]|\s)\s*(\S.*)\s*$"
)
_SENTENCE_PUNCTUATION = frozenset("。！？!?；;：:")
_MAX_HEADING_CHARACTERS = 60
_MAX_ZIP_ENTRIES = 10_000
_MAX_ENTRY_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200.0
_PRIVATE_CHARACTER = "\ue004"
_XML_SUFFIXES = (".xml", ".rels")
_DISALLOWED_PART_PREFIXES = (
    "word/embeddings/",
    "word/activex/",
)
_DISALLOWED_PART_NAMES = {
    "word/vbaproject.bin",
    "word/vbadata.xml",
}
_PACKAGE_OBJECT_NAMES = {"oleobject", "object", "control"}
_MINIMUM_RELATIONSHIP_PARTS = 3


class OoxmlPreparationError(ValueError):
    """表示转换产物不满足工业 DOCX 安全边界。"""


@dataclass(frozen=True, slots=True)
class HeadingDecision:
    """单个段落的保守标题候选判断。"""

    candidate: bool
    accepted: bool
    level: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class OoxmlAudit:
    """不含正文和外部 URL 的单文档清洗审计。"""

    visible_text_sha256: str
    paragraph_count: int
    table_count: int
    image_count: int
    heading_count: int
    list_level_count: int
    ocr_candidate_count: int
    heading_candidate_count: int
    heading_accepted_count: int
    heading_rejected_count: int
    heading_reason_counts: dict[str, int]
    removed_private_character_count: int
    remaining_private_character_count: int
    removed_external_relationship_count: int
    external_relationship_type_counts: dict[str, int]
    parser_unsupported_node_count: int
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """转换为可写入 canonical JSON 的字典。

        Args:
            无参数；字段取自当前 OOXML 审计结果。

        Returns:
            不含正文和 URL 的审计字段。

        """
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _HeadingCounts:
    candidate: int
    accepted: int
    rejected: int
    reasons: Counter[str]


def heading_candidate(text: str, *, in_table: bool = False) -> HeadingDecision:
    """判断段落是否可安全映射为 Heading 1～3。

    Args:
        text: 仅用于本地判断且不会写入审计的段落可见文本。
        in_table: 段落是否位于表格单元格内。

    Returns:
        候选、接受状态、层级和稳定 reason code。

    """
    match = _HEADING_PATTERN.fullmatch(text)
    if match is None:
        return HeadingDecision(False, False, None, "NOT_NUMBERED_HEADING")
    if in_table:
        return HeadingDecision(True, False, None, "TABLE_PARAGRAPH")
    normalized = " ".join(text.split())
    if len(normalized) > _MAX_HEADING_CHARACTERS:
        return HeadingDecision(True, False, None, "HEADING_TOO_LONG")
    title = match.group(2).strip()
    if not title:
        return HeadingDecision(True, False, None, "EMPTY_HEADING_TITLE")
    if any(character in title for character in _SENTENCE_PUNCTUATION):
        return HeadingDecision(True, False, None, "SENTENCE_PUNCTUATION")
    level = min(3, match.group(1).count(".") + 1)
    return HeadingDecision(True, True, level, "NUMBERED_SHORT_HEADING")


def clean_docx(
    *,
    source: Path,
    destination: Path,
    canonical_name: str,
    source_date_epoch: int,
) -> OoxmlAudit:
    """清洗单个 DOCX 并以确定性 ZIP metadata 写入目标。

    Args:
        source: LibreOffice 转换后的临时 DOCX。
        destination: 尚不存在的清洗后 DOCX 路径。
        canonical_name: 用于限定 GM-01/03/04 专项规则的标准文件名。
        source_date_epoch: 写入 ZIP 成员的固定时间戳。

    Returns:
        不含正文和外部 URL 的结构审计。

    Raises:
        FileExistsError: 目标已存在。
        OoxmlPreparationError: DOCX 含危险结构或清洗改变了未授权内容。

    """
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("DOCX 清洗目标已存在，拒绝覆盖。")
    parts = _read_safe_archive(source)
    original_document = _required_xml(parts, "word/document.xml")
    original_visible = _visible_text(original_document)
    removed_relationships = _remove_external_relationships(parts)
    document = _required_xml(parts, "word/document.xml")
    removed_private = _remove_confirmed_private_character(
        document,
        allowed=canonical_name.startswith("GM-03 "),
    )
    heading_counts = _repair_heading_styles(parts, document)
    parts["word/document.xml"] = _serialize_xml(document)
    cleaned_visible = _visible_text(document)
    expected_visible = (
        original_visible.replace(_PRIVATE_CHARACTER, "")
        if canonical_name.startswith("GM-03 ")
        else original_visible
    )
    if cleaned_visible != expected_visible:
        raise OoxmlPreparationError("OOXML 清洗改变了未授权可见文本。")
    _write_deterministic_archive(
        destination,
        parts,
        source_date_epoch=source_date_epoch,
    )
    try:
        elements, parser_audit = DocxParser().parse_with_audit(
            destination,
            display_path=canonical_name,
        )
    except UnsafeDocxError as error:
        destination.unlink(missing_ok=True)
        raise OoxmlPreparationError(
            "清洗后 DOCX 未通过 Parser 审计。"
        ) from error
    if parser_audit.unsupported_content_with_evidence:
        destination.unlink(missing_ok=True)
        raise OoxmlPreparationError("DOCX 含 Parser 无法安全索引的证据结构。")
    remaining_private = _private_character_count(cleaned_visible)
    warnings: list[str] = []
    if canonical_name.startswith("GM-01 "):
        warnings.append("MANUAL_STRUCTURE_REVIEW_REQUIRED")
    if remaining_private:
        warnings.append("UNREVIEWED_PRIVATE_CHARACTERS")
    relationship_counter = Counter(item[0] for item in removed_relationships)
    kind_counter = Counter(element.kind for element in elements)
    return OoxmlAudit(
        visible_text_sha256=_text_sha256(cleaned_visible),
        paragraph_count=(
            kind_counter[ElementKind.PARAGRAPH]
            + kind_counter[ElementKind.HEADING]
        ),
        table_count=kind_counter[ElementKind.TABLE],
        image_count=kind_counter[ElementKind.IMAGE],
        heading_count=kind_counter[ElementKind.HEADING],
        list_level_count=sum(
            element.list_level is not None for element in elements
        ),
        ocr_candidate_count=kind_counter[ElementKind.IMAGE],
        heading_candidate_count=heading_counts.candidate,
        heading_accepted_count=heading_counts.accepted,
        heading_rejected_count=heading_counts.rejected,
        heading_reason_counts=dict(sorted(heading_counts.reasons.items())),
        removed_private_character_count=removed_private,
        remaining_private_character_count=remaining_private,
        removed_external_relationship_count=len(removed_relationships),
        external_relationship_type_counts=dict(sorted(relationship_counter.items())),
        parser_unsupported_node_count=parser_audit.unsupported_nodes,
        warnings=tuple(warnings),
    )


def _read_safe_archive(path: Path) -> dict[str, bytes]:
    if (
        not path.is_file()
        or path.is_symlink()
        or path.suffix.casefold() != ".docx"
    ):
        raise OoxmlPreparationError("转换输出必须是普通 DOCX 文件。")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            _validate_entries(entries)
            parts = {
                entry.filename: archive.read(entry)
                for entry in entries
                if not entry.is_dir()
            }
    except (OSError, zipfile.BadZipFile) as error:
        raise OoxmlPreparationError("转换输出不是有效 DOCX ZIP。") from error
    required = {"[Content_Types].xml", "word/document.xml"}
    if not required.issubset(parts):
        raise OoxmlPreparationError("DOCX 缺少必要 OOXML part。")
    _reject_dangerous_parts(parts)
    for name, payload in parts.items():
        if name.casefold().endswith(_XML_SUFFIXES):
            _parse_xml(payload, label=name)
    return parts


def _validate_entries(entries: list[zipfile.ZipInfo]) -> None:
    if not entries or len(entries) > _MAX_ZIP_ENTRIES:
        raise OoxmlPreparationError("DOCX ZIP 条目数无效。")
    total_bytes = 0
    seen: set[str] = set()
    for entry in entries:
        _validate_part_path(entry.filename)
        if entry.filename in seen:
            raise OoxmlPreparationError("DOCX ZIP 含重复条目。")
        seen.add(entry.filename)
        if entry.flag_bits & 0x1:
            raise OoxmlPreparationError("DOCX ZIP 含加密条目。")
        member_mode = entry.external_attr >> 16
        file_type = stat.S_IFMT(member_mode)
        if file_type and not (
            stat.S_ISREG(member_mode) or stat.S_ISDIR(member_mode)
        ):
            raise OoxmlPreparationError("DOCX ZIP 含 symlink 或特殊成员。")
        if entry.file_size > _MAX_ENTRY_BYTES:
            raise OoxmlPreparationError("DOCX ZIP 单条目过大。")
        total_bytes += entry.file_size
        if total_bytes > _MAX_TOTAL_BYTES:
            raise OoxmlPreparationError("DOCX ZIP 总解压量过大。")
        if entry.file_size and not entry.compress_size:
            raise OoxmlPreparationError("DOCX ZIP 压缩大小异常。")
        if (
            entry.compress_size
            and entry.file_size / entry.compress_size > _MAX_COMPRESSION_RATIO
        ):
            raise OoxmlPreparationError("DOCX ZIP 压缩比过大。")


def _validate_part_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
    ):
        raise OoxmlPreparationError("DOCX ZIP 含越界路径。")


def _reject_dangerous_parts(parts: dict[str, bytes]) -> None:
    names = {name.casefold() for name in parts}
    if names & _DISALLOWED_PART_NAMES or any(
        name.startswith(_DISALLOWED_PART_PREFIXES) for name in names
    ):
        raise OoxmlPreparationError("DOCX 含 macro、OLE、ActiveX 或嵌入包。")
    content_types = _parse_xml(
        parts["[Content_Types].xml"],
        label="[Content_Types].xml",
    )
    for item in content_types.findall(
        f".//{{{_CONTENT_TYPES_NAMESPACE}}}*"
    ):
        content_type = (item.get("ContentType") or "").casefold()
        if "macroenabled" in content_type or "vba" in content_type:
            raise OoxmlPreparationError("DOCX Content-Type 启用了宏。")
    for name, payload in parts.items():
        if not name.casefold().endswith(_XML_SUFFIXES):
            continue
        root = _parse_xml(payload, label=name)
        if any(
            etree.QName(node).localname.casefold() in _PACKAGE_OBJECT_NAMES
            for node in root.iter()
        ):
            raise OoxmlPreparationError("DOCX XML 含 OLE 或 package object。")


def _remove_external_relationships(
    parts: dict[str, bytes],
) -> list[tuple[str, str]]:
    removed: list[tuple[str, str]] = []
    relationship_parts = tuple(
        name for name in sorted(parts) if name.casefold().endswith(".rels")
    )
    for relationship_name in relationship_parts:
        root = _parse_xml(parts[relationship_name], label=relationship_name)
        removed_ids: set[str] = set()
        for relation in tuple(
            root.findall(f"{{{_PACKAGE_REL_NAMESPACE}}}Relationship")
        ):
            if (relation.get("TargetMode") or "").casefold() != "external":
                continue
            relationship_id = relation.get("Id")
            relationship_type = _relationship_type(relation.get("Type") or "")
            if relationship_id:
                removed_ids.add(relationship_id)
            removed.append((relationship_type, relationship_name))
            root.remove(relation)
        if not removed_ids:
            continue
        parts[relationship_name] = _serialize_xml(root)
        owner = _relationship_owner(relationship_name)
        if owner is not None and owner in parts:
            owner_root = _parse_xml(parts[owner], label=owner)
            _strip_removed_relationship_references(owner_root, removed_ids)
            parts[owner] = _serialize_xml(owner_root)
    return removed


def _relationship_type(value: str) -> str:
    normalized = value.rstrip("/")
    suffix = normalized.rsplit("/", maxsplit=1)[-1]
    return suffix.casefold() or "unknown"


def _relationship_owner(relationship_name: str) -> str | None:
    path = PurePosixPath(relationship_name)
    if path == PurePosixPath("_rels/.rels"):
        return None
    if (
        len(path.parts) < _MINIMUM_RELATIONSHIP_PARTS
        or path.parts[-2] != "_rels"
    ):
        return None
    owner_name = path.name.removesuffix(".rels")
    return PurePosixPath(*path.parts[:-2], owner_name).as_posix()


def _strip_removed_relationship_references(
    root: etree._Element,
    removed_ids: set[str],
) -> None:
    relationship_attributes = {
        f"{{{_REL_NAMESPACE}}}id",
        f"{{{_REL_NAMESPACE}}}embed",
        f"{{{_REL_NAMESPACE}}}link",
    }
    for node in root.iter():
        for attribute in relationship_attributes:
            if node.get(attribute) in removed_ids:
                del node.attrib[attribute]


def _remove_confirmed_private_character(
    document: etree._Element,
    *,
    allowed: bool,
) -> int:
    count = 0
    for node in document.findall(f".//{{{_WORD_NAMESPACE}}}t"):
        text = node.text or ""
        found = text.count(_PRIVATE_CHARACTER)
        if found and allowed:
            node.text = text.replace(_PRIVATE_CHARACTER, "")
            count += found
    return count


def _repair_heading_styles(
    parts: dict[str, bytes],
    document: etree._Element,
) -> _HeadingCounts:
    reasons: Counter[str] = Counter()
    accepted_levels: set[int] = set()
    candidate_count = 0
    accepted_count = 0
    rejected_count = 0
    for paragraph in document.findall(f".//{{{_WORD_NAMESPACE}}}p"):
        decision = heading_candidate(
            _paragraph_text(paragraph),
            in_table=any(
                etree.QName(parent).localname == "tc"
                for parent in paragraph.iterancestors()
            ),
        )
        if not decision.candidate:
            continue
        candidate_count += 1
        reasons[decision.reason] += 1
        if not decision.accepted or decision.level is None:
            rejected_count += 1
            continue
        accepted_count += 1
        accepted_levels.add(decision.level)
        _set_paragraph_heading(paragraph, decision.level)
    if accepted_levels:
        parts["word/styles.xml"] = _styles_with_industry_headings(
            parts.get("word/styles.xml"),
            accepted_levels,
        )
    return _HeadingCounts(
        candidate=candidate_count,
        accepted=accepted_count,
        rejected=rejected_count,
        reasons=reasons,
    )


def _set_paragraph_heading(paragraph: etree._Element, level: int) -> None:
    properties = paragraph.find(f"{{{_WORD_NAMESPACE}}}pPr")
    if properties is None:
        properties = etree.Element(f"{{{_WORD_NAMESPACE}}}pPr")
        paragraph.insert(0, properties)
    style = properties.find(f"{{{_WORD_NAMESPACE}}}pStyle")
    if style is None:
        style = etree.Element(f"{{{_WORD_NAMESPACE}}}pStyle")
        properties.insert(0, style)
    style.set(f"{{{_WORD_NAMESPACE}}}val", f"IndustryHeading{level}")


def _styles_with_industry_headings(
    payload: bytes | None,
    levels: set[int],
) -> bytes:
    if payload is None:
        root = etree.Element(
            f"{{{_WORD_NAMESPACE}}}styles",
            nsmap={"w": _WORD_NAMESPACE},
        )
    else:
        root = _parse_xml(payload, label="word/styles.xml")
    existing = {
        style.get(f"{{{_WORD_NAMESPACE}}}styleId")
        for style in root.findall(f"{{{_WORD_NAMESPACE}}}style")
    }
    for level in sorted(levels):
        style_id = f"IndustryHeading{level}"
        if style_id in existing:
            continue
        style = etree.SubElement(root, f"{{{_WORD_NAMESPACE}}}style")
        style.set(f"{{{_WORD_NAMESPACE}}}type", "paragraph")
        style.set(f"{{{_WORD_NAMESPACE}}}styleId", style_id)
        name = etree.SubElement(style, f"{{{_WORD_NAMESPACE}}}name")
        name.set(f"{{{_WORD_NAMESPACE}}}val", style_id)
        paragraph_properties = etree.SubElement(
            style,
            f"{{{_WORD_NAMESPACE}}}pPr",
        )
        outline = etree.SubElement(
            paragraph_properties,
            f"{{{_WORD_NAMESPACE}}}outlineLvl",
        )
        outline.set(f"{{{_WORD_NAMESPACE}}}val", str(level - 1))
    return _serialize_xml(root)


def _required_xml(parts: dict[str, bytes], name: str) -> etree._Element:
    try:
        payload = parts[name]
    except KeyError as error:
        raise OoxmlPreparationError(f"DOCX 缺少必要 part：{name}") from error
    return _parse_xml(payload, label=name)


def _parse_xml(payload: bytes, *, label: str) -> etree._Element:
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise OoxmlPreparationError(f"OOXML XML 禁止 DTD 或实体：{label}")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
    )
    try:
        return etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as error:
        raise OoxmlPreparationError(f"OOXML XML 无效：{label}") from error


def _serialize_xml(root: etree._Element) -> bytes:
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )


def _visible_text(document: etree._Element) -> str:
    pieces: list[str] = []
    for node in document.iter():
        local_name = etree.QName(node).localname
        if local_name == "t":
            pieces.append(node.text or "")
        elif local_name == "tab":
            pieces.append("\t")
        elif local_name in {"br", "cr"} or (
            local_name in {"p", "tr"} and pieces
        ):
            pieces.append("\n")
    return "".join(pieces)


def _paragraph_text(paragraph: etree._Element) -> str:
    return "".join(
        node.text or ""
        for node in paragraph.findall(f".//{{{_WORD_NAMESPACE}}}t")
    ).strip()


def _private_character_count(text: str) -> int:
    return sum(
        "\ue000" <= character <= "\uf8ff"
        for character in text
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _zip_timestamp(
    source_date_epoch: int,
) -> tuple[int, int, int, int, int, int]:
    minimum = int(datetime(1980, 1, 1, tzinfo=UTC).timestamp())
    timestamp = datetime.fromtimestamp(max(source_date_epoch, minimum), tz=UTC)
    second = timestamp.second - timestamp.second % 2
    return (
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        second,
    )


def _write_deterministic_archive(
    destination: Path,
    parts: dict[str, bytes],
    *,
    source_date_epoch: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    timestamp = _zip_timestamp(source_date_epoch)
    try:
        with zipfile.ZipFile(
            destination,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name in sorted(parts):
                info = zipfile.ZipInfo(name, date_time=timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                archive.writestr(info, parts[name])
    except Exception:
        destination.unlink(missing_ok=True)
        raise
