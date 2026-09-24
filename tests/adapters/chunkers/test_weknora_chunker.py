"""固定 Go 分块器与本方来源合同的定向测试。"""

from __future__ import annotations

from rag_app.adapters.chunkers.weknora.chunker import WeKnoraChunkerAdapter
from rag_app.adapters.parsers import DocxOoxmlV4Parser
from rag_app.adapters.parsers.document_router import WeKnoraDocumentRouter
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    DocumentRef,
    ParseContext,
    ParseSource,
    SourceSpanKind,
    WeKnoraChunkingPolicy,
)
from rag_app.core.policies import ParsingPolicy
from tests.adapters.parsers.docx.fixtures import build_package, parse_package
from tests.adapters.parsers.docx_fixtures import build_docx


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_go_parent_child_chunks_keep_exact_source_and_parent_links() -> None:
    original = "信息安全事件应先报告疑似情况，再报告确认情况。" * 12
    document_ir = parse_package(
        build_package(_paragraph(original)), name="流程.docx"
    ).document_ir
    adapter = WeKnoraChunkerAdapter(
        WeKnoraChunkingPolicy(parent_size_chars=120, child_size_chars=40)
    )
    result = adapter.chunk(
        document_ir,
        ChunkingContext(
            chunker_fingerprint=adapter.fingerprint,
            index_revision_id=deterministic_id("irev", "weknora-test"),
        ),
    )

    assert len(result.chunks) > 1
    assert result.parent_passages
    assert result.report.source_span_coverage == 1.0
    by_id = {chunk.chunk_id: chunk for chunk in result.chunks}
    for parent in result.parent_passages:
        assert all(
            by_id[chunk_id].parent_passage_id == parent.parent_passage_id
            for chunk_id in parent.child_chunk_ids
        )
    for chunk in result.chunks:
        assert dict(chunk.metadata)["upstream_commit"] == (
            "1edcd54b43606d9079bb36650efe3f68707a79ea"
        )
        for span in chunk.source_spans:
            if span.span_type is SourceSpanKind.SEPARATOR:
                continue
            node = next(
                item
                for item in document_ir.nodes
                if item.node_id == span.node_id
            )
            assert node.text_payload is not None
            assert (
                chunk.citation_text[span.chunk_start_char : span.chunk_end_char]
                == node.text_payload.exact_text[
                    span.source_start_char : span.source_end_char
                ]
            )


def test_parsed_markdown_spans_point_to_derived_artifact() -> None:
    project_id = deterministic_id("prj", "weknora-md")
    kb_id = deterministic_id("kb", project_id, "weknora-md")
    document_id = deterministic_id("doc", kb_id, "weknora-md")
    router = WeKnoraDocumentRouter(
        DocxOoxmlV4Parser(), extensions=frozenset({".md"}), endpoint=""
    )
    parsed = router.parse(
        ParseSource(
            media_type="text/markdown",
            display_name="说明.md",
            extension=".md",
            content=(
                "# 标题\n\n" + "疑似事件先报告。确认事件再报告。" * 30
            ).encode(),
        ),
        ParsingPolicy(),
        ParseContext(
            document=DocumentRef(
                project_id=project_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                display_name="说明.md",
            )
        ),
    )
    adapter = WeKnoraChunkerAdapter()
    result = adapter.chunk(
        parsed.document_ir,
        ChunkingContext(
            chunker_fingerprint=adapter.fingerprint,
            index_revision_id=deterministic_id("irev", "weknora-md"),
        ),
    )

    assert result.report.source_span_coverage == 1.0
    assert result.parent_passages
    artifact_id = dict(parsed.document_ir.metadata)["parsed_artifact_id"]
    artifact = next(
        item for item in parsed.artifacts if item.artifact_id == artifact_id
    )
    artifact_text = artifact.content.decode("utf-8")
    nodes = {node.node_id: node for node in parsed.document_ir.nodes}
    found = False
    for chunk in result.chunks:
        for span in chunk.source_spans:
            if span.span_type is not SourceSpanKind.PARSED_ARTIFACT_TEXT:
                continue
            found = True
            assert (
                dict(span.metadata)["parsed_artifact_id"]
                == artifact.artifact_id
            )
            node = nodes[span.node_id]
            start = dict(node.metadata)["parsed_artifact_start_char"]
            assert isinstance(start, int)
            assert span.source_start_char is not None
            assert span.source_end_char is not None
            absolute_start = start + span.source_start_char
            absolute_end = start + span.source_end_char
            assert (
                chunk.citation_text[span.chunk_start_char : span.chunk_end_char]
                == artifact_text[absolute_start:absolute_end]
            )
    assert found


def test_long_table_cell_recovers_parent_with_complete_source() -> None:
    cell_text = "Ⅱ级事件先报告疑似情况，再按规定报告确认结果。" * 50
    table = (
        "<w:tbl><w:tr>"
        "<w:tc><w:p><w:r><w:t>报送方式</w:t></w:r></w:p></w:tc>"
        f"<w:tc><w:p><w:r><w:t>{cell_text}</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>"
    )
    document_ir = parse_package(
        build_docx(table), name="长表格.docx"
    ).document_ir
    adapter = WeKnoraChunkerAdapter()
    result = adapter.chunk(
        document_ir,
        ChunkingContext(
            chunker_fingerprint=adapter.fingerprint,
            index_revision_id=deterministic_id("irev", "weknora-table"),
        ),
    )

    assert len(result.chunks) > 1
    assert len(result.parent_passages) == 1
    assert "报送方式" in result.parent_passages[0].citation_text
    assert cell_text in result.parent_passages[0].citation_text
    assert result.report.source_span_coverage == 1.0
    assert result.report.represented_table_cell_count == 2
