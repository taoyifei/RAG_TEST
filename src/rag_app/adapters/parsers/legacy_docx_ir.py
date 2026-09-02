"""现有安全 DOCX Parser 到格式中立 Document IR 的 adapter。"""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path

from rag_app.adapters.parsers.common import (
    DocxPackageAudit,
    inspect_docx_package,
    normalize_document_text,
)
from rag_app.contracts import Element, ElementKind
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ParserCapabilities,
    ProviderMode,
)
from rag_app.core.errors import ConfigurationError, InvalidDocument
from rag_app.core.identifiers import deterministic_id, node_id
from rag_app.core.models import (
    DocumentIR,
    DocumentNode,
    DocumentRef,
    DocumentSource,
    DocumentVersionRef,
    ImageAttributes,
    ListAttributes,
    NodeKind,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParseSource,
    SourceAnchor,
    StoryKind,
    text_payload,
)
from rag_app.core.policies import (
    CommentsPolicy,
    ExternalRelationshipsPolicy,
    HiddenTextPolicy,
    ImagesPolicy,
    ParsingMode,
    ParsingPolicy,
    StoryPolicy,
    UnknownIndexableContentPolicy,
)
from rag_app.core.ports.blob_store import BlobStorePort, BlobWriteRequest
from rag_app.parsers.docx import (
    DocxParseAudit,
    DocxParser,
    DocxParserLimits,
    UnsafeDocxError,
)

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document"
)


class LegacyDocxIrParser:
    """复用旧安全 Parser，并把格式损失变成显式 ParseIssue。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.PARSER,
        name="legacy-docx-ir",
        version=f"{DocxParser.version}+ir-v1",
        mode=ProviderMode.LEGACY,
        capabilities=ComponentCapabilities(formats=(_DOCX_MEDIA_TYPE,)),
    )
    parser_capabilities = ParserCapabilities(
        supported_extensions=(".docx",),
        supported_media_types=(_DOCX_MEDIA_TYPE,),
        supports_tables="partial",
        supports_images="partial",
        supports_numbering="partial",
        supports_headers_footers=False,
        supports_footnotes=False,
        supports_revisions=False,
        supports_comments=False,
        supports_text_boxes=False,
    )

    def __init__(self, blob_store: BlobStorePort | None = None) -> None:
        """创建 adapter 和可选的宿主 BlobStore。

        Args:
            blob_store: 保存源文档与旧图片二进制的受控 Store。

        Returns:
            无返回值。

        """
        self._owns_blob_store = blob_store is None
        if blob_store is None:
            from rag_app.adapters.legacy.stores import (  # noqa: PLC0415
                InMemoryBlobStore,
            )

            self._blob_store: BlobStorePort = InMemoryBlobStore()
        else:
            self._blob_store = blob_store

    def parse(self, source: ParseSource, policy: ParsingPolicy) -> ParseResult:
        """解析受控 DOCX 并产出不含 bytes/Path/lxml 的 IR。

        Args:
            source: DOCX 字节、显示名和声明格式。
            policy: 资源边界、结构策略和稳定逻辑身份。

        Returns:
            Document IR 与同一 ParseReport。

        Raises:
            ConfigurationError: 未提供 project/kb/document 逻辑身份。
            InvalidDocument: 文件、策略或 Blob 写入不满足安全边界。

        """
        identity = _policy_identity(policy)
        if len(source.content) > policy.max_file_bytes:
            raise InvalidDocument(
                "DOCX 文件大小超过 ParsingPolicy 限制。",
                stage="legacy-docx-ir.resource",
            )
        started_at = time.monotonic()
        elements, legacy_audit = self._parse_legacy(source, policy)
        package_audit = inspect_docx_package(source.content)
        issues = list(_package_policy_issues(package_audit, policy))
        if policy.images is ImagesPolicy.REJECT and any(
            element.kind is ElementKind.IMAGE for element in elements
        ):
            raise InvalidDocument(
                "ParsingPolicy 拒绝文档图片。",
                stage="legacy-docx-ir.policy",
            )

        content_sha256 = hashlib.sha256(source.content).hexdigest()
        version_id = deterministic_id("dver", content_sha256)
        document_id = identity["document_id"]
        document_blob_id = f"document:{version_id}"
        nodes, conversion_issues, image_writes = _convert_elements(
            elements,
            document_version_id=version_id,
        )
        issues.extend(conversion_issues)
        issues.extend(_legacy_audit_issues(legacy_audit))
        report = _parse_report(
            nodes,
            elements,
            issues,
            (legacy_audit, package_audit),
            elapsed_seconds=time.monotonic() - started_at,
        )
        document_ir = DocumentIR(
            source=DocumentSource(
                document_id=document_id,
                document_version_id=version_id,
                display_name=source.display_name,
                media_type=_DOCX_MEDIA_TYPE,
                extension=".docx",
                content_sha256=content_sha256,
                size_bytes=len(source.content),
                blob_ref=document_blob_id,
                metadata=(("declared_extension", source.extension),),
            ),
            document=DocumentRef(
                project_id=identity["project_id"],
                knowledge_base_id=identity["knowledge_base_id"],
                document_id=document_id,
                display_name=source.display_name,
            ),
            version=DocumentVersionRef(
                document_id=document_id,
                document_version_id=version_id,
                content_sha256=content_sha256,
            ),
            root_node_ids=tuple(
                node.node_id for node in nodes if node.parent_node_id is None
            ),
            nodes=nodes,
            parse_report=report,
        )
        writes = (
            BlobWriteRequest(
                blob_id=document_blob_id,
                content_sha256=content_sha256,
                media_type=_DOCX_MEDIA_TYPE,
                content=source.content,
            ),
            *image_writes,
        )
        self._commit_blobs(writes)
        return ParseResult(document_ir=document_ir, report=report)

    def close(self) -> None:
        """释放 adapter 自己创建的 BlobStore。

        Args:
            无参数；宿主注入的 Store 生命周期仍归宿主。

        Returns:
            无返回值。

        """
        if self._owns_blob_store:
            self._blob_store.close()

    def _parse_legacy(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
    ) -> tuple[list[Element], DocxParseAudit]:
        limits = DocxParserLimits(
            max_file_bytes=policy.max_file_bytes,
            max_uncompressed_bytes=policy.max_uncompressed_bytes,
            max_entry_bytes=policy.max_entry_bytes,
            max_entries=policy.max_entries,
            max_compression_ratio=policy.max_compression_ratio,
            timeout_seconds=policy.parse_timeout_seconds,
        )
        parser = DocxParser(
            limits,
            allow_unsupported_indexable_content=(
                policy.mode is ParsingMode.BEST_EFFORT
                and policy.unknown_indexable_content
                is UnknownIndexableContentPolicy.ISSUE
            ),
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="rag-p03-docx-"
            ) as directory:
                path = Path(directory) / "materialized.docx"
                path.write_bytes(source.content)
                return parser.parse_with_audit(
                    path,
                    display_path=source.display_name,
                )
        except UnsafeDocxError as error:
            raise InvalidDocument(
                "DOCX 未通过现有安全解析边界。",
                stage="legacy-docx-ir.parse",
                details={"error_type": type(error).__name__},
            ) from None

    def _commit_blobs(self, writes: tuple[BlobWriteRequest, ...]) -> None:
        committed: list[str] = []
        try:
            for request in writes:
                self._blob_store.put(request)
                committed.append(request.blob_id)
        except Exception as error:
            for blob_id in reversed(committed):
                self._blob_store.delete(blob_id)
            raise InvalidDocument(
                "Document IR Blob 写入失败并已清理。",
                stage="legacy-docx-ir.blob",
                details={"error_type": type(error).__name__},
            ) from None


def _policy_identity(policy: ParsingPolicy) -> dict[str, str]:
    metadata = dict(policy.metadata)
    required = ("project_id", "knowledge_base_id", "document_id")
    if any(not isinstance(metadata.get(key), str) for key in required):
        raise ConfigurationError(
            "ParsingPolicy 必须显式提供 project/kb/document 逻辑 ID。",
            stage="legacy-docx-ir.identity",
        )
    return {key: str(metadata[key]) for key in required}


def _package_policy_issues(
    audit: DocxPackageAudit,
    policy: ParsingPolicy,
) -> tuple[ParseIssue, ...]:
    _validate_package_policy(audit, policy)
    issues: list[ParseIssue] = []
    if audit.external_relationships:
        issues.append(
            _issue(
                "DOCX_EXTERNAL_RELATIONSHIP_SKIPPED",
                "metadata_only",
                audit.external_relationships,
                "外部关系仅记录计数，未访问目标。",
            )
        )
    if audit.hidden_text_markers:
        issues.append(
            _issue(
                "DOCX_HIDDEN_TEXT_INCLUDED",
                "included",
                audit.hidden_text_markers,
                "策略显式要求保留隐藏文字。",
            )
        )
    if audit.comments_parts:
        issues.append(
            _issue(
                "DOCX_COMMENT_METADATA_ONLY",
                "metadata_only",
                audit.comments_parts,
                "批注仅记录 part 计数，P03 不解析正文。",
            )
        )
    if (
        audit.header_footer_parts
        and policy.headers_footers is not StoryPolicy.EXCLUDE
    ):
        issues.append(
            _issue(
                "DOCX_HEADER_FOOTER_METADATA_ONLY",
                "metadata_only",
                audit.header_footer_parts,
                "页眉页脚仅记录 part 计数，P03 不解析正文。",
            )
        )
    if (
        audit.footnote_endnote_parts
        and policy.footnotes_endnotes is not StoryPolicy.EXCLUDE
    ):
        issues.append(
            _issue(
                "DOCX_NOTE_METADATA_ONLY",
                "metadata_only",
                audit.footnote_endnote_parts,
                "脚注尾注仅记录 part 计数，P03 不解析正文。",
            )
        )
    if audit.unsupported_media_relationships:
        issues.append(
            _issue(
                "DOCX_IMAGE_UNSUPPORTED_MEDIA",
                "metadata_only",
                audit.unsupported_media_relationships,
                "不支持的图片媒体未写入 BlobStore。",
            )
        )
    return tuple(issues)


def _validate_package_policy(
    audit: DocxPackageAudit,
    policy: ParsingPolicy,
) -> None:
    if (
        audit.external_relationships
        and policy.external_relationships
        is ExternalRelationshipsPolicy.REJECT
    ):
        raise InvalidDocument(
            "ParsingPolicy 拒绝 DOCX 外部关系。",
            stage="legacy-docx-ir.policy",
        )
    if audit.revision_insertions or audit.revision_deletions:
        raise InvalidDocument(
            "legacy-docx-ir 不声明支持修订视图；P04 前拒绝该输入。",
            stage="legacy-docx-ir.capability",
            details={"policy": policy.tracked_changes.value},
        )
    if (
        audit.hidden_text_markers
        and policy.hidden_text is not HiddenTextPolicy.INCLUDE
    ):
        raise InvalidDocument(
            "legacy-docx-ir 无法安全排除隐藏文字。",
            stage="legacy-docx-ir.capability",
        )
    if audit.comments_parts and policy.comments is CommentsPolicy.REJECT:
        raise InvalidDocument(
            "ParsingPolicy 拒绝批注。",
            stage="legacy-docx-ir.policy",
        )
    if audit.comments_parts and policy.comments is CommentsPolicy.INCLUDE:
        raise InvalidDocument(
            "legacy-docx-ir 不支持包含批注正文。",
            stage="legacy-docx-ir.capability",
        )
    if (
        audit.header_footer_parts
        and policy.headers_footers is StoryPolicy.PARSE
    ):
        raise InvalidDocument(
            "legacy-docx-ir 不支持解析页眉页脚正文。",
            stage="legacy-docx-ir.capability",
        )
    if (
        audit.footnote_endnote_parts
        and policy.footnotes_endnotes is StoryPolicy.PARSE
    ):
        raise InvalidDocument(
            "legacy-docx-ir 不支持解析脚注尾注正文。",
            stage="legacy-docx-ir.capability",
        )


def _convert_elements(
    elements: list[Element],
    *,
    document_version_id: str,
) -> tuple[
    tuple[DocumentNode, ...],
    tuple[ParseIssue, ...],
    tuple[BlobWriteRequest, ...],
]:
    nodes: list[DocumentNode] = []
    image_writes: list[BlobWriteRequest] = []
    table_count = 0
    for root_order, element in enumerate(elements):
        anchor = _anchor(element, root_order)
        if element.kind is ElementKind.TABLE:
            table_count += 1
            table_node, representation = _table_nodes(
                element,
                anchor,
                root_order,
                document_version_id,
            )
            nodes.extend((table_node, representation))
            continue
        if element.kind is ElementKind.IMAGE:
            image_node, image_write = _image_node(
                element,
                anchor,
                root_order,
                document_version_id,
            )
            nodes.append(image_node)
            image_writes.append(image_write)
            continue
        exact_text = normalize_document_text(element.text)
        kind = (
            NodeKind.HEADING
            if element.kind is ElementKind.HEADING
            else NodeKind.LIST_ITEM
            if element.list_level is not None
            else NodeKind.PARAGRAPH
        )
        payload = text_payload(exact_text)
        nodes.append(
            DocumentNode(
                node_id=node_id(
                    document_version_id,
                    anchor.part_uri,
                    anchor.structural_path,
                    kind.value,
                    payload.semantic_sha256,
                ),
                kind=kind,
                order=root_order,
                anchor=anchor,
                text_payload=payload,
                list_attributes=(
                    ListAttributes(level=element.list_level)
                    if element.list_level is not None
                    else None
                ),
                metadata=(
                    ("legacy_element_id", element.element_id),
                    ("legacy_heading_index", element.locator.heading_index),
                ),
            )
        )
    issues = (
        (
            _issue(
                "LEGACY_TABLE_STRUCTURE_LOSS",
                "flattened_representation",
                table_count,
                "旧 Parser 只提供扁平表格文本，没有 cell provenance。",
            ),
        )
        if table_count
        else ()
    )
    return tuple(nodes), issues, tuple(image_writes)


def _table_nodes(
    element: Element,
    anchor: SourceAnchor,
    root_order: int,
    document_version_id: str,
) -> tuple[DocumentNode, DocumentNode]:
    empty_hash = hashlib.sha256(b"").hexdigest()
    table_id = node_id(
        document_version_id,
        anchor.part_uri,
        anchor.structural_path,
        NodeKind.TABLE.value,
        empty_hash,
    )
    representation_anchor = anchor.model_copy(
        update={
            "structural_path": (*anchor.structural_path, "representation:0"),
        }
    )
    payload = text_payload(normalize_document_text(element.text))
    representation_id = node_id(
        document_version_id,
        representation_anchor.part_uri,
        representation_anchor.structural_path,
        NodeKind.TABLE_REPRESENTATION.value,
        payload.semantic_sha256,
    )
    table = DocumentNode(
        node_id=table_id,
        kind=NodeKind.TABLE,
        child_ids=(representation_id,),
        order=root_order,
        anchor=anchor,
        metadata=(
            ("legacy_element_id", element.element_id),
            ("legacy_flattened_table", True),
            ("legacy_heading_index", element.locator.heading_index),
        ),
    )
    representation = DocumentNode(
        node_id=representation_id,
        kind=NodeKind.TABLE_REPRESENTATION,
        parent_node_id=table_id,
        order=0,
        anchor=representation_anchor,
        text_payload=payload,
        metadata=(
            ("legacy_flattened_table", True),
            ("legacy_heading_index", element.locator.heading_index),
        ),
    )
    return table, representation


def _image_node(
    element: Element,
    anchor: SourceAnchor,
    root_order: int,
    document_version_id: str,
) -> tuple[DocumentNode, BlobWriteRequest]:
    if (
        element.binary_data is None
        or element.media_type is None
        or element.media_name is None
    ):
        raise InvalidDocument(
            "旧图片 Element 缺少受控二进制或媒体元数据。",
            stage="legacy-docx-ir.image",
        )
    blob_id = f"image:{element.content_sha256}"
    attributes = ImageAttributes(
        blob_ref=blob_id,
        media_type=element.media_type,
        content_sha256=element.content_sha256,
        display_name=element.media_name,
    )
    generated_id = node_id(
        document_version_id,
        anchor.part_uri,
        anchor.structural_path,
        NodeKind.IMAGE.value,
        element.content_sha256,
    )
    return (
        DocumentNode(
            node_id=generated_id,
            kind=NodeKind.IMAGE,
            order=root_order,
            anchor=anchor,
            image_attributes=attributes,
            metadata=(
                ("legacy_element_id", element.element_id),
                ("legacy_heading_index", element.locator.heading_index),
            ),
        ),
        BlobWriteRequest(
            blob_id=blob_id,
            content_sha256=element.content_sha256,
            media_type=element.media_type,
            content=element.binary_data,
        ),
    )


def _anchor(element: Element, ordinal: int) -> SourceAnchor:
    locator = element.locator
    if element.kind is ElementKind.HEADING:
        segment = f"heading:{locator.heading_index or ordinal + 1}"
    elif element.kind is ElementKind.TABLE:
        segment = f"tbl:{locator.table_index or ordinal + 1}"
    elif element.kind is ElementKind.IMAGE:
        segment = f"image:{locator.image_index or ordinal + 1}"
    else:
        segment = f"p:{locator.paragraph_index or ordinal + 1}"
    return SourceAnchor(
        part_uri="/word/document.xml",
        story_kind=StoryKind.BODY,
        structural_path=("body", segment),
        ordinal=ordinal,
        paragraph_index=locator.paragraph_index,
        table_index=locator.table_index,
        relationship_id=None,
        source_start_char=0 if element.text else None,
        source_end_char=len(element.text) if element.text else None,
    )


def _legacy_audit_issues(audit: DocxParseAudit) -> tuple[ParseIssue, ...]:
    issues: list[ParseIssue] = []
    if audit.unsupported_nodes:
        issues.append(
            _issue(
                "DOCX_UNSUPPORTED_NODE",
                "skipped_without_indexable_content",
                audit.unsupported_nodes,
                "未知节点不含可索引文本或媒体，已安全跳过。",
            )
        )
    if audit.unsupported_content_with_evidence:
        issues.append(
            _issue(
                "DOCX_UNSUPPORTED_NODE",
                "issue_without_representation",
                audit.unsupported_content_with_evidence,
                "未知可索引结构按 best-effort 策略跳过。",
            )
        )
    if audit.toc_controls_skipped:
        issues.append(
            _issue(
                "DOCX_TOC_CONTROL_SKIPPED",
                "skipped_generated_content",
                audit.toc_controls_skipped,
                "目录内容控件由旧安全 Parser 明确跳过。",
            )
        )
    return tuple(issues)


def _parse_report(
    nodes: tuple[DocumentNode, ...],
    elements: list[Element],
    issues: list[ParseIssue],
    audits: tuple[DocxParseAudit, DocxPackageAudit],
    *,
    elapsed_seconds: float,
) -> ParseReport:
    legacy_audit, package_audit = audits
    represented = sum(bool(element.text) for element in elements)
    visible = represented + legacy_audit.unsupported_content_with_evidence
    return ParseReport(
        parser_id=LegacyDocxIrParser.descriptor.name,
        parser_version=LegacyDocxIrParser.descriptor.version,
        node_count=len(nodes),
        visible_text_nodes=visible,
        represented_visible_text_nodes=represented,
        unsupported_with_text=legacy_audit.unsupported_content_with_evidence,
        unsupported_with_media=package_audit.unsupported_media_relationships,
        story_counts=((StoryKind.BODY.value, len(nodes)),),
        issues=tuple(issues),
        elapsed_seconds=elapsed_seconds,
        warnings=tuple(issue.code for issue in issues),
    )


def _issue(
    code: str,
    action: str,
    count: int,
    message: str,
) -> ParseIssue:
    return ParseIssue(
        code=code,
        severity="warning",
        action=action,
        count=count,
        safe_message=message,
    )
