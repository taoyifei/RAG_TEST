"""旧契约到 Core 类型的显式单向转换。"""

from __future__ import annotations

from rag_app.adapters.parsers.legacy_docx_ir import LegacyDocxIrParser
from rag_app.contracts import Chunk as LegacyChunk
from rag_app.contracts import ChunkSourceSpan as LegacySourceSpan
from rag_app.contracts import Element as LegacyElement
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    Chunk,
    ChunkingContext,
    ChunkingReport,
    ChunkingResult,
    DocumentIR,
    DocumentNode,
    DocumentVersionRef,
    ImageAttributes,
    ListAttributes,
    NodeKind,
    SourceAnchor,
    SourceSpan,
    StoryKind,
    text_payload,
)


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
    structural_path = ("legacy", element.element_id)
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
    kind = {
        "heading": NodeKind.HEADING,
        "paragraph": (
            NodeKind.LIST_ITEM
            if element.list_level is not None
            else NodeKind.PARAGRAPH
        ),
        "table": NodeKind.TABLE_REPRESENTATION,
        "image": NodeKind.IMAGE,
    }[element.kind.value]
    node = DocumentNode(
        node_id=generated_node_id,
        kind=kind,
        order=0,
        anchor=SourceAnchor(
            part_uri="/word/document.xml",
            story_kind=StoryKind.BODY,
            structural_path=structural_path,
            ordinal=0,
        ),
        text_payload=(
            text_payload(element.text)
            if element.kind.value != "image"
            else None
        ),
        list_attributes=(
            ListAttributes(level=element.list_level)
            if element.list_level is not None
            else None
        ),
        image_attributes=(
            ImageAttributes(
                blob_ref=f"legacy-unpersisted:{element.content_sha256}",
                media_type=element.media_type or "application/octet-stream",
                content_sha256=element.content_sha256,
                display_name=element.media_name,
            )
            if element.kind.value == "image"
            else None
        ),
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


class LegacyDocxParserAdapter(LegacyDocxIrParser):
    """保留 P01 类名并委托 P03 正式 Parser adapter。"""


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
