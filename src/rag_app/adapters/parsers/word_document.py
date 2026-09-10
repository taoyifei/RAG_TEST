"""DOCX 与旧版二进制 DOC 的统一安全 Parser adapter。"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import os
import signal
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from rag_app.adapters.parsers.derived_docx import sanitize_derived_docx
from rag_app.adapters.parsers.doc_conversion import (
    DocConversion,
    DocConversionFailedError,
    DocConversionTimeoutError,
    DocConversionUnavailableError,
    DocConverter,
    SandboxedLibreOfficeConverter,
)
from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ParserCapabilities,
    ProviderMode,
)
from rag_app.core.errors import InvalidDocument
from rag_app.core.identifiers import document_version_id, node_id
from rag_app.core.models import (
    DocumentIR,
    DocumentNode,
    DocumentRelationship,
    DocumentSource,
    DocumentVersionRef,
    NodeKind,
    ParseContext,
    ParsedArtifact,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParseSource,
    SourceAnchor,
    StoryKind,
    text_payload,
)
from rag_app.core.policies import ParsingPolicy

_DOC_MEDIA_TYPE = "application/msword"
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_OCTET_STREAM_MEDIA_TYPE = "application/octet-stream"
_OLE_COMPOUND_FILE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ANTIWORD_EXECUTABLE = "/usr/bin/antiword"
_PRLIMIT_EXECUTABLE = "/usr/bin/prlimit"
_ANTIWORD_PACKAGE_VERSION = "0.37-17"
_MAX_PROCESS_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
_PART_URI = "/legacy-word/document.txt"
_CONVERTED_PART_PREFIX = "/converted-docx"
_OLE_FREE_SECTOR = 0xFFFFFFFF
_OLE_END_OF_CHAIN = 0xFFFFFFFE
_OLE_FAT_SECTOR = 0xFFFFFFFD
_OLE_DIFAT_SECTOR = 0xFFFFFFFC
_OLE_DIRECTORY_ENTRY_BYTES = 128
_OLE_HEADER_BYTES = 512
_OLE_HEADER_DIFAT_ENTRIES = 109
_OLE_DIRECTORY_STREAM_TYPE = 2
_OLE_BYTE_ORDER = 0xFFFE
_OLE_DIRECTORY_NAME_MIN_BYTES = 2
_OLE_DIRECTORY_NAME_MAX_BYTES = 64
_MAX_PART_URI_LENGTH = 512
_PROCESS_POLL_SECONDS = 0.05

DocTextExtractor = Callable[..., str]


class WordDocumentV1Parser:
    """按真实格式路由 DOCX 与旧二进制 DOC。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.PARSER,
        name="word-document-v1",
        version=(
            "2.0.1+docx-4.0.0."
            f"{SandboxedLibreOfficeConverter.recipe}."
            f"antiword-{_ANTIWORD_PACKAGE_VERSION}"
        ),
        mode=ProviderMode.LOCAL,
        capabilities=ComponentCapabilities(
            formats=(_DOCX_MEDIA_TYPE, _DOC_MEDIA_TYPE)
        ),
    )
    parser_capabilities = ParserCapabilities(
        supported_extensions=(".docx", ".doc"),
        supported_media_types=(_DOCX_MEDIA_TYPE, _DOC_MEDIA_TYPE),
        supports_tables="partial",
        supports_images="partial",
        supports_numbering="partial",
        supports_headers_footers="partial",
        supports_footnotes="partial",
        supports_revisions="partial",
        supports_comments="partial",
        supports_text_boxes="partial",
    )

    def __init__(
        self,
        *,
        docx_parser: DocxOoxmlV4Parser | None = None,
        doc_converter: DocConverter | None = None,
        doc_extractor: DocTextExtractor | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建统一 Word parser。

        Args:
            docx_parser: 可注入的现有 OOXML parser。
            doc_converter: 可注入的有界 DOC→DOCX converter。
            doc_extractor: 可注入的旧 DOC 文本提取器。
            clock: 解析报告使用的单调时钟。

        Returns:
            无返回值。

        """
        self._docx_parser = docx_parser or DocxOoxmlV4Parser(clock=clock)
        self._doc_converter = doc_converter or SandboxedLibreOfficeConverter(
            clock=clock
        )
        self._doc_extractor = doc_extractor or _extract_doc_text
        self._clock = clock

    def parse(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        """按扩展名、媒体类型和文件签名解析 Word 文档。

        Args:
            source: 受控文档字节与格式元数据。
            policy: 不可放宽资源边界的解析策略。
            context: 不进入策略指纹的逻辑文档身份。

        Returns:
            Document IR 与同一解析报告。

        Raises:
            InvalidDocument: 格式合同不匹配或解析失败。

        """
        extension = source.extension.casefold()
        if extension == ".docx":
            _require_media_type(
                source.media_type,
                allowed=frozenset({_DOCX_MEDIA_TYPE, _OCTET_STREAM_MEDIA_TYPE}),
            )
            return self._docx_parser.parse(source, policy, context)
        if extension == ".doc":
            _require_media_type(
                source.media_type,
                allowed=frozenset({_DOC_MEDIA_TYPE, _OCTET_STREAM_MEDIA_TYPE}),
            )
            return self._parse_doc(source, policy, context)
        raise InvalidDocument(
            "Word parser 仅接受 .doc 或 .docx 扩展名。",
            stage="word-document-v1.input",
        )

    def close(self) -> None:
        """关闭内部 parser 资源。

        Args:
            无参数。

        Returns:
            无返回值。

        """
        self._docx_parser.close()

    def _parse_doc(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        if len(source.content) > policy.max_file_bytes:
            raise InvalidDocument(
                "DOC 文件大小超过 ParsingPolicy 限制。",
                stage="word-document-v1.resource",
            )
        if not source.content.startswith(_OLE_COMPOUND_FILE_MAGIC):
            detail = _non_ole_doc_reason(source.content)
            raise InvalidDocument(
                detail,
                stage="word-document-v1.input",
            )
        _require_word_document_stream(source.content, policy)
        started_at = self._clock()
        try:
            converted = self._doc_converter.convert(
                source.content,
                policy,
                context.cancel_check,
            )
        except DocConversionUnavailableError:
            return self._parse_doc_fallback(
                source,
                policy,
                context,
                started_at=started_at,
            )
        except DocConversionTimeoutError as error:
            raise InvalidDocument(
                "DOC 转换超过时间上限。",
                stage="word-document-v1.timeout",
                retryable=True,
            ) from error
        except DocConversionFailedError as error:
            raise InvalidDocument(
                "DOC 转换失败或输出不满足安全合同。",
                stage="word-document-v1.convert",
            ) from error
        return self._parse_converted_doc(
            source,
            policy,
            context,
            converted,
            started_at=started_at,
        )

    def _parse_doc_fallback(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
        *,
        started_at: float,
    ) -> ParseResult:
        """仅在隔离转换器不可用时显式降级到 Antiword。"""
        extracted = _normalize_extracted_text(
            _invoke_doc_extractor(
                self._doc_extractor,
                source.content,
                policy,
                context.cancel_check,
            )
        )
        paragraphs = _paragraphs(extracted)
        if not paragraphs:
            raise InvalidDocument(
                "DOC 未提取到可索引文本。",
                stage="word-document-v1.content",
            )

        document = context.document
        content_sha256 = hashlib.sha256(source.content).hexdigest()
        version_id = document_version_id(
            document.document_id,
            content_sha256,
        )
        nodes = tuple(
            _paragraph_node(version_id, order, text)
            for order, text in enumerate(paragraphs)
        )
        issues = (
            ParseIssue(
                code="DOC_CONVERTER_UNAVAILABLE_FALLBACK",
                severity="warning",
                action="use_bounded_antiword_fallback",
                safe_message="隔离 DOC 转换器不可用，已显式降级到文本提取。",
            ),
            ParseIssue(
                code="LEGACY_DOC_FLATTENED_TEXT",
                severity="warning",
                action="flattened_to_paragraphs",
                safe_message=(
                    "旧版 DOC 以纯文本段落解析；表格、图片和复杂结构不保留。"
                ),
            ),
        )
        report = ParseReport(
            parser_id=self.descriptor.name,
            parser_version=self.descriptor.version,
            node_count=len(nodes),
            visible_text_nodes=len(nodes),
            represented_visible_text_nodes=len(nodes),
            part_count=1,
            story_counts=((StoryKind.BODY.value, len(nodes)),),
            issues=issues,
            elapsed_seconds=self._clock() - started_at,
            warnings=tuple(issue.code for issue in issues),
        )
        artifact_id = f"sha256:{content_sha256}"
        document_ir = DocumentIR(
            source=DocumentSource(
                document_id=document.document_id,
                document_version_id=version_id,
                display_name=source.display_name,
                media_type=_DOC_MEDIA_TYPE,
                extension=".doc",
                content_sha256=content_sha256,
                size_bytes=len(source.content),
                blob_ref=artifact_id,
            ),
            document=document.model_copy(
                update={"display_name": source.display_name}
            ),
            version=DocumentVersionRef(
                document_id=document.document_id,
                document_version_id=version_id,
                content_sha256=content_sha256,
            ),
            root_node_ids=tuple(node.node_id for node in nodes),
            nodes=nodes,
            parse_report=report,
            metadata=(
                ("converter", f"antiword-{_ANTIWORD_PACKAGE_VERSION}"),
                ("media_inventory", "unknown"),
                ("native_text", "antiword_flattened"),
                ("parsing_policy_id", policy.policy_id),
                ("relationship_status", "unavailable_after_flattening"),
                ("source_representation", "flattened-text"),
            ),
        )
        return ParseResult(
            document_ir=document_ir,
            report=report,
            artifacts=(
                ParsedArtifact(
                    artifact_id=artifact_id,
                    content_sha256=content_sha256,
                    media_type=_DOC_MEDIA_TYPE,
                    content=source.content,
                    role="source_document",
                ),
            ),
        )

    def _parse_converted_doc(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
        converted: DocConversion,
        *,
        started_at: float,
    ) -> ParseResult:
        """清洗派生 DOCX，并映射回原始 DOC 版本身份。"""
        cleaned, removed_external = sanitize_derived_docx(
            converted.content,
            policy,
        )
        parsed = self._docx_parser.parse(
            ParseSource(
                media_type=_DOCX_MEDIA_TYPE,
                display_name=_derived_display_name(source.display_name),
                content=cleaned,
                extension=".docx",
            ),
            policy,
            context,
        )
        return _map_converted_result(
            parsed,
            source=source,
            context=context,
            converted=converted,
            cleaned=cleaned,
            removed_external=removed_external,
            parser=self,
            elapsed_seconds=self._clock() - started_at,
        )


def _map_converted_result(  # noqa: PLR0913
    parsed: ParseResult,
    *,
    source: ParseSource,
    context: ParseContext,
    converted: DocConversion,
    cleaned: bytes,
    removed_external: int,
    parser: WordDocumentV1Parser,
    elapsed_seconds: float,
) -> ParseResult:
    original_sha256 = hashlib.sha256(source.content).hexdigest()
    version_id = document_version_id(
        context.document.document_id,
        original_sha256,
    )
    converted_anchors = {
        item.node_id: item.anchor.model_copy(
            update={
                "part_uri": _converted_part_uri(item.anchor.part_uri),
                "structural_path": (
                    "converted-docx",
                    *item.anchor.structural_path,
                ),
            }
        )
        for item in parsed.document_ir.nodes
    }
    node_ids = {
        item.node_id: node_id(
            version_id,
            converted_anchors[item.node_id].part_uri,
            converted_anchors[item.node_id].structural_path,
            item.kind.value,
            item.content_sha256,
        )
        for item in parsed.document_ir.nodes
    }
    nodes = tuple(
        item.model_copy(
            update={
                "node_id": node_ids[item.node_id],
                "parent_node_id": (
                    node_ids[item.parent_node_id]
                    if item.parent_node_id is not None
                    else None
                ),
                "child_ids": tuple(node_ids[value] for value in item.child_ids),
                "anchor": converted_anchors[item.node_id],
            }
        )
        for item in parsed.document_ir.nodes
    )
    relationships = tuple(
        _converted_relationship(item, node_ids)
        for item in parsed.document_ir.relationships
    )
    media_status = "partial" if parsed.report.media_count else "unknown"
    media_issue_code = (
        "DOC_MEDIA_INVENTORY_PARTIAL"
        if media_status == "partial"
        else "DOC_MEDIA_INVENTORY_UNKNOWN"
    )
    extra_issues = [
        ParseIssue(
            code="DOC_CONVERTED_TO_SANITIZED_DOCX",
            severity="info",
            action="parse_derived_ooxml",
            safe_message=(
                "旧版 DOC 已在受限进程中转换并按派生 OOXML 结构解析。"
            ),
        ),
        ParseIssue(
            code=media_issue_code,
            severity="warning",
            action="retain_converted_media_with_unverified_source_inventory",
            safe_message=(
                "已保留转换产物中的媒体实例；原始 DOC 媒体总数无法独立证明。"
            ),
        ),
    ]
    if removed_external:
        extra_issues.append(
            ParseIssue(
                code="DOC_EXTERNAL_RELATIONSHIPS_REMOVED",
                severity="warning",
                action="remove_external_relationships",
                count=removed_external,
                safe_message="派生 OOXML 的外部关系已移除且未访问目标。",
            )
        )
    warnings = (*parsed.report.warnings, media_issue_code)
    if removed_external:
        warnings = (*warnings, "DOC_EXTERNAL_RELATIONSHIPS_REMOVED")
    report = parsed.report.model_copy(
        update={
            "parser_id": parser.descriptor.name,
            "parser_version": parser.descriptor.version,
            "issues": (*parsed.report.issues, *extra_issues),
            "elapsed_seconds": elapsed_seconds,
            "warnings": warnings,
        }
    )
    derived_sha256 = hashlib.sha256(cleaned).hexdigest()
    derived_artifact_id = f"sha256:{derived_sha256}"
    source_artifact_id = f"sha256:{original_sha256}"
    metadata = dict(parsed.document_ir.metadata)
    metadata.update(
        {
            "conversion_recipe": (
                f"{converted.recipe}+derived-ooxml-sanitizer-v1"
            ),
            "converter": converted.converter,
            "converter_version": converted.converter_version,
            "derived_artifact_id": derived_artifact_id,
            "derived_content_sha256": derived_sha256,
            "mapping_quality": "converted_instance",
            "media_inventory": media_status,
            "media_instance_count": parsed.report.media_count,
            "native_text": (
                "converted"
                if parsed.report.visible_text_nodes
                else "none_detected"
            ),
            "parsing_policy_id": (
                dict(parsed.document_ir.metadata).get("parsing_policy_id")
                or "unknown"
            ),
            "relationship_status": (
                "external_removed"
                if removed_external
                else "derived_internal_only"
            ),
            "source_location_fidelity": "converted_instance",
            "source_representation": "sanitized-derived-docx",
        }
    )
    document_ir = DocumentIR(
        source=DocumentSource(
            document_id=context.document.document_id,
            document_version_id=version_id,
            display_name=source.display_name,
            media_type=_DOC_MEDIA_TYPE,
            extension=".doc",
            content_sha256=original_sha256,
            size_bytes=len(source.content),
            blob_ref=source_artifact_id,
        ),
        document=context.document.model_copy(
            update={"display_name": source.display_name}
        ),
        version=DocumentVersionRef(
            document_id=context.document.document_id,
            document_version_id=version_id,
            content_sha256=original_sha256,
        ),
        root_node_ids=tuple(
            node_ids[value] for value in parsed.document_ir.root_node_ids
        ),
        nodes=nodes,
        relationships=relationships,
        parse_report=report,
        metadata=tuple(sorted(metadata.items())),
    )
    artifacts = (
        ParsedArtifact(
            artifact_id=source_artifact_id,
            content_sha256=original_sha256,
            media_type=_DOC_MEDIA_TYPE,
            content=source.content,
            role="source_document",
        ),
        ParsedArtifact(
            artifact_id=derived_artifact_id,
            content_sha256=derived_sha256,
            media_type=_DOCX_MEDIA_TYPE,
            content=cleaned,
            role="derived_document",
        ),
        *(
            artifact
            for artifact in parsed.artifacts
            if artifact.role == "embedded_media"
        ),
    )
    return ParseResult(
        document_ir=document_ir,
        report=report,
        artifacts=artifacts,
    )


def _converted_relationship(
    relationship: DocumentRelationship,
    node_ids: dict[str, str],
) -> DocumentRelationship:
    return relationship.model_copy(
        update={
            "source_node_id": node_ids[relationship.source_node_id],
            "target_node_id": node_ids[relationship.target_node_id],
        }
    )


def _converted_part_uri(value: str) -> str:
    converted = f"{_CONVERTED_PART_PREFIX}{value}"
    if len(converted) <= _MAX_PART_URI_LENGTH:
        return converted
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{_CONVERTED_PART_PREFIX}/part-{digest}"


def _derived_display_name(value: str) -> str:
    path = Path(value)
    return f"{path.stem}.docx"


def _require_media_type(
    media_type: str,
    *,
    allowed: frozenset[str],
) -> None:
    if media_type.casefold() not in allowed:
        raise InvalidDocument(
            "Word 文档的扩展名与 Content-Type 不匹配。",
            stage="word-document-v1.input",
        )


def _non_ole_doc_reason(content: bytes) -> str:
    prefix = content[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if prefix.startswith(b"{\\rtf"):
        return "RTF 改扩展名不能作为二进制 DOC 入库。"
    if prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "HTML 改扩展名不能作为二进制 DOC 入库。"
    if content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "OOXML 文件必须使用 .docx 扩展名和匹配媒体类型。"
    return "DOC 文件签名无效。"


def _require_word_document_stream(
    content: bytes,
    policy: ParsingPolicy,
) -> None:
    names = _ole_directory_stream_names(content, policy)
    if "worddocument" not in names:
        raise InvalidDocument(
            "OLE compound file 不是受支持的 Word DOC。",
            stage="word-document-v1.input",
        )


def _ole_directory_stream_names(  # noqa: PLR0912, PLR0915
    content: bytes,
    policy: ParsingPolicy,
) -> frozenset[str]:
    try:
        if len(content) < _OLE_HEADER_BYTES:
            raise ValueError
        major_version = _unsigned_short(content, 26)
        byte_order = _unsigned_short(content, 28)
        sector_shift = _unsigned_short(content, 30)
        if byte_order != _OLE_BYTE_ORDER or (
            major_version,
            sector_shift,
        ) not in {(3, 9), (4, 12)}:
            raise ValueError
        sector_size = 1 << sector_shift
        # FAT 与目录只从完整 sector 读取；兼容 Office 实现可读取的
        # 非整 sector 尾段，原始尾段仍原样交给后续受限转换器。
        if len(content) < sector_size * 2:
            raise ValueError
        sector_count = len(content) // sector_size - 1
        traversal_limit = min(sector_count, policy.max_entries)

        def sector(sector_id: int) -> bytes:
            """读取已验证范围内的一个 CFB sector。

            Args:
                sector_id: 不含 512 字节 CFB header 的 sector 序号。

            Returns:
                对应 sector 的原始字节。

            """
            if sector_id < 0 or sector_id >= sector_count:
                raise ValueError
            start = (sector_id + 1) * sector_size
            return content[start : start + sector_size]

        fat_count = _unsigned_long(content, 44)
        if fat_count <= 0 or fat_count > traversal_limit:
            raise ValueError
        fat_sector_ids = [
            value
            for value in struct.unpack_from(
                f"<{_OLE_HEADER_DIFAT_ENTRIES}I", content, 76
            )
            if value != _OLE_FREE_SECTOR
        ]
        difat_sector_id = _unsigned_long(content, 68)
        difat_count = _unsigned_long(content, 72)
        if difat_count > traversal_limit:
            raise ValueError
        seen_difat: set[int] = set()
        values_per_difat = sector_size // 4 - 1
        for _ in range(difat_count):
            if difat_sector_id in seen_difat or difat_sector_id in {
                _OLE_FREE_SECTOR,
                _OLE_END_OF_CHAIN,
                _OLE_FAT_SECTOR,
                _OLE_DIFAT_SECTOR,
            }:
                raise ValueError
            seen_difat.add(difat_sector_id)
            payload = sector(difat_sector_id)
            values = struct.unpack_from(f"<{values_per_difat + 1}I", payload, 0)
            fat_sector_ids.extend(
                value for value in values[:-1] if value != _OLE_FREE_SECTOR
            )
            difat_sector_id = values[-1]
        if len(fat_sector_ids) < fat_count:
            raise ValueError
        fat_entries: list[int] = []
        for sector_id in fat_sector_ids[:fat_count]:
            if sector_id in {_OLE_FREE_SECTOR, _OLE_END_OF_CHAIN}:
                raise ValueError
            fat_entries.extend(
                struct.unpack_from(f"<{sector_size // 4}I", sector(sector_id))
            )

        directory_sector_id = _unsigned_long(content, 48)
        directory = bytearray()
        seen_directory: set[int] = set()
        while directory_sector_id != _OLE_END_OF_CHAIN:
            if (
                directory_sector_id in seen_directory
                or len(seen_directory) >= traversal_limit
                or directory_sector_id >= len(fat_entries)
                or directory_sector_id
                in {_OLE_FREE_SECTOR, _OLE_FAT_SECTOR, _OLE_DIFAT_SECTOR}
            ):
                raise ValueError
            seen_directory.add(directory_sector_id)
            directory.extend(sector(directory_sector_id))
            if len(directory) > policy.max_uncompressed_bytes:
                raise ValueError
            directory_sector_id = fat_entries[directory_sector_id]
        names: set[str] = set()
        for offset in range(0, len(directory), _OLE_DIRECTORY_ENTRY_BYTES):
            entry = directory[offset : offset + _OLE_DIRECTORY_ENTRY_BYTES]
            if len(entry) != _OLE_DIRECTORY_ENTRY_BYTES:
                raise ValueError
            name_bytes = _unsigned_short(entry, 64)
            object_type = entry[66]
            if object_type != _OLE_DIRECTORY_STREAM_TYPE:
                continue
            if (
                name_bytes < _OLE_DIRECTORY_NAME_MIN_BYTES
                or name_bytes > _OLE_DIRECTORY_NAME_MAX_BYTES
                or name_bytes % 2
            ):
                raise ValueError
            name = bytes(
                entry[: name_bytes - _OLE_DIRECTORY_NAME_MIN_BYTES]
            ).decode("utf-16le")
            names.add(name.casefold())
        return frozenset(names)
    except (IndexError, struct.error, UnicodeDecodeError, ValueError) as error:
        raise InvalidDocument(
            "DOC OLE 目录结构无效。",
            stage="word-document-v1.input",
        ) from error


def _unsigned_short(content: bytes | bytearray, offset: int) -> int:
    return int(struct.unpack_from("<H", content, offset)[0])


def _unsigned_long(content: bytes | bytearray, offset: int) -> int:
    return int(struct.unpack_from("<I", content, offset)[0])


def _invoke_doc_extractor(
    extractor: DocTextExtractor,
    content: bytes,
    policy: ParsingPolicy,
    cancel_check: Callable[[], None] | None,
) -> str:
    try:
        signature = inspect.signature(extractor)
    except (TypeError, ValueError):
        return extractor(content, policy, cancel_check)
    try:
        signature.bind(content, policy, cancel_check)
    except TypeError:
        return extractor(content, policy)
    return extractor(content, policy, cancel_check)


def _extract_doc_text(
    content: bytes,
    policy: ParsingPolicy,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    executable = Path(_ANTIWORD_EXECUTABLE)
    limiter = Path(_PRLIMIT_EXECUTABLE)
    if not executable.is_file() or not limiter.is_file():
        raise InvalidDocument(
            "DOC 解析组件不可用。",
            stage="word-document-v1.runtime",
        )
    output_limit = min(
        policy.max_uncompressed_bytes,
        policy.max_entry_bytes,
    )
    with tempfile.TemporaryDirectory(prefix="rag-doc-") as temporary:
        temporary_path = Path(temporary)
        source_path = temporary_path / "source.doc"
        output_path = temporary_path / "extracted.txt"
        source_path.write_bytes(content)
        source_path.chmod(0o400)
        command = (
            str(limiter),
            f"--fsize={output_limit}:{output_limit}",
            (
                "--as="
                f"{_MAX_PROCESS_ADDRESS_SPACE_BYTES}:"
                f"{_MAX_PROCESS_ADDRESS_SPACE_BYTES}"
            ),
            "--",
            str(executable),
            "-m",
            "UTF-8.txt",
            "-w",
            "0",
            str(source_path),
        )
        try:
            with output_path.open("wb") as output:
                process = subprocess.Popen(  # noqa: S603
                    command,
                    cwd=temporary_path,
                    env={
                        "HOME": str(temporary_path),
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "PATH": "/usr/bin:/bin",
                        "TMPDIR": str(temporary_path),
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                deadline = time.monotonic() + policy.parse_timeout_seconds
                while True:
                    _check_job_cancelled(cancel_check, process)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _kill_process(process)
                        raise InvalidDocument(
                            "DOC 解析超过时间上限。",
                            stage="word-document-v1.timeout",
                        )
                    try:
                        return_code = process.wait(
                            timeout=min(_PROCESS_POLL_SECONDS, remaining)
                        )
                        break
                    except subprocess.TimeoutExpired:
                        continue
        except OSError as error:
            raise InvalidDocument(
                "DOC 解析组件无法启动。",
                stage="word-document-v1.runtime",
            ) from error
        if return_code != 0:
            raise InvalidDocument(
                "DOC 解析失败或输出超过资源上限。",
                stage="word-document-v1.convert",
            )
        if output_path.stat().st_size > output_limit:
            raise InvalidDocument(
                "DOC 解析输出超过资源上限。",
                stage="word-document-v1.resource",
            )
        try:
            return output_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise InvalidDocument(
                "DOC 解析输出不是有效 UTF-8。",
                stage="word-document-v1.convert",
            ) from error


def _check_job_cancelled(
    cancel_check: Callable[[], None] | None,
    process: subprocess.Popen[bytes],
) -> None:
    if cancel_check is None:
        return
    try:
        cancel_check()
    except Exception:
        _kill_process(process)
        raise


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)


def _normalize_extracted_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in normalized:
        raise InvalidDocument(
            "DOC 解析输出包含无效控制字符。",
            stage="word-document-v1.convert",
        )
    return normalized


def _paragraphs(value: str) -> tuple[str, ...]:
    paragraphs: list[str] = []
    for raw_line in value.splitlines(keepends=True):
        line = raw_line.rstrip("\n\f\v")
        text = line.strip()
        if text:
            paragraphs.append(text)
    return tuple(paragraphs)


def _paragraph_node(
    version_id: str,
    order: int,
    text: str,
) -> DocumentNode:
    payload = text_payload(text)
    structural_path = ("body", f"paragraph:{order}")
    return DocumentNode(
        node_id=node_id(
            version_id,
            _PART_URI,
            structural_path,
            NodeKind.PARAGRAPH.value,
            payload.semantic_sha256,
        ),
        kind=NodeKind.PARAGRAPH,
        order=order,
        anchor=SourceAnchor(
            part_uri=_PART_URI,
            story_kind=StoryKind.BODY,
            structural_path=structural_path,
            ordinal=order,
            paragraph_index=order,
            source_start_char=0,
            source_end_char=len(text),
        ),
        text_payload=payload,
    )


__all__ = ["WordDocumentV1Parser"]
