"""清洗 DOC 转换得到的通用 OOXML 制品。"""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import PurePosixPath

from lxml import etree

from rag_app.core.errors import InvalidDocument
from rag_app.core.policies import ParsingPolicy

_PACKAGE_REL_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_OFFICE_REL_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_DISALLOWED_DIRECTORY_NAMES = {"activex", "embeddings"}
_DISALLOWED_FILE_NAMES = {"vbadata.xml", "vbaproject.bin"}
_DISALLOWED_CONTENT_TYPE_MARKERS = (
    "activex",
    "oleobject",
    "vbaproject",
)
_OBJECT_ELEMENTS = {"oleobject", "object", "control"}
_XML_SUFFIXES = (".xml", ".rels")
_MINIMUM_RELATIONSHIP_PARTS = 3


def sanitize_derived_docx(
    content: bytes,
    policy: ParsingPolicy,
) -> tuple[bytes, int]:
    """拒绝可执行/嵌入对象并移除外部关系。

    Args:
        content: LibreOffice 生成的 OOXML ZIP。
        policy: 与后续 DOCX parser 相同的资源边界。

    Returns:
        固定 ZIP 元数据的清洗后 DOCX，以及移除的外部关系数量。

    Raises:
        InvalidDocument: 派生 ZIP、XML 或安全结构不满足合同。

    """
    parts = _read_parts(content, policy)
    _reject_dangerous_parts(parts)
    removed = _remove_external_relationships(parts)
    output = _write_parts(parts)
    if len(output) > policy.max_file_bytes:
        raise _invalid("派生 DOCX 清洗后超过文件大小上限。")
    return output, removed


def _read_parts(content: bytes, policy: ParsingPolicy) -> dict[str, bytes]:
    if len(content) > policy.max_file_bytes:
        raise _invalid("DOC 转换输出超过文件大小上限。")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            _validate_entries(entries, policy)
            parts = {
                entry.filename: archive.read(entry)
                for entry in entries
                if not entry.is_dir()
            }
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise _invalid("DOC 转换输出不是有效 DOCX ZIP。") from error
    if not {"[Content_Types].xml", "word/document.xml"}.issubset(parts):
        raise _invalid("DOC 转换输出缺少必要 OOXML part。")
    for name, payload in parts.items():
        if name.casefold().endswith(_XML_SUFFIXES):
            _parse_xml(payload)
    return parts


def _validate_entries(
    entries: list[zipfile.ZipInfo],
    policy: ParsingPolicy,
) -> None:
    if not entries or len(entries) > policy.max_entries:
        raise _invalid("派生 DOCX ZIP 条目数无效。")
    seen: set[str] = set()
    total = 0
    for entry in entries:
        _validate_part_path(entry.filename)
        normalized_name = entry.filename.casefold()
        if normalized_name in seen:
            raise _invalid("派生 DOCX ZIP 含重复条目。")
        seen.add(normalized_name)
        if entry.flag_bits & 0x1:
            raise _invalid("派生 DOCX ZIP 含加密条目。")
        member_mode = entry.external_attr >> 16
        file_type = stat.S_IFMT(member_mode)
        if file_type and not (
            stat.S_ISREG(member_mode) or stat.S_ISDIR(member_mode)
        ):
            raise _invalid("派生 DOCX ZIP 含 symlink 或特殊成员。")
        if entry.file_size > policy.max_entry_bytes:
            raise _invalid("派生 DOCX ZIP 单条目超过资源上限。")
        total += entry.file_size
        if total > policy.max_uncompressed_bytes:
            raise _invalid("派生 DOCX ZIP 总解压量超过资源上限。")
        if entry.file_size and not entry.compress_size:
            raise _invalid("派生 DOCX ZIP 压缩大小异常。")
        if (
            entry.compress_size
            and entry.file_size / entry.compress_size
            > policy.max_compression_ratio
        ):
            raise _invalid("派生 DOCX ZIP 压缩比超过资源上限。")


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
        raise _invalid("派生 DOCX ZIP 含越界路径。")


def _reject_dangerous_parts(parts: dict[str, bytes]) -> None:
    for name in parts:
        path = PurePosixPath(name.casefold())
        if (
            path.name in _DISALLOWED_FILE_NAMES
            or _DISALLOWED_DIRECTORY_NAMES.intersection(path.parts)
        ):
            raise _invalid("派生 DOCX 含 macro、OLE、ActiveX 或嵌入包。")
    content_types = _parse_xml(parts["[Content_Types].xml"])
    for item in content_types.findall(f".//{{{_CONTENT_TYPES_NAMESPACE}}}*"):
        content_type = (item.get("ContentType") or "").casefold()
        if (
            "macroenabled" in content_type
            or "vba" in content_type
            or any(
                marker in content_type
                for marker in _DISALLOWED_CONTENT_TYPE_MARKERS
            )
        ):
            raise _invalid("派生 DOCX Content-Type 含宏、OLE 或 ActiveX。")
    for name, payload in parts.items():
        if not name.casefold().endswith(_XML_SUFFIXES):
            continue
        root = _parse_xml(payload)
        if any(
            etree.QName(node).localname.casefold() in _OBJECT_ELEMENTS
            for node in root.iter()
        ):
            raise _invalid("派生 DOCX XML 含 OLE 或 ActiveX 对象。")


def _remove_external_relationships(parts: dict[str, bytes]) -> int:
    removed = 0
    for relationship_name in tuple(
        name for name in sorted(parts) if name.casefold().endswith(".rels")
    ):
        root = _parse_xml(parts[relationship_name])
        removed_ids: set[str] = set()
        for relationship in tuple(
            root.findall(f"{{{_PACKAGE_REL_NAMESPACE}}}Relationship")
        ):
            if (relationship.get("TargetMode") or "").casefold() != "external":
                continue
            relationship_id = relationship.get("Id")
            if relationship_id:
                removed_ids.add(relationship_id)
            root.remove(relationship)
            removed += 1
        if not removed_ids:
            continue
        parts[relationship_name] = _serialize_xml(root)
        owner_name = _relationship_owner(relationship_name)
        if owner_name is not None and owner_name in parts:
            owner = _parse_xml(parts[owner_name])
            _strip_relationship_references(owner, removed_ids)
            parts[owner_name] = _serialize_xml(owner)
    return removed


def _relationship_owner(name: str) -> str | None:
    path = PurePosixPath(name)
    if path == PurePosixPath("_rels/.rels"):
        return None
    if (
        len(path.parts) < _MINIMUM_RELATIONSHIP_PARTS
        or path.parts[-2] != "_rels"
    ):
        return None
    return PurePosixPath(
        *path.parts[:-2], path.name.removesuffix(".rels")
    ).as_posix()


def _strip_relationship_references(
    root: etree._Element,
    removed_ids: set[str],
) -> None:
    relationship_attributes = {
        f"{{{_OFFICE_REL_NAMESPACE}}}id",
        f"{{{_OFFICE_REL_NAMESPACE}}}embed",
        f"{{{_OFFICE_REL_NAMESPACE}}}link",
    }
    for node in root.iter():
        for attribute in relationship_attributes:
            if node.get(attribute) in removed_ids:
                del node.attrib[attribute]


def _parse_xml(content: bytes) -> etree._Element:
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise _invalid("派生 DOCX XML 禁止 DTD 或实体。")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
    )
    try:
        return etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as error:
        raise _invalid("派生 DOCX XML 无效。") from error


def _serialize_xml(root: etree._Element) -> bytes:
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )


def _write_parts(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, parts[name])
    return buffer.getvalue()


def _invalid(message: str) -> InvalidDocument:
    return InvalidDocument(message, stage="word-document-v2.derived_ooxml")


__all__ = ["sanitize_derived_docx"]
