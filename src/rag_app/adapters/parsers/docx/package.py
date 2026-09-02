"""安全读取 OPC package 并建立 DOCX Part catalog。"""

from __future__ import annotations

import hashlib
import io
import time
import zipfile
from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from lxml import etree

from rag_app.adapters.parsers.docx.models import (
    PartCatalog,
    PartInfo,
    RelationshipInfo,
)
from rag_app.adapters.parsers.docx.namespaces import (
    PACKAGE_REL,
    qn,
)
from rag_app.adapters.parsers.docx.relationships import (
    archive_name,
    normalize_absolute_part,
    relationship_source,
    resolve_relationship_target,
    validate_archive_path,
)
from rag_app.core.errors import InvalidDocument
from rag_app.core.policies import ParsingPolicy

_OFFICE_DOCUMENT_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    "officeDocument"
)
_MAIN_DOCUMENT_CONTENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml",
    }
)
_FATAL_CONTENT_MARKERS = (
    "macroenabled",
    "vbaproject",
)


class DocxPackage:
    """通过安全边界校验的只读内存 OPC package。"""

    def __init__(
        self,
        content: bytes,
        policy: ParsingPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """校验归档并建立 catalog。

        Args:
            content: 受控 DOCX 字节。
            policy: 资源和安全策略。
            clock: 可注入单调时钟。

        Returns:
            无返回值。

        Raises:
            InvalidDocument: 归档、关系或资源边界无效。

        """
        if len(content) > policy.max_file_bytes:
            raise InvalidDocument(
                "DOCX 文件大小超过 ParsingPolicy 限制。",
                stage="docx-ooxml-v4.resource",
            )
        self._content = content
        self._policy = policy
        self._clock = clock
        self._started_at = clock()
        self._archive = self._open_archive(content)
        self._entries = self._validate_archive()
        content_types = self._read_content_types()
        relationships = self._read_all_relationships()
        main_part_uri = self._resolve_main_part(relationships, content_types)
        parts = self._build_parts(content_types)
        self.catalog = PartCatalog(
            main_part_uri=main_part_uri,
            parts=parts,
            relationships=relationships,
        )

    def close(self) -> None:
        """关闭内存 ZIP reader。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        self._archive.close()

    def __enter__(self) -> DocxPackage:
        """进入 package 读取作用域。"""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """离开读取作用域并关闭 ZIP。"""
        del exc_type, exc_value, traceback
        self.close()

    def read(self, part_uri: str) -> bytes:
        """读取一个已登记 Part。

        Args:
            part_uri: 以 `/` 开头的规范化 package URI。

        Returns:
            Part 原始字节。

        Raises:
            InvalidDocument: Part 不存在或解析超时。

        """
        self.check_timeout()
        name = archive_name(part_uri)
        if name not in self._entries:
            raise InvalidDocument(
                "DOCX 关系指向缺失的 package Part。",
                stage="docx-ooxml-v4.relationship",
            )
        try:
            return self._archive.read(name)
        except (KeyError, OSError, RuntimeError) as error:
            raise InvalidDocument(
                "DOCX package Part 无法读取。",
                stage="docx-ooxml-v4.package",
                details={"error_type": type(error).__name__},
            ) from None

    def xml(self, part_uri: str) -> etree._Element:
        """使用禁止 DTD、实体和网络的 parser 读取 XML Part。

        Args:
            part_uri: 已登记的 XML Part URI。

        Returns:
            仅在 adapter 内使用的 lxml 根元素。

        Raises:
            InvalidDocument: XML 语法、深度或节点数超限。

        """
        payload = self.read(part_uri)
        parser = etree.XMLParser(
            load_dtd=False,
            no_network=True,
            recover=False,
            resolve_entities=False,
            huge_tree=False,
        )
        try:
            root = etree.fromstring(payload, parser=parser)
        except (etree.XMLSyntaxError, ValueError):
            raise InvalidDocument(
                "DOCX package XML 结构无效。",
                stage="docx-ooxml-v4.xml",
            ) from None
        for node_count, node in enumerate(root.iter(), start=1):
            if node_count > self._policy.max_xml_nodes:
                raise InvalidDocument(
                    "DOCX XML 节点数超过限制。",
                    stage="docx-ooxml-v4.resource",
                )
            depth = len(tuple(node.iterancestors())) + 1
            if depth > self._policy.max_xml_depth:
                raise InvalidDocument(
                    "DOCX XML 深度超过限制。",
                    stage="docx-ooxml-v4.resource",
                )
        self.check_timeout()
        return root

    def relationship(
        self,
        source_part_uri: str,
        relationship_id: str,
    ) -> RelationshipInfo | None:
        """查找指定源 Part 的关系。

        Args:
            source_part_uri: 关系源 Part URI。
            relationship_id: OOXML 关系 ID。

        Returns:
            找到的关系，否则为 `None`。

        """
        return next(
            (
                relationship
                for relationship in self.catalog.relationships
                if relationship.source_part_uri == source_part_uri
                and relationship.relationship_id == relationship_id
            ),
            None,
        )

    def check_timeout(self) -> None:
        """在确定性检查点执行总耗时门禁。

        Args:
            无参数。

        Returns:
            无返回值。

        Raises:
            InvalidDocument: 总解析耗时超过策略上限。

        """
        if (
            self._clock() - self._started_at
            > self._policy.parse_timeout_seconds
        ):
            raise InvalidDocument(
                "DOCX 解析耗时超过限制。",
                stage="docx-ooxml-v4.resource",
            )

    @staticmethod
    def _open_archive(content: bytes) -> zipfile.ZipFile:
        try:
            return zipfile.ZipFile(io.BytesIO(content))
        except (zipfile.BadZipFile, OSError):
            raise InvalidDocument(
                "输入不是有效 DOCX ZIP package。",
                stage="docx-ooxml-v4.package",
            ) from None

    def _validate_archive(self) -> dict[str, zipfile.ZipInfo]:
        entries = self._archive.infolist()
        if len(entries) > self._policy.max_entries:
            raise InvalidDocument(
                "DOCX ZIP 条目数超过限制。",
                stage="docx-ooxml-v4.resource",
            )
        by_name: dict[str, zipfile.ZipInfo] = {}
        total_size = 0
        for entry in entries:
            validate_archive_path(entry.filename)
            if entry.filename in by_name:
                raise InvalidDocument(
                    "DOCX ZIP 存在重复条目。",
                    stage="docx-ooxml-v4.package",
                )
            by_name[entry.filename] = entry
            if entry.flag_bits & 0x1:
                raise InvalidDocument(
                    "DOCX 不接受加密 ZIP 条目。",
                    stage="docx-ooxml-v4.package",
                )
            if entry.file_size > self._policy.max_entry_bytes:
                raise InvalidDocument(
                    "DOCX ZIP 单条目解压量超过限制。",
                    stage="docx-ooxml-v4.resource",
                )
            total_size += entry.file_size
            if total_size > self._policy.max_uncompressed_bytes:
                raise InvalidDocument(
                    "DOCX ZIP 总解压量超过限制。",
                    stage="docx-ooxml-v4.resource",
                )
            if entry.file_size and entry.compress_size == 0:
                raise InvalidDocument(
                    "DOCX ZIP 条目压缩大小异常。",
                    stage="docx-ooxml-v4.package",
                )
            if entry.compress_size:
                ratio = entry.file_size / entry.compress_size
                if ratio > self._policy.max_compression_ratio:
                    raise InvalidDocument(
                        "DOCX ZIP 条目压缩比超过限制。",
                        stage="docx-ooxml-v4.resource",
                    )
        required = {"[Content_Types].xml", "_rels/.rels"}
        if not required.issubset(by_name):
            raise InvalidDocument(
                "DOCX 缺少 OPC content types 或根关系。",
                stage="docx-ooxml-v4.package",
            )
        return by_name

    def _read_content_types(self) -> dict[str, str]:
        root = self.xml("/[Content_Types].xml")
        defaults: dict[str, str] = {}
        overrides: dict[str, str] = {}
        for child in root:
            extension = child.get("Extension")
            part_name = child.get("PartName")
            content_type = child.get("ContentType")
            if not content_type:
                continue
            if any(
                marker in content_type.casefold()
                for marker in _FATAL_CONTENT_MARKERS
            ):
                raise InvalidDocument(
                    "DOCX 包含宏相关 content type。",
                    stage="docx-ooxml-v4.macro",
                )
            if extension:
                defaults[extension.casefold()] = content_type
            if part_name:
                overrides[normalize_absolute_part(part_name)] = content_type
        result: dict[str, str] = {}
        for name in self._entries:
            if name.endswith("/"):
                continue
            part_uri = f"/{name}"
            suffix = PurePosixPath(name).suffix.removeprefix(".").casefold()
            result[part_uri] = overrides.get(
                part_uri,
                defaults.get(suffix, "application/octet-stream"),
            )
        return result

    def _read_all_relationships(self) -> tuple[RelationshipInfo, ...]:
        relationships: list[RelationshipInfo] = []
        for name in sorted(self._entries):
            if not name.endswith(".rels"):
                continue
            source = relationship_source(name)
            root = self.xml(f"/{name}")
            seen: set[str] = set()
            for item in root.findall(qn(PACKAGE_REL, "Relationship")):
                relationship_id = item.get("Id") or ""
                relationship_type = item.get("Type") or ""
                target = item.get("Target") or ""
                target_mode = item.get("TargetMode") or "Internal"
                if not relationship_id or relationship_id in seen:
                    raise InvalidDocument(
                        "DOCX relationship ID 缺失或重复。",
                        stage="docx-ooxml-v4.relationship",
                    )
                seen.add(relationship_id)
                if any(
                    marker in relationship_type.casefold()
                    for marker in _FATAL_CONTENT_MARKERS
                ):
                    raise InvalidDocument(
                        "DOCX 包含宏相关 relationship。",
                        stage="docx-ooxml-v4.macro",
                    )
                if target_mode.casefold() == "external":
                    relationships.append(
                        RelationshipInfo(
                            source_part_uri=source,
                            relationship_id=relationship_id,
                            relationship_type=relationship_type,
                            target_mode="External",
                            target_part_uri=None,
                            external_scheme=(urlsplit(target).scheme or None),
                        )
                    )
                    continue
                target_part = resolve_relationship_target(source, target)
                if archive_name(target_part) not in self._entries:
                    raise InvalidDocument(
                        "DOCX relationship 指向缺失 Part。",
                        stage="docx-ooxml-v4.relationship",
                    )
                relationships.append(
                    RelationshipInfo(
                        source_part_uri=source,
                        relationship_id=relationship_id,
                        relationship_type=relationship_type,
                        target_mode="Internal",
                        target_part_uri=target_part,
                    )
                )
        return tuple(
            sorted(
                relationships,
                key=lambda item: (
                    item.source_part_uri,
                    item.relationship_id,
                ),
            )
        )

    def _resolve_main_part(
        self,
        relationships: tuple[RelationshipInfo, ...],
        content_types: dict[str, str],
    ) -> str:
        candidates = [
            relationship.target_part_uri
            for relationship in relationships
            if relationship.source_part_uri == "/"
            and relationship.relationship_type == _OFFICE_DOCUMENT_RELATIONSHIP
            and relationship.target_part_uri is not None
        ]
        if len(candidates) != 1:
            raise InvalidDocument(
                "DOCX 必须包含唯一主文档关系。",
                stage="docx-ooxml-v4.package",
            )
        main_part_uri = candidates[0]
        if content_types.get(main_part_uri) not in _MAIN_DOCUMENT_CONTENT_TYPES:
            raise InvalidDocument(
                "DOCX 主文档 content type 不匹配。",
                stage="docx-ooxml-v4.package",
            )
        return main_part_uri

    def _build_parts(
        self,
        content_types: dict[str, str],
    ) -> tuple[PartInfo, ...]:
        parts: list[PartInfo] = []
        for name, entry in sorted(self._entries.items()):
            if name.endswith("/"):
                continue
            payload = self._archive.read(name)
            parts.append(
                PartInfo(
                    part_uri=f"/{name}",
                    content_type=content_types[f"/{name}"],
                    size=entry.file_size,
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
        return tuple(parts)
