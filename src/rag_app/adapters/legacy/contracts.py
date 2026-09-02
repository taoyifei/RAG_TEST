"""旧契约到 Core 类型的显式单向转换。"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from rag_app.contracts import Chunk as LegacyChunk
from rag_app.contracts import ChunkSourceSpan as LegacySourceSpan
from rag_app.contracts import Element as LegacyElement
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import ConfigurationError, InvalidDocument
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    Chunk,
    ChunkingContext,
    ChunkingReport,
    ChunkingResult,
    DocumentIR,
    DocumentNode,
    DocumentRef,
    DocumentVersionRef,
    ParsePolicy,
    ParseReport,
    ParseResult,
    ParseSource,
    SourceSpan,
)
from rag_app.parsers.docx import DocxParser, UnsafeDocxError


def legacy_element_to_core(
    element: LegacyElement,
    document_version_id: str,
) -> tuple[DocumentNode, tuple[str, ...]]:
    """把旧 Element 转成不含 binary data 的 DocumentNode。

    Args:
        element: 旧解析器元素。
        document_version_id: 新 Core 文档版本 ID。

    Returns:
        新节点和所有显式丢失字段 warning。

    """
    structural_path = (
        *element.locator.heading_path,
        element.locator.logical_key(),
    )
    generated_node_id = deterministic_id(
        "node",
        document_version_id,
        structural_path,
        element.kind.value,
        element.content_sha256,
    )
    warnings: list[str] = ["LEGACY_FILE_PATH_OMITTED"]
    if element.binary_data is not None:
        warnings.append("LEGACY_BINARY_DATA_OMITTED")
    node = DocumentNode(
        node_id=generated_node_id,
        node_type=element.kind.value,
        structural_path=structural_path,
        text=element.text,
        content_sha256=element.content_sha256,
        metadata=(
            ("legacy_element_id", element.element_id),
            ("media_type", element.media_type),
            ("media_name", element.media_name),
            ("list_level", element.list_level),
            ("ocr_state", element.ocr_state.value),
            ("ocr_confidence", element.ocr_confidence),
            ("ocr_error_code", element.ocr_error),
        ),
    )
    return node, tuple(warnings)


def legacy_span_to_core(
    span: LegacySourceSpan,
    document_version_id: str,
) -> tuple[SourceSpan, tuple[str, ...]]:
    """把旧 ChunkSourceSpan 转成格式中立 SourceSpan。

    Args:
        span: 旧字符跨度。
        document_version_id: 新 Core 文档版本 ID。

    Returns:
        新跨度和文件展示名丢失 warning。

    """
    structural_path = (*span.locator.heading_path, span.locator.logical_key())
    generated_node_id = deterministic_id(
        "node",
        document_version_id,
        structural_path,
        span.element_id,
    )
    return (
        SourceSpan(
            node_id=generated_node_id,
            structural_path=structural_path,
            chunk_start_char=span.start_char,
            chunk_end_char=span.end_char,
            source_start_char=span.source_start_char,
            source_end_char=span.source_end_char,
            metadata=(("is_repeated", span.is_repeated),),
        ),
        ("LEGACY_FILE_PATH_OMITTED",),
    )


def legacy_chunk_to_core(
    chunk: LegacyChunk,
) -> tuple[Chunk, tuple[str, ...]]:
    """把旧 Chunk 转成 Core Chunk，并显式报告兼容损失。

    Args:
        chunk: 旧索引 chunk。

    Returns:
        新 chunk 和有序去重 warning。

    """
    document_id = deterministic_id("doc", chunk.source_id)
    version_id = deterministic_id("dver", chunk.doc_version)
    version = DocumentVersionRef(
        document_id=document_id,
        document_version_id=version_id,
        content_sha256=chunk.content_sha256,
    )
    converted = tuple(
        legacy_span_to_core(span, version_id) for span in chunk.source_spans
    )
    spans = tuple(item[0] for item in converted)
    warnings = tuple(
        sorted(
            {
                warning
                for _, item_warnings in converted
                for warning in item_warnings
            }
        )
    )
    core_chunk_id = (
        chunk.chunk_id
        if chunk.chunk_id.startswith("chunk_")
        else deterministic_id("chunk", chunk.chunk_id)
    )
    return (
        Chunk(
            chunk_id=core_chunk_id,
            version=version,
            chunker_fingerprint=chunk.pipeline_fingerprint,
            source_spans=spans,
            citation_text=chunk.text,
            embedding_text=chunk.embedding_text,
            metadata=(
                ("legacy_source_id", chunk.source_id),
                ("legacy_section_id", chunk.section_id),
                ("legacy_neighbor_group_id", chunk.neighbor_group_id),
                ("legacy_chunk_role", chunk.chunk_role.value),
            ),
        ),
        warnings,
    )


class LegacyDocxParserAdapter:
    """用受控临时文件调用现有安全 DOCX parser。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.PARSER,
        name="legacy-docx",
        version=DocxParser.version,
        mode=ProviderMode.LEGACY,
        capabilities=ComponentCapabilities(
            formats=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        ),
    )

    def __init__(self) -> None:
        """创建现有 DOCX parser 实例。

        Args:
            无参数；使用现有安全默认限制。

        Returns:
            无返回值。

        """
        self._parser = DocxParser()

    def parse(self, source: ParseSource, policy: ParsePolicy) -> ParseResult:
        """解析受控 DOCX 字节并转换为 Core IR。

        Args:
            source: DOCX 字节和展示名。
            policy: metadata 中必须提供 project/kb/document 逻辑 ID。

        Returns:
            格式中立 IR 和显式转换 warning。

        Raises:
            ConfigurationError: 逻辑身份未显式提供。
            InvalidDocument: 旧 parser 拒绝输入。

        """
        identity = dict(policy.metadata)
        required = ("project_id", "knowledge_base_id", "document_id")
        if any(not isinstance(identity.get(key), str) for key in required):
            raise ConfigurationError(
                "ParsePolicy 必须显式提供 project/kb/document 逻辑 ID。",
                stage="legacy.parser",
            )
        content_sha256 = hashlib.sha256(source.content).hexdigest()
        version = DocumentVersionRef(
            document_id=str(identity["document_id"]),
            document_version_id=deterministic_id("dver", content_sha256),
            content_sha256=content_sha256,
        )
        document = DocumentRef(
            project_id=str(identity["project_id"]),
            knowledge_base_id=str(identity["knowledge_base_id"]),
            document_id=str(identity["document_id"]),
            display_name=source.display_name,
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="rag-p01-docx-"
            ) as directory:
                path = Path(directory) / "input.docx"
                path.write_bytes(source.content)
                elements = self._parser.parse(
                    path, display_path=source.display_name
                )
        except UnsafeDocxError as error:
            raise InvalidDocument(
                "DOCX 未通过现有安全解析边界。",
                stage="legacy.parser",
                details={"error_type": type(error).__name__},
            ) from None
        converted = tuple(
            legacy_element_to_core(element, version.document_version_id)
            for element in elements
        )
        nodes = tuple(item[0] for item in converted)
        warnings = tuple(
            sorted(
                {
                    warning
                    for _, item_warnings in converted
                    for warning in item_warnings
                }
            )
        )
        return ParseResult(
            document_ir=DocumentIR(
                document=document,
                version=version,
                nodes=nodes,
            ),
            report=ParseReport(node_count=len(nodes), warnings=warnings),
        )


class LegacySectionChunkerAdapter:
    """在 P01 保留来源跨度的一节点一 chunk 兼容边界。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.CHUNKER,
        name="legacy-section-pack",
        version="section-pack-v2-provisional",
        mode=ProviderMode.LEGACY,
    )

    def chunk(
        self,
        document_ir: DocumentIR,
        context: ChunkingContext,
    ) -> ChunkingResult:
        """把每个非空文本节点转换为可追溯 chunk。

        Args:
            document_ir: 格式中立文档。
            context: 冻结 chunker 指纹。

        Returns:
            有序 chunks 与计数报告。

        """
        chunks: list[Chunk] = []
        for node in document_ir.nodes:
            if not node.text.strip():
                continue
            span = SourceSpan(
                node_id=node.node_id,
                structural_path=node.structural_path,
                chunk_start_char=0,
                chunk_end_char=len(node.text),
                source_start_char=0,
                source_end_char=len(node.text),
            )
            chunks.append(
                Chunk(
                    chunk_id=deterministic_id(
                        "chunk",
                        document_ir.version.document_version_id,
                        context.chunker_fingerprint,
                        node.node_id,
                        node.content_sha256,
                    ),
                    version=document_ir.version,
                    chunker_fingerprint=context.chunker_fingerprint,
                    source_spans=(span,),
                    citation_text=node.text,
                    embedding_text=node.text,
                    metadata=(("legacy_adapter", "one-node-one-chunk"),),
                )
            )
        return ChunkingResult(
            chunks=tuple(chunks),
            report=ChunkingReport(chunk_count=len(chunks)),
        )
