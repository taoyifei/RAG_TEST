"""DOCX OOXML v4 ParserPort adapter。"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Callable

from rag_app.adapters.parsers.docx.blocks import BlockParser
from rag_app.adapters.parsers.docx.builder import IrNodeBuilder
from rag_app.adapters.parsers.docx.issues import IssueCollector
from rag_app.adapters.parsers.docx.namespaces import WORD, qn
from rag_app.adapters.parsers.docx.numbering import NumberingCatalog
from rag_app.adapters.parsers.docx.package import DocxPackage
from rag_app.adapters.parsers.docx.stories import parse_related_stories
from rag_app.adapters.parsers.docx.styles import StyleCatalog
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ParserCapabilities,
    ProviderMode,
)
from rag_app.core.errors import ConfigurationError, InvalidDocument
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    DocumentIR,
    DocumentNode,
    DocumentRef,
    DocumentSource,
    DocumentVersionRef,
    NodeKind,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParseSource,
    StoryKind,
)
from rag_app.core.policies import (
    ExternalRelationshipsPolicy,
    ParsingPolicy,
)
from rag_app.core.ports.blob_store import BlobStorePort, BlobWriteRequest

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document"
)


class DocxOoxmlV4Parser:
    """直接读取安全 OOXML package 并输出 P03 Document IR。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.PARSER,
        name="docx-ooxml-v4",
        version="4.0.0",
        mode=ProviderMode.LOCAL,
        capabilities=ComponentCapabilities(formats=(_DOCX_MEDIA_TYPE,)),
    )
    parser_capabilities = ParserCapabilities(
        supported_extensions=(".docx",),
        supported_media_types=(_DOCX_MEDIA_TYPE,),
        supports_tables=True,
        supports_images=True,
        supports_numbering=True,
        supports_headers_footers=True,
        supports_footnotes=True,
        supports_revisions=True,
        supports_comments=True,
        supports_text_boxes=True,
    )

    def __init__(
        self,
        blob_store: BlobStorePort | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建 parser 和可选宿主 BlobStore。

        Args:
            blob_store: 保存源文档和媒体的受控 Store。
            clock: 用于 timeout 与报告的可注入单调时钟。

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
        self._clock = clock

    def parse(self, source: ParseSource, policy: ParsingPolicy) -> ParseResult:
        """安全解析一个 DOCX 字节源。

        Args:
            source: 受控字节、显示名和扩展名。
            policy: 资源、结构和稳定逻辑身份策略。

        Returns:
            Document IR 和同一 ParseReport。

        Raises:
            ConfigurationError: 缺少 project、kb 或 document 逻辑 ID。
            InvalidDocument: package、安全边界或 Blob 写入失败。

        """
        if source.extension.casefold() != ".docx":
            raise InvalidDocument(
                "DOCX v4 仅接受 .docx 扩展名。",
                stage="docx-ooxml-v4.input",
            )
        identity = _policy_identity(policy)
        started_at = self._clock()
        content_sha256 = hashlib.sha256(source.content).hexdigest()
        version_id = deterministic_id("dver", content_sha256)
        issues = IssueCollector()
        builder = IrNodeBuilder(version_id)
        with DocxPackage(
            source.content,
            policy,
            clock=self._clock,
        ) as package:
            _validate_external_relationships(package, policy, issues)
            _record_orphan_parts(package, issues)
            styles = StyleCatalog.from_package(package, issues)
            numbering = NumberingCatalog.from_package(package, issues)
            block_parser = BlockParser(
                package=package,
                policy=policy,
                builder=builder,
                issues=issues,
                styles=styles,
                numbering=numbering,
            )
            main_root = package.xml(package.catalog.main_part_uri)
            body = main_root.find(qn(WORD, "body"))
            if body is None:
                raise InvalidDocument(
                    "DOCX 主文档缺少 w:body。",
                    stage="docx-ooxml-v4.body",
                )
            block_parser.parse_container(
                body,
                parent_node_id=None,
                part_uri=package.catalog.main_part_uri,
                story_kind=StoryKind.BODY,
                structural_path=("body",),
                table_depth=0,
            )
            relationships = parse_related_stories(block_parser)
            nodes = builder.freeze()
            frozen_issues = issues.freeze()
            report = _parse_report(
                nodes,
                frozen_issues,
                unsupported_text=issues.unsupported_text,
                unsupported_media=issues.unsupported_media,
                part_count=len(package.catalog.parts),
                relationship_count=len(package.catalog.relationships),
                revision_count=block_parser.revision_count,
                elapsed_seconds=self._clock() - started_at,
            )
            catalog_identity = canonical_sha256(
                {
                    "main_part_uri": package.catalog.main_part_uri,
                    "parts": [
                        {
                            "part_uri": part.part_uri,
                            "content_type": part.content_type,
                            "size": part.size,
                            "sha256": part.sha256,
                        }
                        for part in package.catalog.parts
                    ],
                    "relationships": [
                        {
                            "source": relationship.source_part_uri,
                            "id": relationship.relationship_id,
                            "type": relationship.relationship_type,
                            "mode": relationship.target_mode,
                            "target": relationship.target_part_uri,
                            "external_scheme": relationship.external_scheme,
                        }
                        for relationship in package.catalog.relationships
                    ],
                }
            )
            document_blob_id = f"document:{version_id}"
            document_ir = DocumentIR(
                source=DocumentSource(
                    document_id=identity["document_id"],
                    document_version_id=version_id,
                    display_name=source.display_name,
                    media_type=_DOCX_MEDIA_TYPE,
                    extension=".docx",
                    content_sha256=content_sha256,
                    size_bytes=len(source.content),
                    blob_ref=document_blob_id,
                ),
                document=DocumentRef(
                    project_id=identity["project_id"],
                    knowledge_base_id=identity["knowledge_base_id"],
                    document_id=identity["document_id"],
                    display_name=source.display_name,
                ),
                version=DocumentVersionRef(
                    document_id=identity["document_id"],
                    document_version_id=version_id,
                    content_sha256=content_sha256,
                ),
                root_node_ids=builder.root_ids,
                nodes=nodes,
                relationships=relationships,
                parse_report=report,
                metadata=(
                    ("part_catalog_identity", catalog_identity),
                    ("parsing_policy_id", policy.policy_id),
                ),
            )
            writes = (
                BlobWriteRequest(
                    blob_id=document_blob_id,
                    content_sha256=content_sha256,
                    media_type=_DOCX_MEDIA_TYPE,
                    content=source.content,
                ),
                *block_parser.blob_writes.values(),
            )
        self._commit_blobs(writes)
        return ParseResult(document_ir=document_ir, report=report)

    def close(self) -> None:
        """释放 parser 自己创建的 BlobStore。

        Args:
            无参数；宿主注入 Store 的生命周期仍归宿主。

        Returns:
            无返回值。

        """
        if self._owns_blob_store:
            self._blob_store.close()

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
                "DOCX v4 Blob 写入失败并已清理。",
                stage="docx-ooxml-v4.blob",
                details={"error_type": type(error).__name__},
            ) from None


def _policy_identity(policy: ParsingPolicy) -> dict[str, str]:
    metadata = dict(policy.metadata)
    required = ("project_id", "knowledge_base_id", "document_id")
    if any(not isinstance(metadata.get(key), str) for key in required):
        raise ConfigurationError(
            "ParsingPolicy 必须显式提供 project/kb/document 逻辑 ID。",
            stage="docx-ooxml-v4.identity",
        )
    return {key: str(metadata[key]) for key in required}


def _validate_external_relationships(
    package: DocxPackage,
    policy: ParsingPolicy,
    issues: IssueCollector,
) -> None:
    external = [
        relationship
        for relationship in package.catalog.relationships
        if relationship.target_mode == "External"
    ]
    if not external:
        return
    if (
        policy.external_relationships
        is ExternalRelationshipsPolicy.REJECT
    ):
        raise InvalidDocument(
            "ParsingPolicy 拒绝 DOCX 外部关系。",
            stage="docx-ooxml-v4.relationship",
        )
    schemes = tuple(
        sorted(
            {
                relationship.external_scheme or "unknown"
                for relationship in external
            }
        )
    )
    issues.add(
        "DOCX_EXTERNAL_RELATIONSHIP_METADATA_ONLY",
        action="metadata_only_no_fetch",
        message="外部关系仅记录协议类型，未访问目标。",
        count=len(external),
        metadata=(("schemes", schemes),),
    )


def _record_orphan_parts(
    package: DocxPackage,
    issues: IssueCollector,
) -> None:
    reachable = {
        package.catalog.main_part_uri,
        "/[Content_Types].xml",
        "/_rels/.rels",
    }
    reachable.update(
        relationship.target_part_uri
        for relationship in package.catalog.relationships
        if relationship.target_part_uri is not None
    )
    relationship_parts = {
        part.part_uri
        for part in package.catalog.parts
        if part.part_uri.endswith(".rels")
    }
    reachable.update(relationship_parts)
    orphans = [
        part
        for part in package.catalog.parts
        if part.part_uri not in reachable
    ]
    if orphans:
        issues.add(
            "DOCX_ORPHAN_PART_IGNORED",
            severity="info",
            action="audit_only",
            message="不可达 package Part 未自动作为正文。",
            count=len(orphans),
        )


def _parse_report(  # noqa: PLR0913
    nodes: tuple[DocumentNode, ...],
    issues: tuple[ParseIssue, ...],
    *,
    unsupported_text: int,
    unsupported_media: int,
    part_count: int,
    relationship_count: int,
    revision_count: int,
    elapsed_seconds: float,
) -> ParseReport:
    represented = sum(
        bool(node.text.strip())
        for node in nodes
        if node.kind is not NodeKind.UNSUPPORTED
    )
    story_counts = Counter(
        node.anchor.story_kind.value for node in nodes
    )
    media_count = sum(
        node.kind is NodeKind.IMAGE for node in nodes
    )
    return ParseReport(
        parser_id=DocxOoxmlV4Parser.descriptor.name,
        parser_version=DocxOoxmlV4Parser.descriptor.version,
        node_count=len(nodes),
        visible_text_nodes=represented + unsupported_text,
        represented_visible_text_nodes=represented,
        unsupported_with_text=unsupported_text,
        unsupported_with_media=unsupported_media,
        part_count=part_count,
        relationship_count=relationship_count,
        media_count=media_count,
        revision_count=revision_count,
        story_counts=tuple(sorted(story_counts.items())),
        issues=issues,
        elapsed_seconds=elapsed_seconds,
        warnings=tuple(issue.code for issue in issues),
    )
