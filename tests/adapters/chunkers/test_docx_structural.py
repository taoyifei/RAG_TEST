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
        item for item in _cases()
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
    assert tuple(node.node_id for node in first.nodes) == tuple(
        node.node_id for node in repeated.nodes
    ) == tuple(node.node_id for node in renamed.nodes)
    assert tuple(chunk.chunk_id for chunk in first_chunks) == tuple(
        chunk.chunk_id for chunk in _chunk(repeated).chunks
    ) == tuple(chunk.chunk_id for chunk in _chunk(renamed).chunks)
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
                    update={
                        "source_anchor": nodes_by_id[span.node_id].anchor
                    }
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
