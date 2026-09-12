from __future__ import annotations

import hashlib
import random

import pytest

from rag_app.adapters.chunkers import DocxStructuralChunker
from rag_app.adapters.chunkers.docx_structural.atoms import (
    AtomicUnit,
    RunPlan,
    SourceFragment,
)
from rag_app.adapters.chunkers.docx_structural.packing import pack_run
from rag_app.adapters.chunkers.docx_structural.rendering import (
    render_atoms,
    separator_fragment,
)
from rag_app.adapters.chunkers.docx_structural.reports import (
    build_chunking_report,
)
from rag_app.adapters.chunkers.docx_structural.validation import (
    quote_is_publishable,
    validate_chunks,
)
from rag_app.adapters.tokenizers import (
    ConservativeEstimatedTokenCounter,
    DeterministicUtf8TokenCounter,
)
from rag_app.core.errors import RagError
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    ChunkingPolicy,
    ChunkingResult,
    ChunkRole,
    DocumentIR,
    NodeKind,
    SourceAnchor,
    SourceSpanKind,
    StoryKind,
)
from rag_app.core.ports import TokenCounterPort
from tests.adapters.parsers.docx.fixtures import (
    build_package,
    context,
    parse_package,
)
from tests.fixtures.docx_v4.generate_fixtures import _cases


def _parse(name: str) -> DocumentIR:
    case = next(item for item in _cases() if item.name == name)
    return parse_package(case.content, name=name, **case.policy).document_ir


def _chunk(
    document_ir: DocumentIR,
    *,
    policy: ChunkingPolicy | None = None,
    counter: TokenCounterPort | None = None,
) -> ChunkingResult:
    chunker = DocxStructuralChunker(policy, counter)
    return chunker.chunk(
        document_ir,
        ChunkingContext(
            chunker_fingerprint=chunker.fingerprint,
            index_revision_id=deterministic_id(
                "irev",
                document_ir.version.document_version_id,
                chunker.fingerprint,
            ),
        ),
    )


def _paragraph(text: str) -> str:
    """构造不带标题样式的合成段落。"""
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_all_p04_fixtures_respect_parser_boundary() -> None:
    parsed_count = 0
    rejected_count = 0
    counter = DeterministicUtf8TokenCounter()
    for case in _cases():
        try:
            document_ir = parse_package(
                case.content,
                name=case.name,
                **case.policy,
            ).document_ir
        except RagError:
            rejected_count += 1
            continue
        parsed_count += 1
        result = _chunk(document_ir)
        assert result.report.stable_id_duplicate_count == 0
        assert result.report.cross_boundary_violations == 0
        for chunk in result.chunks:
            assert counter.count(chunk.citation_text).count <= 512
            assert counter.count(chunk.embedding_text).count <= 512
            assert chunk.source_spans[0].chunk_start_char == 0
            assert chunk.source_spans[-1].chunk_end_char == len(
                chunk.citation_text
            )
    assert parsed_count == 18
    assert rejected_count == 2


def test_flat_numbered_headings_propagate_auditable_dependencies() -> None:
    blocks = "".join(
        _paragraph(text)
        for text in (
            "4 部门职责",
            "4.1 合成经理",
            "a）制定公开合成质量目标并组织落实。",
            "b）协调公开合成资源并跟踪交付风险。",
            "c）批准公开合成制度并检查执行结果。",
            "d）组织公开合成复盘并推动持续改进。",
            "4.2 合成财务",
            "在合成经理领导下，负责公开合成账目核对。",
        )
    )
    document_ir = parse_package(
        build_package(blocks), name="flat-numbered-headings.docx"
    ).document_ir
    result = _chunk(
        document_ir,
        policy=ChunkingPolicy(
            target_tokens=180,
            hard_max_tokens=240,
            overlap_cap_tokens=24,
            min_tail_tokens=32,
            profile_hard_cap=240,
        ),
    )
    nodes_by_text = {node.text.strip(): node for node in document_ir.nodes}
    manager_node = nodes_by_text["4.1 合成经理"]
    department_node = nodes_by_text["4 部门职责"]
    manager_duty_nodes = {
        nodes_by_text[text].node_id
        for text in (
            "a）制定公开合成质量目标并组织落实。",
            "b）协调公开合成资源并跟踪交付风险。",
            "c）批准公开合成制度并检查执行结果。",
            "d）组织公开合成复盘并推动持续改进。",
        )
    }
    manager_chunks = [
        chunk
        for chunk in result.chunks
        if manager_duty_nodes
        & {span.node_id for span in chunk.source_spans if span.node_id}
    ]

    assert len(manager_chunks) >= 2
    assert all(
        chunk.heading_path == ("4 部门职责", "4.1 合成经理")
        for chunk in manager_chunks
    )
    assert all(
        tuple(
            dependency.source_node_id
            for dependency in chunk.context_dependencies
        )
        == (department_node.node_id, manager_node.node_id)
        for chunk in manager_chunks
    )
    assert all(
        {dependency.origin for dependency in chunk.context_dependencies}
        == {"inferred_numbered_heading"}
        for chunk in manager_chunks
    )
    finance_chunk = next(
        chunk
        for chunk in result.chunks
        if "公开合成账目核对" in chunk.citation_text
    )
    assert finance_chunk.heading_path == ("4 部门职责", "4.2 合成财务")
    assert result.report.source_span_coverage == 1.0
    assert result.report.missing_source_chars == 0
    assert (
        sum("4.1 合成经理" in chunk.citation_text for chunk in result.chunks)
        == 1
    )

    repeated = _chunk(
        document_ir,
        policy=ChunkingPolicy(
            target_tokens=180,
            hard_max_tokens=240,
            overlap_cap_tokens=24,
            min_tail_tokens=32,
            profile_hard_cap=240,
        ),
    )
    assert tuple(chunk.chunk_id for chunk in repeated.chunks) == tuple(
        chunk.chunk_id for chunk in result.chunks
    )


def test_implicit_heading_rejects_toc_clusters_clauses_and_measurements() -> (
    None
):
    texts = (
        "1、总则",
        "2、安全管理委员会",
        "3、消防安全管理制度",
        "以下为正文",
        "1、总则",
        "为加强公开合成安全管理，制定本制度。",
        "4.1.2 专用量具的制作",
        "4.1.2.1采购部根据制作计划联系合成供应商",
        "4.1.4.4 验收合格的专用量具在编号后加刻管理编号后办理入库手续",
        "3.7.2 在制品质量检验由品质部负责检验。\ue004",
        "100 分（暂定）",
        "20%",
    )
    document_ir = parse_package(
        build_package("".join(_paragraph(text) for text in texts)),
        name="heading-negative-cases.docx",
    ).document_ir
    result = _chunk(document_ir)
    nodes = [
        node for node in document_ir.nodes if node.text.strip() in set(texts)
    ]
    first_toc_ids = {node.node_id for node in nodes[:3]}
    actual_heading = next(
        node for node in nodes[3:] if node.text.strip() == "1、总则"
    )
    clause = next(
        node for node in nodes if node.text.strip().startswith("4.1.2.1采购部")
    )
    long_clause = next(
        node for node in nodes if node.text.strip().startswith("4.1.4.4 验收")
    )
    legacy_clause = next(
        node for node in nodes if node.text.strip().startswith("3.7.2 在制品")
    )
    dependency_ids = {
        dependency.source_node_id
        for chunk in result.chunks
        for dependency in chunk.context_dependencies
    }
    clause_chunk = next(
        chunk
        for chunk in result.chunks
        if clause.node_id
        in {span.node_id for span in chunk.source_spans if span.node_id}
    )

    assert first_toc_ids.isdisjoint(dependency_ids)
    assert actual_heading.node_id in dependency_ids
    assert clause.node_id not in dependency_ids
    assert long_clause.node_id not in dependency_ids
    assert legacy_clause.node_id not in dependency_ids
    assert clause_chunk.heading_path[-1] == "4.1.2 专用量具的制作"
    assert all(
        "100 分" not in heading and "20%" not in heading
        for chunk in result.chunks
        for heading in chunk.heading_path
    )
    assert result.report.source_span_coverage == 1.0


def test_toc_cluster_stops_at_numbering_reset_without_body_separator() -> None:
    texts = (
        "1、总则",
        "2、安全管理委员会",
        "3、消防安全管理制度",
        "4、安全生产教育制度",
        "1、总则",
        "本制度用于公开合成验证。",
    )
    document_ir = parse_package(
        build_package("".join(_paragraph(text) for text in texts)),
        name="toc-followed-by-body.docx",
    ).document_ir
    result = _chunk(document_ir)
    ordered = [
        node
        for node in document_ir.nodes
        if node.kind is NodeKind.PARAGRAPH and node.text.strip()
    ]
    dependency_ids = {
        dependency.source_node_id
        for chunk in result.chunks
        for dependency in chunk.context_dependencies
    }

    assert {node.node_id for node in ordered[:4]}.isdisjoint(dependency_ids)
    assert ordered[4].node_id in dependency_ids
    body_chunk = next(
        chunk
        for chunk in result.chunks
        if "公开合成验证" in chunk.citation_text
    )
    assert body_chunk.heading_path == ("1、总则",)


def test_mixed_flat_numbering_preserves_parent_child_series() -> None:
    texts = (
        "1、目的",
        "目的正文。",
        "2.范围",
        "范围正文。",
        "3、职责",
        "职责正文。",
        "4、安全生产责任制度",
        "一.合成总经理的安全职责",
        "组织公开合成安全检查。",
        "二.合成生产经理的安全职责",
        "落实公开合成现场措施。",
        "5、安全生产检查制度",
        "1.安全检查的内容",
        "检查公开合成设备。",
        "2.安全检查的形式",
        "开展公开合成巡检。",
        "6、附则",
        "附则正文。",
    )
    result = _chunk(
        parse_package(
            build_package("".join(_paragraph(text) for text in texts)),
            name="mixed-flat-numbering.docx",
        ).document_ir
    )
    chunks_by_phrase = {
        phrase: next(
            chunk for chunk in result.chunks if phrase in chunk.citation_text
        )
        for phrase in (
            "范围正文",
            "组织公开合成安全检查",
            "落实公开合成现场措施",
            "检查公开合成设备",
            "开展公开合成巡检",
            "附则正文",
        )
    }

    assert chunks_by_phrase["范围正文"].heading_path == ("2.范围",)
    assert chunks_by_phrase["组织公开合成安全检查"].heading_path == (
        "4、安全生产责任制度",
        "一.合成总经理的安全职责",
    )
    assert chunks_by_phrase["落实公开合成现场措施"].heading_path == (
        "4、安全生产责任制度",
        "二.合成生产经理的安全职责",
    )
    assert chunks_by_phrase["检查公开合成设备"].heading_path == (
        "5、安全生产检查制度",
        "1.安全检查的内容",
    )
    assert chunks_by_phrase["开展公开合成巡检"].heading_path == (
        "5、安全生产检查制度",
        "2.安全检查的形式",
    )
    assert chunks_by_phrase["附则正文"].heading_path == ("6、附则",)


def test_chinese_top_level_can_contain_arabic_flat_subheadings() -> None:
    texts = (
        "一、目的",
        "目的正文。",
        "二、范围",
        "范围正文。",
        "三、内容",
        "1、入库验收",
        "执行公开合成入库检查。",
        "2、出库领发",
        "执行公开合成出库检查。",
        "四、附表",
        "附表正文。",
    )
    result = _chunk(
        parse_package(
            build_package("".join(_paragraph(text) for text in texts)),
            name="chinese-parent-arabic-child.docx",
        ).document_ir
    )
    inbound = next(
        chunk for chunk in result.chunks if "入库检查" in chunk.citation_text
    )
    outbound = next(
        chunk for chunk in result.chunks if "出库检查" in chunk.citation_text
    )
    appendix = next(
        chunk for chunk in result.chunks if "附表正文" in chunk.citation_text
    )

    assert inbound.heading_path == ("三、内容", "1、入库验收")
    assert outbound.heading_path == ("三、内容", "2、出库领发")
    assert appendix.heading_path == ("四、附表",)


def test_short_numbered_titles_keep_internal_comma_colon_and_question() -> None:
    texts = (
        "4、安全教育",
        "1.质量管理，技术培训教育制度",
        "培训公开合成正文。",
        "2.年度整体考核：",
        "考核公开合成正文。",
        "3.如何识别现场风险？",
        "风险公开合成正文。",
        "5、附则",
        "附则公开合成正文。",
    )
    result = _chunk(
        parse_package(
            build_package("".join(_paragraph(text) for text in texts)),
            name="punctuated-numbered-headings.docx",
        ).document_ir
    )

    assert next(
        chunk
        for chunk in result.chunks
        if "培训公开合成" in chunk.citation_text
    ).heading_path == ("4、安全教育", "1.质量管理，技术培训教育制度")
    assert next(
        chunk
        for chunk in result.chunks
        if "考核公开合成" in chunk.citation_text
    ).heading_path == ("4、安全教育", "2.年度整体考核：")
    assert next(
        chunk
        for chunk in result.chunks
        if "风险公开合成" in chunk.citation_text
    ).heading_path == ("4、安全教育", "3.如何识别现场风险？")
    assert next(
        chunk
        for chunk in result.chunks
        if "附则公开合成" in chunk.citation_text
    ).heading_path == ("5、附则",)


def test_missing_numbered_parent_does_not_attach_to_prior_root() -> None:
    texts = (
        "3、职责",
        "职责公开合成正文。",
        "4.1总则",
        "总则公开合成正文。",
        "4.2质量管理",
        "质量公开合成正文。",
        "5、附则",
        "附则公开合成正文。",
    )
    result = _chunk(
        parse_package(
            build_package("".join(_paragraph(text) for text in texts)),
            name="missing-numbered-parent.docx",
        ).document_ir
    )

    assert next(
        chunk
        for chunk in result.chunks
        if "总则公开合成" in chunk.citation_text
    ).heading_path == ("4.1总则",)
    assert next(
        chunk
        for chunk in result.chunks
        if "质量公开合成" in chunk.citation_text
    ).heading_path == ("4.2质量管理",)
    assert next(
        chunk
        for chunk in result.chunks
        if "附则公开合成" in chunk.citation_text
    ).heading_path == ("5、附则",)


def test_chinese_numbered_subheading_is_relative_to_arabic_parent() -> None:
    document_ir = parse_package(
        build_package(
            "".join(
                _paragraph(text)
                for text in (
                    "6、安全生产责任制度",
                    "一.总经理的安全职责",
                    "组织公开合成安全检查并闭环隐患。",
                    "二.生产经理的安全职责",
                    "落实公开合成现场安全措施。",
                )
            )
        ),
        name="mixed-numbered-headings.docx",
    ).document_ir
    result = _chunk(document_ir)
    manager_chunk = next(
        chunk for chunk in result.chunks if "闭环隐患" in chunk.citation_text
    )
    production_chunk = next(
        chunk
        for chunk in result.chunks
        if "现场安全措施" in chunk.citation_text
    )

    assert manager_chunk.heading_path == (
        "6、安全生产责任制度",
        "一.总经理的安全职责",
    )
    assert production_chunk.heading_path == (
        "6、安全生产责任制度",
        "二.生产经理的安全职责",
    )
    assert all(
        len(chunk.context_dependencies) == len(chunk.heading_path)
        for chunk in (manager_chunk, production_chunk)
    )


def test_explicit_heading_dependency_keeps_document_structure_origin() -> None:
    document_ir = parse_package(
        build_package(
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            "<w:r><w:t>合成范围</w:t></w:r></w:p>"
            + _paragraph("公开合成正文。")
        ),
        name="explicit-heading-dependency.docx",
    ).document_ir
    chunk = next(
        chunk
        for chunk in _chunk(document_ir).chunks
        if "公开合成正文" in chunk.citation_text
    )

    assert chunk.heading_path == ("合成范围",)
    assert len(chunk.context_dependencies) == 1
    assert chunk.context_dependencies[0].origin == "document_heading"
    assert chunk.context_dependencies[0].source_node_id == next(
        node.node_id for node in document_ir.nodes if node.text == "合成范围"
    )


def test_empty_explicit_heading_does_not_create_context_dependency() -> None:
    """空标题样式段落不能成为后续正文的标题路径或来源依赖。"""
    document_ir = parse_package(
        build_package(
            '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr></w:p>'
            + _paragraph("版本修订记录")
            + _paragraph("公开合成版本说明。")
        ),
        name="empty-explicit-heading.docx",
    ).document_ir

    chunks = _chunk(document_ir).chunks

    assert chunks
    assert all("" not in chunk.heading_path for chunk in chunks)
    assert all(not chunk.context_dependencies for chunk in chunks)


def test_inferred_children_follow_numbered_explicit_parent() -> None:
    document_ir = parse_package(
        build_package(
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            "<w:r><w:t>4 部门职责</w:t></w:r></w:p>"
            + _paragraph("4.1 合成经理")
            + _paragraph("制定公开合成质量目标。")
            + _paragraph("4.2 合成财务")
            + _paragraph("核对公开合成账目。")
        ),
        name="mixed-explicit-inferred-headings.docx",
    ).document_ir
    result = _chunk(document_ir)
    manager = next(
        chunk for chunk in result.chunks if "质量目标" in chunk.citation_text
    )
    finance = next(
        chunk for chunk in result.chunks if "合成账目" in chunk.citation_text
    )

    assert manager.heading_path == ("4 部门职责", "4.1 合成经理")
    assert finance.heading_path == ("4 部门职责", "4.2 合成财务")
    assert tuple(
        dependency.origin for dependency in manager.context_dependencies
    ) == ("document_heading", "inferred_numbered_heading")


def test_heading_context_is_not_counted_as_missing_citable_text() -> None:
    result = _chunk(_parse("01-headings-custom-outline.docx"))

    assert result.report.source_span_coverage == 1.0
    assert result.report.missing_source_chars == 0


def test_note_relationship_targets_are_represented_by_child_chunks() -> None:
    result = _chunk(_parse("11-footnotes-endnotes.docx"))

    assert result.report.orphan_note_count == 0
    assert result.report.orphan_relation_count == 0
    assert result.report.missing_note_ref_count == 0


def test_list_numbering_and_restart_groups() -> None:
    result = _chunk(_parse("03-numbering-restart-override.docx"))
    assert len(result.chunks) == 2
    assert all(chunk.role is ChunkRole.LIST for chunk in result.chunks)
    assert all(
        any(
            span.span_type is SourceSpanKind.DERIVED_NUMBERING
            for span in chunk.source_spans
        )
        for chunk in result.chunks
    )


def test_table_merge_and_nested_table_keep_real_source_relationships() -> None:
    merged = _chunk(_parse("07-table-gridspan-vmerge.docx"))
    assert any(
        span.span_type is SourceSpanKind.REPEATED_CONTEXT
        for chunk in merged.chunks
        for span in chunk.source_spans
    )
    nested = _chunk(_parse("09-table-nested-list-image.docx"))
    outer = next(chunk for chunk in nested.chunks if chunk.child_group_ids)
    assert outer.role is ChunkRole.TABLE
    assert all(group.startswith("group_") for group in outer.child_group_ids)


def test_table_keeps_middle_empty_column_and_continuous_header_rows() -> None:
    table = """
<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
  <w:tr><w:trPr><w:tblHeader/></w:trPr>
    <w:tc><w:p><w:r><w:t>H1</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>H2</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>H3</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:trPr><w:tblHeader/></w:trPr>
    <w:tc><w:p><w:r><w:t>S1</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>S2</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>S3</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>
    <w:tc><w:p/></w:tc>
    <w:tc><w:p><w:r><w:t>C</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
"""
    document_ir = parse_package(
        build_package(table), name="empty-column.docx"
    ).document_ir

    result = _chunk(document_ir)
    combined_citation = "\n".join(
        chunk.citation_text for chunk in result.chunks
    )
    combined_embedding = "\n".join(
        chunk.embedding_text for chunk in result.chunks
    )

    assert "A |  | C" in combined_citation
    assert "<EMPTY>" not in combined_citation
    assert "[列2] <EMPTY>" in combined_embedding
    assert "[列1] H1 | [列2] H2 | [列3] H3" in combined_embedding
    assert "[列1] S1 | [列2] S2 | [列3] S3" in combined_embedding
    assert result.report.represented_table_cell_count == 9


def test_notes_images_textbox_and_furniture_default_policy() -> None:
    notes = _chunk(_parse("11-footnotes-endnotes.docx"))
    assert sum(chunk.role is ChunkRole.NOTE for chunk in notes.chunks) == 2
    body = next(chunk for chunk in notes.chunks if chunk.role is ChunkRole.TEXT)
    assert len(body.note_refs) == 2
    images = _chunk(_parse("14-images-inline-anchor-vml.docx"))
    assert any(
        chunk.role is ChunkRole.IMAGE_METADATA for chunk in images.chunks
    )
    textbox = _chunk(_parse("16-textbox.docx"))
    assert {chunk.role for chunk in textbox.chunks} == {ChunkRole.TEXT_BOX}
    furniture = _chunk(_parse("10-sections-headers-footers.docx"))
    assert ChunkRole.HEADER_FOOTER not in {
        chunk.role for chunk in furniture.chunks
    }
    assert "HEADER_FOOTER_METADATA_ONLY" in furniture.report.warnings


def test_rename_is_stable_but_content_and_policy_change_ids() -> None:
    case = next(
        item
        for item in _cases()
        if item.name == "03-numbering-restart-override.docx"
    )
    original = parse_package(
        case.content,
        name=case.name,
        **case.policy,
    ).document_ir
    renamed = parse_package(
        case.content,
        name="renamed.docx",
        **case.policy,
    ).document_ir
    original_ids = tuple(chunk.chunk_id for chunk in _chunk(original).chunks)
    renamed_ids = tuple(chunk.chunk_id for chunk in _chunk(renamed).chunks)
    assert original_ids == renamed_ids
    changed_policy = ChunkingPolicy(target_tokens=320)
    policy_ids = tuple(
        chunk.chunk_id
        for chunk in _chunk(original, policy=changed_policy).chunks
    )
    assert policy_ids != original_ids
    changed_content = _chunk(_parse("02-numbering-multilevel.docx"))
    changed_ids = tuple(chunk.chunk_id for chunk in changed_content.chunks)
    assert changed_ids != original_ids


def test_document_identity_scopes_version_node_and_chunk_ids() -> None:
    case = next(
        item
        for item in _cases()
        if item.name == "03-numbering-restart-override.docx"
    )
    first_context = context(document_id=f"doc_{'1' * 32}")
    second_context = context(document_id=f"doc_{'2' * 32}")
    first = parse_package(
        case.content,
        name=case.name,
        parse_context=first_context,
    ).document_ir
    repeated = parse_package(
        case.content,
        name=case.name,
        parse_context=first_context,
    ).document_ir
    renamed = parse_package(
        case.content,
        name="renamed.docx",
        parse_context=first_context,
    ).document_ir
    other_document = parse_package(
        case.content,
        name=case.name,
        parse_context=second_context,
    ).document_ir

    first_chunks = _chunk(first).chunks
    assert first.version == repeated.version == renamed.version
    assert (
        tuple(node.node_id for node in first.nodes)
        == tuple(node.node_id for node in repeated.nodes)
        == tuple(node.node_id for node in renamed.nodes)
    )
    assert (
        tuple(chunk.chunk_id for chunk in first_chunks)
        == tuple(chunk.chunk_id for chunk in _chunk(repeated).chunks)
        == tuple(chunk.chunk_id for chunk in _chunk(renamed).chunks)
    )
    assert first.version != other_document.version
    assert tuple(node.node_id for node in first.nodes) != tuple(
        node.node_id for node in other_document.nodes
    )
    assert tuple(chunk.chunk_id for chunk in first_chunks) != tuple(
        chunk.chunk_id for chunk in _chunk(other_document).chunks
    )
    assert first.source.blob_ref == other_document.source.blob_ref


def test_estimated_counter_applies_margin_and_quote_validation() -> None:
    counter = ConservativeEstimatedTokenCounter(safety_margin=0.15)
    result = _chunk(
        _parse("04-hyperlinks-bookmarks-fields.docx"),
        counter=counter,
    )
    assert result.chunks
    assert all(chunk.token_count_is_estimate for chunk in result.chunks)
    first = result.chunks[0]
    citable = next(span for span in first.source_spans if span.is_citable)
    assert quote_is_publishable(
        first,
        citable.chunk_start_char,
        citable.chunk_end_char,
    )


def test_report_and_validator_detect_adversarial_chunk_corruption() -> None:
    document_ir = parse_package(
        build_package(
            "<w:p><w:r><w:t>甲</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>乙</w:t></w:r></w:p>"
        ),
        name="adversarial.docx",
    ).document_ir
    result = _chunk(document_ir)
    assert len(result.chunks) == 1
    valid = result.chunks[0]

    missing_span = valid.model_copy(
        update={"source_spans": valid.source_spans[:-1]}
    )
    missing_report = build_chunking_report(
        (missing_span,),
        document_ir,
        ChunkingPolicy(),
        elapsed_seconds=0.0,
    )
    assert missing_report.missing_source_chars > 0
    assert missing_report.source_span_coverage < 1.0

    repeated_source = valid.model_copy(
        update={
            "source_spans": (
                *valid.source_spans,
                next(span for span in valid.source_spans if span.is_citable),
            )
        }
    )
    repeated_report = build_chunking_report(
        (repeated_source,),
        document_ir,
        ChunkingPolicy(),
        elapsed_seconds=0.0,
    )
    assert repeated_report.duplicated_citable_chars > 0

    updated_nodes = tuple(
        node.model_copy(
            update={
                "anchor": node.anchor.model_copy(
                    update={"section_index": index}
                )
            }
        )
        for index, node in enumerate(document_ir.nodes)
    )
    nodes_by_id = {node.node_id: node for node in updated_nodes}
    cross_section = valid.model_copy(
        update={
            "source_spans": tuple(
                span.model_copy(
                    update={"source_anchor": nodes_by_id[span.node_id].anchor}
                )
                if span.node_id is not None
                else span
                for span in valid.source_spans
            )
        }
    )
    section_ir = document_ir.model_copy(update={"nodes": updated_nodes})
    cross_report = build_chunking_report(
        (cross_section,),
        section_ir,
        ChunkingPolicy(),
        elapsed_seconds=0.0,
    )
    assert cross_report.cross_section_violations == 1
    with pytest.raises(ValueError, match="Section"):
        validate_chunks(
            (cross_section,),
            section_ir,
            ChunkingPolicy(),
            DeterministicUtf8TokenCounter(),
        )

    missing_refs = valid.model_copy(
        update={
            "child_group_ids": (f"group_{'f' * 32}",),
            "note_refs": (f"node_{'e' * 32}",),
        }
    )
    refs_report = build_chunking_report(
        (missing_refs,),
        document_ir,
        ChunkingPolicy(),
        elapsed_seconds=0.0,
    )
    assert refs_report.missing_child_group_count == 1
    assert refs_report.missing_note_ref_count == 1
    with pytest.raises(ValueError, match="child group"):
        validate_chunks(
            (missing_refs,),
            document_ir,
            ChunkingPolicy(),
            DeterministicUtf8TokenCounter(),
        )


@pytest.mark.parametrize("seed", range(10))
def test_randomized_long_text_always_progresses_and_preserves_order(
    seed: int,
) -> None:
    randomizer = random.Random(seed)  # noqa: S311
    case = next(
        item
        for item in _cases()
        if item.name == "10-sections-headers-footers.docx"
    )
    document_ir = parse_package(
        case.content,
        name=case.name,
        **case.policy,
    ).document_ir
    words = ["甲", "乙", "Gamma", "P00001", "GB/T 1234-2025"]
    text = "。".join(randomizer.choice(words) for _ in range(200)) + "。"
    paragraph = next(
        node
        for node in document_ir.nodes
        if node.kind is NodeKind.PARAGRAPH
        and node.parent_node_id is None
        and node.text_payload is not None
    )
    payload = paragraph.text_payload.model_copy(
        update={
            "exact_text": text,
            "semantic_text": text,
            "exact_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "semantic_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    )
    node = paragraph.model_copy(update={"text_payload": payload})
    synthetic = document_ir.model_copy(
        update={
            "nodes": (node,),
            "root_node_ids": (node.node_id,),
            "parse_report": document_ir.parse_report.model_copy(
                update={"node_count": 1}
            ),
        }
    )
    result = _chunk(
        synthetic,
        policy=ChunkingPolicy(
            target_tokens=96,
            hard_max_tokens=128,
            overlap_cap_tokens=24,
            min_tail_tokens=16,
            profile_hard_cap=128,
        ),
    )
    assert len(result.chunks) > 1
    assert all(chunk.token_count <= 128 for chunk in result.chunks)
    assert all(chunk.citation_text for chunk in result.chunks)
    assert any(
        span.span_type is SourceSpanKind.REPEATED_CONTEXT
        for chunk in result.chunks[1:]
        for span in chunk.source_spans
    )


@pytest.mark.parametrize(
    "role",
    (ChunkRole.TEXT, ChunkRole.LIST, ChunkRole.TABLE),
)
@pytest.mark.parametrize("seed", range(5))
def test_randomized_short_structures_keep_source_order(
    role: ChunkRole,
    seed: int,
) -> None:
    randomizer = random.Random(seed)  # noqa: S311
    atoms: list[AtomicUnit] = []
    expected_node_ids: list[str] = []
    for index in range(6):
        node_id = deterministic_id("node", role.value, seed, index)
        expected_node_ids.append(node_id)
        anchor = SourceAnchor(
            part_uri="/word/document.xml",
            story_kind=StoryKind.BODY,
            structural_path=(role.value, f"item:{index}"),
            ordinal=index,
        )
        text = randomizer.choice(("甲", "Beta", "P00001", "值 42"))
        fragments: list[SourceFragment] = []
        if role is ChunkRole.LIST:
            fragments.append(
                SourceFragment(
                    text=f"{index + 1}. ",
                    span_type=SourceSpanKind.DERIVED_NUMBERING,
                    node_id=node_id,
                    source_anchor=anchor,
                )
            )
        fragments.append(
            SourceFragment(
                text=text,
                span_type=SourceSpanKind.ORIGINAL_TEXT,
                node_id=node_id,
                source_anchor=anchor,
                source_start_char=0,
                source_end_char=len(text),
            )
        )
        if role is ChunkRole.TABLE:
            fragments.extend(
                (
                    separator_fragment(" | "),
                    SourceFragment(
                        text=str(index),
                        span_type=SourceSpanKind.ORIGINAL_TEXT,
                        node_id=node_id,
                        source_anchor=anchor,
                        source_start_char=0,
                        source_end_char=1,
                    ),
                )
            )
        atoms.append(
            AtomicUnit(
                unit_id=node_id,
                role=role,
                parent_node_id=node_id,
                section_id="section_property",
                neighbor_group_id="group_property",
                heading_path=(),
                fragments=tuple(fragments),
            )
        )
    run = RunPlan(
        run_id="run_property",
        role=role,
        section_id="section_property",
        neighbor_group_id="group_property",
        heading_path=(),
        atoms=tuple(atoms),
    )
    packs = pack_run(
        run,
        document_title="property.docx",
        policy=ChunkingPolicy(
            target_tokens=64,
            hard_max_tokens=128,
            overlap_cap_tokens=16,
            min_tail_tokens=8,
            profile_hard_cap=128,
        ),
        token_counter=DeterministicUtf8TokenCounter(),
    )
    observed = [atom.unit_id for pack in packs for atom in pack]
    assert observed == expected_node_ids
    for pack in packs:
        rendered = render_atoms(pack)
        assert rendered.spans[0].chunk_start_char == 0
        assert rendered.spans[-1].chunk_end_char == len(rendered.text)
