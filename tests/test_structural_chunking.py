from rag_app.chunking import Chunker, ChunkerConfig, Utf8TokenCounter
from rag_app.contracts import (
    DocumentMetadata,
    Element,
    ElementKind,
    Locator,
    OcrState,
)

_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64
_SOURCE_ID = "src_" + "1" * 32
_METADATA = DocumentMetadata(
    document_status="active",
    authority_level="official",
    effective_from=None,
    effective_to=None,
)


def _paragraph(
    text: str,
    paragraph_index: int,
    *,
    heading_path: tuple[str, ...] = ("第一章", "范围"),
) -> Element:
    return Element(
        element_id=f"element-{paragraph_index}",
        kind=ElementKind.PARAGRAPH,
        text=text,
        locator=Locator(
            file_path="规范.docx",
            heading_path=heading_path,
            paragraph_index=paragraph_index,
            fragment=text[:240],
        ),
        content_sha256="a" * 64,
        ocr_state=OcrState.NOT_APPLICABLE,
    )


def _chunker() -> Chunker:
    return Chunker(
        ChunkerConfig(
            target_tokens=24,
            hard_max_tokens=32,
            overlap_tokens=6,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )


def test_heading_context_is_not_added_to_citation_text() -> None:
    paragraph = _paragraph("原文证据", 1)
    heading = Element(
        element_id="heading-1",
        kind=ElementKind.HEADING,
        text="范围",
        locator=Locator(
            file_path="规范.docx",
            heading_path=paragraph.locator.heading_path,
            heading_index=1,
            fragment="范围",
        ),
        content_sha256="a" * 64,
    )
    chunk = _chunker().chunk(
        source_id=_SOURCE_ID,
        doc_version="sha256:" + "a" * 64,
        elements=[heading, paragraph],
        metadata=_METADATA,
    )[0]

    assert chunk.text == "原文证据"
    assert chunk.embedding_text == "第一章 > 范围\n原文证据"
    assert chunk.pipeline_fingerprint == _PIPELINE_FINGERPRINT


def test_normal_structure_boundaries_do_not_overlap() -> None:
    chunks = _chunker().chunk(
        source_id=_SOURCE_ID,
        doc_version="sha256:" + "a" * 64,
        elements=[_paragraph("第一段", 1), _paragraph("第二段", 2)],
        metadata=_METADATA,
    )

    assert [chunk.text for chunk in chunks] == ["第一段\n\n第二段"]
    assert chunks[0].previous_chunk_id is None
    assert chunks[0].next_chunk_id is None


def test_table_groups_repeat_header_and_preserve_original_rows() -> None:
    table_text = "字段 | 值\n状态 | 有效\n责任人 | 张三\n期限 | 三天"
    table = Element(
        element_id="table-1",
        kind=ElementKind.TABLE,
        text=table_text,
        locator=Locator(
            file_path="规范.docx",
            heading_path=("第一章",),
            table_index=1,
            fragment=table_text,
        ),
        content_sha256="b" * 64,
    )

    chunks = _chunker().chunk(
        source_id=_SOURCE_ID,
        doc_version="sha256:" + "b" * 64,
        elements=[table],
        metadata=_METADATA,
    )

    assert len(chunks) >= 2
    assert all(chunk.text.startswith("字段 | 值\n") for chunk in chunks)
    assert all(
        Utf8TokenCounter().count(chunk.text) <= 32 for chunk in chunks
    )


def test_long_element_uses_overlap_but_never_exceeds_hard_max() -> None:
    chunks = _chunker().chunk(
        source_id=_SOURCE_ID,
        doc_version="sha256:" + "c" * 64,
        elements=[_paragraph("abcdefghijklmnopqrstuvwxyz1234567890", 1)],
        metadata=_METADATA,
    )

    assert len(chunks) >= 2
    assert "".join(chunk.text for chunk in chunks) == (
        "abcdefghijklmnopqrstuvwxyz1234567890"
    )
    assert chunks[0].text[-6:] != chunks[1].text[:6]
    assert all(
        Utf8TokenCounter().count(chunk.text) <= 32 for chunk in chunks
    )
