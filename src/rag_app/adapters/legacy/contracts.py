"""旧契约到 Core 类型的显式单向转换。"""

from __future__ import annotations

import hashlib

from rag_app.adapters.parsers.legacy_docx_ir import LegacyDocxIrParser
from rag_app.contracts import Chunk as LegacyChunk
from rag_app.contracts import ChunkIdentity as LegacyChunkIdentity
from rag_app.contracts import ChunkRole as LegacyChunkRole
from rag_app.contracts import ChunkSourceSpan as LegacySourceSpan
from rag_app.contracts import Element as LegacyElement
from rag_app.contracts import ElementKind as LegacyElementKind
from rag_app.contracts import Locator
from rag_app.contracts import stable_chunk_id as legacy_stable_chunk_id
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
    ChunkRole,
    DocumentIR,
    DocumentNode,
    DocumentVersionRef,
    ImageAttributes,
    ListAttributes,
    NodeKind,
    SourceAnchor,
    SourceSpan,
    SourceSpanKind,
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
            source_anchor=SourceAnchor(
                part_uri="/legacy/document",
                story_kind=StoryKind.BODY,
                structural_path=structural_path,
                ordinal=0,
                source_start_char=span.source_start_char,
                source_end_char=span.source_end_char,
            ),
            structural_path=structural_path,
            chunk_start_char=span.start_char,
            chunk_end_char=span.end_char,
            source_start_char=span.source_start_char,
            source_end_char=span.source_end_char,
            span_type=(
                SourceSpanKind.REPEATED_CONTEXT
                if span.is_repeated
                else SourceSpanKind.ORIGINAL_TEXT
            ),
            is_repeated=span.is_repeated,
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
    spans = _cover_legacy_span_gaps(
        chunk.text,
        tuple(item[0] for item in converted),
    )
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
            lexical_text=chunk.text,
            token_count=len(chunk.embedding_text.encode("utf-8")),
            token_count_is_estimate=True,
            tokenizer_id="legacy-unknown-v1",
            content_sha256=hashlib.sha256(
                chunk.text.encode("utf-8")
            ).hexdigest(),
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

    descriptor = ComponentDescriptor(
        kind=ComponentKind.PARSER,
        name="legacy-docx",
        version=LegacyDocxIrParser.descriptor.version,
        mode=ProviderMode.LEGACY,
        capabilities=LegacyDocxIrParser.descriptor.capabilities,
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
            exact_text = (
                node.text_payload.exact_text
                if node.text_payload is not None
                else node.text
            )
            span = SourceSpan(
                node_id=node.node_id,
                source_anchor=node.anchor,
                structural_path=node.structural_path,
                chunk_start_char=0,
                chunk_end_char=len(exact_text),
                source_start_char=0,
                source_end_char=len(exact_text),
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
                    citation_text=exact_text,
                    embedding_text=exact_text,
                    lexical_text=exact_text,
                    token_count=len(exact_text.encode("utf-8")),
                    token_count_is_estimate=True,
                    tokenizer_id="legacy-unknown-v1",
                    content_sha256=hashlib.sha256(
                        exact_text.encode("utf-8")
                    ).hexdigest(),
                    metadata=(("legacy_adapter", "one-node-one-chunk"),),
                )
            )
        return ChunkingResult(
            chunks=tuple(chunks),
            report=ChunkingReport(chunk_count=len(chunks)),
        )


def _cover_legacy_span_gaps(
    text: str,
    spans: tuple[SourceSpan, ...],
) -> tuple[SourceSpan, ...]:
    covered: list[SourceSpan] = []
    cursor = 0
    for span in spans:
        if span.chunk_start_char > cursor:
            covered.append(
                SourceSpan(
                    span_type=SourceSpanKind.SEPARATOR,
                    chunk_start_char=cursor,
                    chunk_end_char=span.chunk_start_char,
                    is_citable=False,
                )
            )
        covered.append(span)
        cursor = span.chunk_end_char
    if cursor < len(text):
        covered.append(
            SourceSpan(
                span_type=SourceSpanKind.SEPARATOR,
                chunk_start_char=cursor,
                chunk_end_char=len(text),
                is_citable=False,
            )
        )
    return tuple(covered)


def core_chunk_to_legacy(
    chunk: Chunk,
    *,
    display_name: str,
) -> tuple[LegacyChunk, tuple[str, ...]]:
    """把基础 V3 字段映射回旧 Chunk，并报告结构损失。

    Args:
        chunk: canonical Chunk V3。
        display_name: 旧 Locator 需要的展示名，不参与稳定身份。

    Returns:
        可供旧 Query/index 读取的 Chunk 与有序损失 warning。

    """
    legacy_spans: list[LegacySourceSpan] = []
    warnings: set[str] = set()
    for span in chunk.source_spans:
        if span.span_type in {
            SourceSpanKind.SEPARATOR,
            SourceSpanKind.DERIVED_NUMBERING,
        }:
            warnings.add("CHUNK_V3_SPAN_NOT_EXPRESSIBLE_IN_LEGACY")
            continue
        anchor = span.source_anchor
        if anchor is None or span.node_id is None:
            warnings.add("CHUNK_V3_SPAN_NOT_EXPRESSIBLE_IN_LEGACY")
            continue
        source_start = span.source_start_char
        source_end = span.source_end_char
        if source_start is None or source_end is None:
            warnings.add("CHUNK_V3_SPAN_NOT_EXPRESSIBLE_IN_LEGACY")
            continue
        locator = Locator(
            file_path=display_name,
            heading_path=chunk.heading_path,
            paragraph_index=_legacy_positive(anchor.paragraph_index),
            table_index=_legacy_positive(anchor.table_index),
            image_index=(1 if chunk.role is ChunkRole.IMAGE_METADATA else None),
            fragment=chunk.citation_text[
                span.chunk_start_char : span.chunk_end_char
            ][:240],
        )
        legacy_spans.append(
            LegacySourceSpan(
                element_id=span.node_id,
                locator=locator,
                start_char=span.chunk_start_char,
                end_char=span.chunk_end_char,
                source_start_char=source_start,
                source_end_char=source_end,
                is_repeated=span.is_repeated,
            )
        )
    if not legacy_spans:
        raise ValueError("Chunk V3 不含旧 payload 可表达的来源跨度。")
    if chunk.child_group_ids or chunk.note_refs:
        warnings.add("CHUNK_V3_RELATIONSHIPS_NOT_EXPRESSIBLE_IN_LEGACY")
    legacy_role, element_kind = _legacy_chunk_role(chunk.role)
    source_digest = hashlib.sha256(
        chunk.version.document_id.encode()
    ).hexdigest()
    source_id = f"src_{source_digest[:32]}"
    doc_version = f"sha256:{chunk.version.content_sha256}"
    section_id = _legacy_prefixed_id("section", chunk.section_id)
    group_id = _legacy_prefixed_id("group", chunk.neighbor_group_id)
    identity = LegacyChunkIdentity(
        section_id=section_id,
        neighbor_group_id=group_id,
        chunk_role=legacy_role,
        source_spans=tuple(legacy_spans),
    )
    legacy_id = legacy_stable_chunk_id(source_id, identity, chunk.citation_text)
    locators = tuple(dict.fromkeys(span.locator for span in legacy_spans))
    return (
        LegacyChunk(
            chunk_id=legacy_id,
            source_id=source_id,
            doc_version=doc_version,
            pipeline_fingerprint=chunk.chunker_fingerprint,
            section_id=section_id,
            neighbor_group_id=group_id,
            chunk_role=legacy_role,
            source_spans=tuple(legacy_spans),
            text=chunk.citation_text,
            embedding_text=chunk.embedding_text,
            element_kind=element_kind,
            locators=locators,
            content_sha256=chunk.content_sha256,
            document_status="active",
            authority_level="official",
            effective_from=None,
            effective_to=None,
            contains_ocr=False,
        ),
        tuple(sorted(warnings)),
    )


def _legacy_chunk_role(
    role: ChunkRole,
) -> tuple[LegacyChunkRole, LegacyElementKind]:
    if role is ChunkRole.TABLE:
        return LegacyChunkRole.TABLE, LegacyElementKind.TABLE
    if role is ChunkRole.IMAGE_METADATA:
        return LegacyChunkRole.TEXT, LegacyElementKind.PARAGRAPH
    return LegacyChunkRole.TEXT, LegacyElementKind.PARAGRAPH


def _legacy_prefixed_id(prefix: str, value: str) -> str:
    expected = f"{prefix}_"
    if value.startswith(expected) and len(value) == len(expected) + 32:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _legacy_positive(value: int | None) -> int | None:
    return None if value is None else max(1, value)
