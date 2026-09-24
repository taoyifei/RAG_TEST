"""固定 Go 分块器与本方来源合同的定向测试。"""

from __future__ import annotations

import json
import subprocess
from itertools import pairwise

import pytest

from rag_app.adapters.chunkers.weknora import chunker as chunker_module
from rag_app.adapters.chunkers.weknora.chunker import WeKnoraChunkerAdapter
from rag_app.adapters.chunkers.weknora.reading_domain import (
    plan_reading_domains,
)
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


def test_markdown_reading_domain_matches_fixed_go_wrapper() -> None:
    """标题、围栏、表格与 Unicode 坐标在入库和直连 Go 间一致。"""
    source_text = (
        "# 一级😀\r\n\r\n"
        "```python\r\n# 围栏内不是标题\r\n```\r\n\r\n"
        "## 二级\r\n\r\n"
        "| 字段 | 值 |\r\n| --- | --- |\r\n| 期限 | 七天 |\r\n\r\n"
        "- 第一步\r\n- 第二步\r\n"
    )
    project_id = deterministic_id("prj", "weknora-diff")
    kb_id = deterministic_id("kb", project_id, "weknora-diff")
    document_id = deterministic_id("doc", kb_id, "weknora-diff")
    parser = WeKnoraDocumentRouter(
        DocxOoxmlV4Parser(), extensions=frozenset({".md"}), endpoint=""
    )
    parsed = parser.parse(
        ParseSource(
            media_type="text/markdown",
            display_name="语法.md",
            extension=".md",
            content=source_text.encode("utf-8"),
        ),
        ParsingPolicy(),
        ParseContext(
            document=DocumentRef(
                project_id=project_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
                display_name="语法.md",
            )
        ),
    )
    assert (
        "".join(
            node.text_payload.exact_text
            for node in parsed.document_ir.nodes
            if node.text_payload is not None
        )
        == source_text
    )
    headings = [
        dict(node.metadata)["heading_level"]
        for node in parsed.document_ir.nodes
        if node.kind.value == "heading"
    ]
    assert headings == [1, 2]
    assert any(
        "# 围栏内不是标题" in node.text_payload.exact_text
        and dict(node.metadata)["reading_syntax"] == "code"
        for node in parsed.document_ir.nodes
        if node.text_payload is not None
    )
    domains = plan_reading_domains(parsed.document_ir, WeKnoraChunkingPolicy())
    assert len(domains) == 1
    assert domains[0].view.text == source_text.replace("\r\n", "\n")
    adapter = WeKnoraChunkerAdapter()
    preview = adapter.preview(
        parsed.document_ir,
        ChunkingContext(
            chunker_fingerprint=adapter.fingerprint,
            index_revision_id=deterministic_id("irev", "weknora-diff"),
        ),
    )
    request = {
        "normalized_text": domains[0].view.text,
        "mode": "parent_child",
        "strategy": adapter.policy.strategy,
        "chunk_size": adapter.policy.chunk_size_chars,
        "overlap": adapter.policy.overlap_chars,
        "parent_size": adapter.policy.parent_size_chars,
        "child_size": adapter.policy.child_size_chars,
        "token_limit": adapter.policy.upstream_token_limit,
        "language_hints": list(adapter.policy.language_hints),
    }
    direct = subprocess.run(  # noqa: S603
        [str(adapter.binary_path)],
        input=json.dumps(request, ensure_ascii=False).encode(),
        capture_output=True,
        check=True,
    )
    upstream = json.loads(direct.stdout)
    by_range = {
        (item["start"], item["end"]): item
        for item in upstream["children"]
        if item["content"].strip()
    }
    assert len(preview.result.chunks) == len(by_range)
    for chunk in preview.result.chunks:
        metadata = dict(chunk.metadata)
        item = by_range[
            (
                metadata["reading_view_start_char"],
                metadata["reading_view_end_char"],
            )
        ]
        assert chunk.citation_text == item["content"]
        expected = (
            item["context_header"] + "\n\n" + item["content"].strip()
            if item["context_header"]
            else item["content"].strip()
        )
        assert chunk.embedding_text == expected
    assert (
        preview.domains[0].selected_tier
        == (upstream["diagnostics"]["selected_tier"])
    )
    assert (
        list(preview.domains[0].tier_chain)
        == (upstream["diagnostics"]["tier_chain"])
    )
    assert preview.result.report.source_span_coverage == 1.0


def test_docx_headings_share_body_domain_across_old_sections() -> None:
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>第一章</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>第一段说明。</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
        "<w:r><w:t>第二节</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>第二段说明。</w:t></w:r></w:p>"
    )
    document_ir = parse_package(build_package(body)).document_ir
    domains = plan_reading_domains(document_ir, WeKnoraChunkingPolicy())
    body_domains = [item for item in domains if item.boundary == "docx_body"]
    assert len(body_domains) == 1
    assert "# 第一章" in body_domains[0].view.text
    assert "## 第二节" in body_domains[0].view.text
    assert "第一段说明。" in body_domains[0].view.text
    assert "第二段说明。" in body_domains[0].view.text


def test_bounded_long_domain_keeps_absolute_offsets_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大阅读域分段后，局部 Go 坐标仍能回到全域字符位置。"""
    monkeypatch.setattr(chunker_module, "_MAX_DOMAIN_CHARS", 700)
    document_ir = parse_package(
        build_package(_paragraph("说明含 emoji 🧭 与中文。" * 160)),
        name="长正文.docx",
    ).document_ir
    policy = WeKnoraChunkingPolicy(parent_size_chars=600, child_size_chars=180)
    domain = plan_reading_domains(document_ir, policy)[0]
    adapter = WeKnoraChunkerAdapter(policy)
    preview = adapter.preview(
        document_ir,
        ChunkingContext(
            chunker_fingerprint=adapter.fingerprint,
            index_revision_id=deterministic_id("irev", "bounded-domain"),
        ),
    )
    assert len(preview.domains) > 1
    assert preview.domains[0].start_char == 0
    assert preview.domains[-1].end_char == len(domain.view.text)
    assert all(
        left.end_char == right.start_char
        for left, right in pairwise(preview.domains)
    )
    for chunk in preview.result.chunks:
        metadata = dict(chunk.metadata)
        assert metadata["domain_piece_start_char"] == (
            metadata["domain_start_char"] + metadata["reading_view_start_char"]
        )
        assert metadata["domain_piece_end_char"] == (
            metadata["domain_start_char"] + metadata["reading_view_end_char"]
        )
    assert preview.result.report.source_span_coverage == 1.0
