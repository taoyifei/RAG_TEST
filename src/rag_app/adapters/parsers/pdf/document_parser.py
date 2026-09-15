"""把规范化 Paddle PDF 页面映射为现有 Document IR。"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from collections.abc import Callable

from rag_app.adapters.parsers.pdf.source import inspect_pdf_source
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ParserCapabilities,
    ProviderMode,
)
from rag_app.core.errors import InvalidDocument, ProviderInvalidResponse
from rag_app.core.identifiers import document_version_id, node_id
from rag_app.core.models import (
    CellGrid,
    DocumentIR,
    DocumentNode,
    DocumentSource,
    DocumentVersionRef,
    JsonObject,
    ListAttributes,
    NodeKind,
    ParseContext,
    ParsedArtifact,
    ParseIssue,
    ParseReport,
    ParseResult,
    ParseSource,
    PdfBlock,
    PdfBlockType,
    PdfPage,
    PdfPageProgress,
    PdfParseResult,
    PdfSourceInspection,
    PdfTableCell,
    SourceAnchor,
    StoryKind,
    text_payload,
    validate_document_ir,
)
from rag_app.core.policies import ParsingPolicy
from rag_app.core.ports import (
    PdfDocumentParserPort,
    PdfParseCachePort,
    PdfProgressPort,
)

_PDF_MEDIA_TYPE = "application/pdf"
_MAPPER_REVISION = "pdf-ir-v1"


class PdfIrParser:
    """复用现有 ParserPort，内部只增加 PDF Adapter 和 IR 映射。"""

    parser_capabilities = ParserCapabilities(
        supported_extensions=(".pdf",),
        supported_media_types=(_PDF_MEDIA_TYPE,),
        supports_tables=True,
        supports_images="partial",
        supports_numbering="partial",
    )

    def __init__(
        self,
        provider: PdfDocumentParserPort,
        *,
        cache: PdfParseCachePort | None = None,
        progress: PdfProgressPort | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建 PDF IR Parser。

        Args:
            provider: 自托管或官方 API Adapter。
            cache: 可选持久解析缓存。
            progress: 可选持久 Job 页面进度。
            clock: ParseReport 单调时钟。

        Returns:
            无返回值。

        """
        self._provider = provider
        self._cache = cache
        self._progress = progress
        self._clock = clock

    @property
    def descriptor(self) -> ComponentDescriptor:
        """返回包含 Provider 修订的 IR Parser 身份。"""
        return ComponentDescriptor(
            kind=ComponentKind.PARSER,
            name="paddle-pdf-ir",
            version=f"{_MAPPER_REVISION}+{self._provider.parser_revision}",
            mode=ProviderMode.REMOTE,
            capabilities=ComponentCapabilities(
                permits_network=True,
                formats=(_PDF_MEDIA_TYPE,),
            ),
        )

    def parse(
        self,
        source: ParseSource,
        policy: ParsingPolicy,
        context: ParseContext,
    ) -> ParseResult:
        """检查原件、读取完整 Paddle 结果并映射到格式中立 IR。

        Args:
            source: 原始 PDF 与上传元数据。
            policy: 当前不可放宽的资源策略。
            context: 文档、Job、取消与页面进度边界。

        Returns:
            原始 PDF Artifact、Document IR 和解析报告。

        Raises:
            InvalidDocument: PDF 空白、损坏、加密或无可索引内容。
            ProviderInvalidResponse: 缓存或 Provider 身份不一致。

        """
        started_at = self._clock()
        inspection = inspect_pdf_source(source, policy, context)
        self._put_progress(context, inspection.page_count, 0, (), False)
        cached = self._cached(inspection)

        def _page_progress(
            parsed_pages: int,
            failed_page_indices: tuple[int, ...],
            truncated: bool,
        ) -> None:
            self._put_progress(
                context,
                inspection.page_count,
                parsed_pages,
                failed_page_indices,
                truncated,
            )
            if context.page_progress is not None:
                context.page_progress(
                    parsed_pages, failed_page_indices, truncated
                )

        if cached is None:
            parsed = self._provider.parse_pdf(
                source,
                policy,
                context.model_copy(update={"page_progress": _page_progress}),
                inspection,
            )
            self._validate_provider_result(parsed, inspection)
            if self._cache is not None:
                self._cache.put(parsed)
        else:
            parsed = cached
        self._put_progress(
            context,
            inspection.page_count,
            inspection.page_count,
            (),
            False,
        )
        return _to_document_ir(
            source,
            context,
            parsed,
            policy,
            elapsed_seconds=self._clock() - started_at,
        )

    def close(self) -> None:
        """关闭具体 Paddle Adapter。"""
        self._provider.close()

    def _cached(self, inspection: PdfSourceInspection) -> PdfParseResult | None:
        if self._cache is None:
            return None
        cached = self._cache.get(
            source_sha256=inspection.source_sha256,
            parser_mode=self._provider.parser_mode,
            parser_model=self._provider.parser_model,
            parser_revision=self._provider.parser_revision,
            parser_options_identity=self._provider.parser_options_identity,
        )
        if cached is not None and cached.page_count != inspection.page_count:
            return None
        return cached

    def _put_progress(
        self,
        context: ParseContext,
        total_pages: int,
        parsed_pages: int,
        failed_page_indices: tuple[int, ...],
        truncated: bool,
    ) -> None:
        if self._progress is None or context.job_id is None:
            return
        self._progress.put_progress(
            context.job_id,
            PdfPageProgress(
                parser_mode=self._provider.parser_mode,
                parser_model=self._provider.parser_model,
                total_pages=total_pages,
                parsed_pages=parsed_pages,
                failed_page_indices=failed_page_indices,
                truncated=truncated,
            ),
        )

    def _validate_provider_result(
        self, parsed: PdfParseResult, inspection: PdfSourceInspection
    ) -> None:
        expected = (
            inspection.source_sha256,
            self._provider.parser_mode,
            self._provider.parser_model,
            self._provider.parser_revision,
            self._provider.parser_options_identity,
            inspection.page_count,
        )
        observed = (
            parsed.source_sha256,
            parsed.parser_mode,
            parsed.parser_model,
            parsed.parser_revision,
            parsed.parser_options_identity,
            parsed.page_count,
        )
        if observed != expected:
            raise ProviderInvalidResponse(
                "PDF Parser 返回身份与本次请求不一致。",
                stage="pdf.ir.identity",
                code="PDF_PARSER_IDENTITY_MISMATCH",
            )


class _PdfIrBuilder:
    """按 Paddle 页面和块顺序生成满足现有 IR 不变量的节点。"""

    def __init__(self, version_id: str) -> None:
        self.version_id = version_id
        self.nodes: list[DocumentNode] = []
        self.roots: list[str] = []
        self.issues: list[ParseIssue] = []
        self._root_order = 0

    def add_page(self, page: PdfPage) -> None:
        for block in page.blocks:
            if block.block_type is PdfBlockType.TABLE:
                self._add_table(page, block)
            elif block.block_type is PdfBlockType.FORMULA:
                self._add_formula(page, block)
            elif block.block_type in {PdfBlockType.IMAGE, PdfBlockType.CHART}:
                self._add_unsupported(page, block, "PDF_MEDIA_METADATA_ONLY")
            elif block.block_type is PdfBlockType.UNSUPPORTED:
                self._add_unsupported(page, block, "PDF_BLOCK_UNSUPPORTED")
            else:
                self._add_text(page, block)

    def _add_text(self, page: PdfPage, block: PdfBlock) -> None:
        value = (block.text or block.markdown or "").strip()
        if not value:
            return
        kind = {
            PdfBlockType.HEADING: NodeKind.HEADING,
            PdfBlockType.LIST_ITEM: NodeKind.LIST_ITEM,
        }.get(block.block_type, NodeKind.PARAGRAPH)
        path = _block_path(page, block)
        payload = text_payload(value)
        identifier = node_id(
            self.version_id,
            _part_uri(page),
            path,
            kind.value,
            payload.semantic_sha256,
        )
        metadata: JsonObject = (
            ("origin", "pdf_parsed"),
            ("pdf_block_label", block.raw_label),
        )
        if kind is NodeKind.HEADING:
            metadata += (("heading_level", _heading_level(block.raw_label)),)
        node = DocumentNode(
            node_id=identifier,
            kind=kind,
            order=self._root_order,
            anchor=_anchor(
                page,
                block,
                path,
                block.order,
                text=value,
            ),
            text_payload=payload,
            list_attributes=(
                ListAttributes(level=0) if kind is NodeKind.LIST_ITEM else None
            ),
            metadata=metadata,
        )
        self._append_root(node)

    def _add_formula(self, page: PdfPage, block: PdfBlock) -> None:
        value = (block.text or block.markdown or "").strip()
        if not value:
            return
        note_path = _block_path(page, block)
        empty_hash = hashlib.sha256(b"").hexdigest()
        note_id = node_id(
            self.version_id,
            _part_uri(page),
            note_path,
            NodeKind.NOTE.value,
            empty_hash,
        )
        text_path = (*note_path, "formula-text")
        payload = text_payload(value)
        text_id = node_id(
            self.version_id,
            _part_uri(page),
            text_path,
            NodeKind.PARAGRAPH.value,
            payload.semantic_sha256,
        )
        note = DocumentNode(
            node_id=note_id,
            kind=NodeKind.NOTE,
            child_ids=(text_id,),
            order=self._root_order,
            anchor=_anchor(page, block, note_path, block.order),
            metadata=(("pdf_block_label", block.raw_label),),
        )
        formula = DocumentNode(
            node_id=text_id,
            kind=NodeKind.PARAGRAPH,
            parent_node_id=note_id,
            order=0,
            anchor=_anchor(
                page,
                block,
                text_path,
                block.order,
                text=value,
            ),
            text_payload=payload,
            metadata=(
                ("origin", "pdf_parsed"),
                ("pdf_block_label", block.raw_label),
            ),
        )
        self._append_root(note)
        self.nodes.append(formula)

    def _add_table(self, page: PdfPage, block: PdfBlock) -> None:
        if block.table is None:
            self._add_unsupported(
                page,
                block,
                "PDF_TABLE_STRUCTURE_UNAVAILABLE",
            )
            return
        table_path = _block_path(page, block)
        empty_hash = hashlib.sha256(b"").hexdigest()
        table_id = node_id(
            self.version_id,
            _part_uri(page),
            table_path,
            NodeKind.TABLE.value,
            empty_hash,
        )
        grouped: dict[int, list[PdfTableCell]] = defaultdict(list)
        for cell in block.table.cells:
            grouped[cell.row_index].append(cell)
        rows: list[DocumentNode] = []
        descendants: list[DocumentNode] = []
        for row_order, source_row in enumerate(sorted(grouped)):
            row_path = (*table_path, f"row:{source_row}")
            row_id = node_id(
                self.version_id,
                _part_uri(page),
                row_path,
                NodeKind.TABLE_ROW.value,
                empty_hash,
            )
            cells: list[DocumentNode] = []
            row_descendants: list[DocumentNode] = []
            ordered_cells = sorted(
                grouped[source_row], key=lambda item: item.column_index
            )
            for cell_order, cell in enumerate(ordered_cells):
                cell_path = (
                    *row_path,
                    f"cell:{cell.row_index}:{cell.column_index}",
                )
                payload = text_payload(cell.text)
                cell_id = node_id(
                    self.version_id,
                    _part_uri(page),
                    cell_path,
                    NodeKind.TABLE_CELL.value,
                    payload.semantic_sha256,
                )
                text_id: str | None = None
                if cell.text:
                    text_path = (*cell_path, "text")
                    text_id = node_id(
                        self.version_id,
                        _part_uri(page),
                        text_path,
                        NodeKind.PARAGRAPH.value,
                        payload.semantic_sha256,
                    )
                    row_descendants.append(
                        DocumentNode(
                            node_id=text_id,
                            kind=NodeKind.PARAGRAPH,
                            parent_node_id=cell_id,
                            order=0,
                            anchor=_anchor(
                                page,
                                block,
                                text_path,
                                block.order,
                                text=cell.text,
                                table_id=block.table.table_id,
                            ),
                            text_payload=payload,
                            metadata=(
                                ("origin", "pdf_parsed"),
                                ("pdf_block_label", block.raw_label),
                            ),
                        )
                    )
                cells.append(
                    DocumentNode(
                        node_id=cell_id,
                        kind=NodeKind.TABLE_CELL,
                        parent_node_id=row_id,
                        child_ids=() if text_id is None else (text_id,),
                        order=cell_order,
                        anchor=_anchor(
                            page,
                            block,
                            cell_path,
                            block.order,
                            table_id=block.table.table_id,
                        ),
                        cell_grid=CellGrid(
                            row_index=cell.row_index,
                            column_index=cell.column_index,
                            row_span=cell.row_span,
                            column_span=cell.column_span,
                        ),
                        metadata=(
                            ("column_span", cell.column_span),
                            ("row_span", cell.row_span),
                        ),
                    )
                )
            rows.append(
                DocumentNode(
                    node_id=row_id,
                    kind=NodeKind.TABLE_ROW,
                    parent_node_id=table_id,
                    child_ids=tuple(cell.node_id for cell in cells),
                    order=row_order,
                    anchor=_anchor(
                        page,
                        block,
                        row_path,
                        block.order,
                        table_id=block.table.table_id,
                    ),
                )
            )
            descendants.extend((*cells, *row_descendants))
        table = DocumentNode(
            node_id=table_id,
            kind=NodeKind.TABLE,
            child_ids=tuple(row.node_id for row in rows),
            order=self._root_order,
            anchor=_anchor(
                page,
                block,
                table_path,
                block.order,
                table_id=block.table.table_id,
            ),
            metadata=(
                ("pdf_block_label", block.raw_label),
                ("pdf_table_parse", block.table.source_format),
            ),
        )
        self._append_root(table)
        self.nodes.extend(rows)
        self.nodes.extend(descendants)

    def _add_unsupported(
        self, page: PdfPage, block: PdfBlock, issue_code: str
    ) -> None:
        path = _block_path(page, block)
        empty_hash = hashlib.sha256(b"").hexdigest()
        identifier = node_id(
            self.version_id,
            _part_uri(page),
            path,
            NodeKind.UNSUPPORTED.value,
            empty_hash,
        )
        anchor = _anchor(page, block, path, block.order)
        self._append_root(
            DocumentNode(
                node_id=identifier,
                kind=NodeKind.UNSUPPORTED,
                order=self._root_order,
                anchor=anchor,
                metadata=(("pdf_block_label", block.raw_label),),
            )
        )
        self.issues.append(
            ParseIssue(
                code=issue_code,
                severity="warning",
                action="preserve_metadata_only",
                anchor=anchor,
                safe_message="PDF 块未映射为可索引正文，已保留来源身份。",
            )
        )

    def _append_root(self, node: DocumentNode) -> None:
        self.nodes.append(node)
        self.roots.append(node.node_id)
        self._root_order += 1


def _to_document_ir(
    source: ParseSource,
    context: ParseContext,
    parsed: PdfParseResult,
    policy: ParsingPolicy,
    *,
    elapsed_seconds: float,
) -> ParseResult:
    content_sha256 = hashlib.sha256(source.content).hexdigest()
    version_id = document_version_id(
        context.document.document_id, content_sha256
    )
    builder = _PdfIrBuilder(version_id)
    for page in parsed.pages:
        builder.add_page(page)
    visible = sum(
        bool(node.text_payload and node.text_payload.exact_text.strip())
        for node in builder.nodes
    )
    if visible == 0:
        raise InvalidDocument(
            "PaddleOCR 未从 PDF 中返回可索引文字。",
            stage="pdf.ir.content",
            code="PDF_NO_INDEXABLE_CONTENT",
        )
    warnings = tuple(dict.fromkeys(issue.code for issue in builder.issues))
    report = ParseReport(
        parser_id="paddle-pdf-ir",
        parser_version=f"{_MAPPER_REVISION}+{parsed.parser_revision}",
        node_count=len(builder.nodes),
        visible_text_nodes=visible,
        represented_visible_text_nodes=visible,
        unsupported_with_media=sum(
            node.kind is NodeKind.UNSUPPORTED for node in builder.nodes
        ),
        part_count=parsed.page_count,
        story_counts=((StoryKind.BODY.value, len(builder.nodes)),),
        issues=tuple(builder.issues),
        elapsed_seconds=elapsed_seconds,
        warnings=warnings,
        pdf_page_count=parsed.page_count,
        pdf_parsed_page_count=len(parsed.pages),
        pdf_parser_mode=parsed.parser_mode.value,
        pdf_parser_model=parsed.parser_model,
    )
    artifact_id = f"sha256:{content_sha256}"
    document_ir = DocumentIR(
        source=DocumentSource(
            document_id=context.document.document_id,
            document_version_id=version_id,
            display_name=source.display_name,
            media_type=_PDF_MEDIA_TYPE,
            extension=".pdf",
            content_sha256=content_sha256,
            size_bytes=len(source.content),
            blob_ref=artifact_id,
        ),
        document=context.document.model_copy(
            update={"display_name": source.display_name}
        ),
        version=DocumentVersionRef(
            document_id=context.document.document_id,
            document_version_id=version_id,
            content_sha256=content_sha256,
        ),
        root_node_ids=tuple(builder.roots),
        nodes=tuple(builder.nodes),
        parse_report=report,
        metadata=(
            ("parsing_policy_id", policy.policy_id),
            ("pdf_parser_mode", parsed.parser_mode.value),
            ("pdf_parser_model", parsed.parser_model),
            ("pdf_parser_revision", parsed.parser_revision),
            ("pdf_parser_options_identity", parsed.parser_options_identity),
        ),
    )
    validate_document_ir(document_ir)
    return ParseResult(
        document_ir=document_ir,
        report=report,
        artifacts=(
            ParsedArtifact(
                artifact_id=artifact_id,
                content_sha256=content_sha256,
                media_type=_PDF_MEDIA_TYPE,
                content=source.content,
                role="source_document",
            ),
        ),
    )


def _anchor(  # noqa: PLR0913
    page: PdfPage,
    block: PdfBlock,
    path: tuple[str, ...],
    ordinal: int,
    *,
    text: str | None = None,
    table_id: str | None = None,
) -> SourceAnchor:
    return SourceAnchor(
        part_uri=_part_uri(page),
        story_kind=StoryKind.BODY,
        structural_path=path,
        ordinal=ordinal,
        source_start_char=0 if text is not None else None,
        source_end_char=len(text) if text is not None else None,
        page_index=page.page_index,
        page_width=page.width,
        page_height=page.height,
        bbox=block.bbox,
        pdf_block_id=block.block_id,
        pdf_table_id=table_id,
    )


def _part_uri(page: PdfPage) -> str:
    return f"/pdf/pages/{page.page_index}"


def _block_path(page: PdfPage, block: PdfBlock) -> tuple[str, ...]:
    return (f"page:{page.page_index}", f"block:{block.block_id}")


def _heading_level(raw_label: str) -> int:
    return 1 if raw_label in {"doc_title", "document_title"} else 2


__all__ = ["PdfIrParser"]
