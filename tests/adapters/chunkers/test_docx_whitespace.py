"""长表格空白分割的公共合成回归。"""

from __future__ import annotations

import random
from xml.sax.saxutils import escape

import pytest

from rag_app.adapters.chunkers.docx_structural.reports import (
    build_chunking_report,
)
from rag_app.core.models import ChunkingPolicy, NodeKind, SourceSpanKind
from tests.adapters.chunkers.test_docx_structural import _chunk
from tests.adapters.parsers.docx.fixtures import build_package, parse_package


@pytest.mark.parametrize("separator", [" ", "  ", "\n"])
@pytest.mark.parametrize("trailing", ["", " \n"])
def test_long_table_rows_do_not_publish_whitespace_only_chunks(
    separator: str,
    trailing: str,
) -> None:
    """长表格行的断句空白仍归属于来源，不能独立成为无正文块。"""
    randomizer = random.Random(0)  # noqa: S311
    text = (
        separator.join(
            "检查" * randomizer.randint(1, 20) + "。" for _ in range(16)
        )
        + trailing
    )
    blocks = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>" + "长章节名称" * 10 + "</w:t></w:r></w:p>"
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>"
        "<w:tr><w:tc><w:p><w:r><w:t>巡检员</w:t></w:r></w:p></w:tc>"
        '<w:tc><w:p><w:r><w:t xml:space="preserve">'
        + escape(text)
        + "</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    document_ir = parse_package(build_package(blocks)).document_ir
    result = _chunk(
        document_ir,
        policy=ChunkingPolicy(overlap_cap_tokens=0),
    )
    assert len(result.chunks) > 1
    assert all(chunk.citation_text.strip() for chunk in result.chunks)
    paragraph = next(node for node in document_ir.nodes if node.text == text)
    spans = [
        span
        for chunk in result.chunks
        for span in chunk.source_spans
        if span.node_id == paragraph.node_id
        and span.span_type is SourceSpanKind.ORIGINAL_TEXT
    ]
    assert (
        "".join(
            text[span.source_start_char : span.source_end_char]
            for span in spans
        )
        == text
    )


def test_bookmark_heading_is_represented_by_chunk_heading_context() -> None:
    """正文引用已有标题时，标题上下文不能误报为悬空关系。"""
    document_ir = parse_package(
        build_package(
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            '<w:bookmarkStart w:id="1" w:name="inspection"/>'
            "<w:r><w:t>巡检要求</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>每日确认设备状态。</w:t></w:r></w:p>"
            '<w:p><w:fldSimple w:instr=" REF inspection ">'
            "<w:r><w:t>按巡检要求完成记录。</w:t></w:r>"
            "</w:fldSimple></w:p>"
        )
    ).document_ir
    result = _chunk(document_ir)
    assert document_ir.relationships
    assert result.report.orphan_relation_count == 0
    assert result.report.source_span_coverage == 1.0
    heading = next(n for n in document_ir.nodes if n.kind is NodeKind.HEADING)
    assert all(heading.text in chunk.heading_path for chunk in result.chunks)
    # 真正丢失标题上下文仍必须被报告，不能把所有标题关系豁免。
    without_context = tuple(
        chunk.model_copy(update={"heading_path": ()}) for chunk in result.chunks
    )
    report = build_chunking_report(
        without_context,
        document_ir,
        ChunkingPolicy(),
        elapsed_seconds=0.0,
    )
    assert report.orphan_relation_count == 1
