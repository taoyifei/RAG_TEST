from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from evaluation.chunking_ablation import (
    DEFAULT_SECTION_CANDIDATES,
    load_tuning_cases_only,
    parse_candidate,
    summarize_section_candidate,
)
from rag_app.chunking import Chunker, ChunkerConfig, Utf8TokenCounter
from rag_app.contracts import (
    Chunk,
    DocumentMetadata,
    Element,
    ElementKind,
    Locator,
)


def _element(
    element_id: str,
    kind: ElementKind,
    text: str,
    *,
    heading_index: int,
    paragraph_index: int | None = None,
) -> Element:
    """构造 section 消融使用的公开合成元素。

    Args:
        element_id: 元素稳定 ID。
        kind: 标题或段落类型。
        text: 公开合成文本。
        heading_index: 当前标题序号。
        paragraph_index: 可选段落序号。

    Returns:
        可交给真实 Chunker 的元素。

    """
    return Element(
        element_id=element_id,
        kind=kind,
        text=text,
        locator=Locator(
            file_path="synthetic.docx",
            heading_path=("公开标题",),
            heading_index=heading_index,
            paragraph_index=paragraph_index,
            fragment=text,
        ),
        content_sha256="a" * 64,
    )


def test_default_ablation_candidates_are_exactly_required() -> None:
    assert [
        (
            candidate.target_tokens,
            candidate.hard_max_tokens,
            candidate.overlap_tokens,
        )
        for candidate in DEFAULT_SECTION_CANDIDATES
    ] == [
        (256, 512, 32),
        (320, 512, 48),
        (384, 512, 64),
    ]
    assert parse_candidate("320/512/48").label == (
        "section-pack-v2-320-512-48"
    )


@pytest.mark.parametrize(
    "raw",
    ("", "320", "320/512", "0/512/48", "513/512/48", "320/512/513"),
)
def test_invalid_candidate_spec_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_candidate(raw)


def test_structural_report_uses_real_source_spans() -> None:
    elements = (
        _element(
            "heading-1",
            ElementKind.HEADING,
            "公开标题",
            heading_index=1,
        ),
        _element(
            "paragraph-1",
            ElementKind.PARAGRAPH,
            "第一段公开证据。",
            heading_index=1,
            paragraph_index=1,
        ),
        _element(
            "paragraph-2",
            ElementKind.PARAGRAPH,
            "第二段公开证据。",
            heading_index=1,
            paragraph_index=2,
        ),
    )
    config = ChunkerConfig(
        target_tokens=64,
        hard_max_tokens=96,
        overlap_tokens=16,
    )
    counter = Utf8TokenCounter()
    chunks = Chunker(
        config,
        counter,
        pipeline_fingerprint="sha256:" + "b" * 64,
    ).chunk(
        source_id="src_" + "c" * 32,
        doc_version="sha256:" + "d" * 64,
        elements=list(elements),
        metadata=DocumentMetadata(
            document_status="active",
            authority_level="official",
            effective_from=None,
            effective_to=None,
        ),
    )

    report = summarize_section_candidate(
        ((elements, cast(tuple[Chunk, ...], chunks)),),
        counter,
        config,
    )

    assert report["chunks"] == 1
    assert report["standalone_heading_chunks"] == 0
    assert report["cross_section_chunks"] == 0
    assert report["cross_neighbor_group_links"] == 0
    assert report["hard_max_violations"] == 0
    assert report["uncovered_source_elements"] == 0
    assert report["blank_chunks"] == 0
    assert report["duplicate_chunk_ids"] == 0
    assert report["quote_locator_contract_violations"] == 0
    assert report["ordinary_duplicate_source_character_ratio"] == 0.0


def test_tuning_loader_rejects_non_tuning_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Case:
        split = "holdout"

    monkeypatch.setattr(
        "evaluation.chunking_ablation.load_tuning_cases",
        lambda _: (cast(object, _Case()),),
    )

    with pytest.raises(ValueError, match="holdout"):
        load_tuning_cases_only(Path("dataset.json"))
