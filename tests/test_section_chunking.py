from itertools import pairwise

from rag_app.chunking import Chunker, ChunkerConfig, Utf8TokenCounter
from rag_app.contracts import (
    Chunk,
    ChunkRole,
    DocumentMetadata,
    Element,
    ElementKind,
    Locator,
    OcrState,
)

_SOURCE_ID = "src_" + "1" * 32
_DOC_VERSION = "sha256:" + "a" * 64
_PIPELINE = "sha256:" + "f" * 64
_METADATA = DocumentMetadata(
    document_status="active",
    authority_level="official",
    effective_from=None,
    effective_to=None,
)


def _element(  # noqa: PLR0913
    element_id: str,
    kind: ElementKind,
    text: str,
    *,
    heading_path: tuple[str, ...] = (),
    heading_index: int | None = None,
    paragraph_index: int | None = None,
    table_index: int | None = None,
    image_index: int | None = None,
    list_level: int | None = None,
    ocr_state: OcrState = OcrState.NOT_APPLICABLE,
    ocr_confidence: float | None = None,
) -> Element:
    """构造 section chunking 测试元素。

    Args:
        element_id: 稳定元素 ID。
        kind: 文档元素类型。
        text: 原始元素文本。
        heading_path: 当前完整标题路径。
        heading_index: 当前标题序号。
        paragraph_index: 可选段落序号。
        table_index: 可选表格序号。
        image_index: 可选图片序号。
        list_level: 可选列表层级。
        ocr_state: 图片 OCR 状态。
        ocr_confidence: 图片 OCR 置信度。

    Returns:
        可供生产 Chunker 消费的不可变元素。

    """
    return Element(
        element_id=element_id,
        kind=kind,
        text=text,
        locator=Locator(
            file_path="private.docx",
            heading_path=heading_path,
            heading_index=heading_index,
            paragraph_index=paragraph_index,
            table_index=table_index,
            image_index=image_index,
            fragment=text[:240] or "image.png",
        ),
        content_sha256="a" * 64,
        list_level=list_level,
        media_type="image/png" if kind == ElementKind.IMAGE else None,
        media_name="image.png" if kind == ElementKind.IMAGE else None,
        ocr_state=ocr_state,
        ocr_confidence=ocr_confidence,
    )


def _chunker(
    *,
    target: int = 64,
    hard_max: int = 96,
    overlap: int = 12,
) -> Chunker:
    """构造使用确定性 UTF-8 计数的 Chunker。

    Args:
        target: 目标 token 数。
        hard_max: 硬 token 上限。
        overlap: 长原子内部 overlap 上限。

    Returns:
        测试用 production Chunker。

    """
    return Chunker(
        ChunkerConfig(
            target_tokens=target,
            hard_max_tokens=hard_max,
            overlap_tokens=overlap,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE,
    )


def _chunk(
    elements: list[Element],
    chunker: Chunker | None = None,
) -> list[Chunk]:
    return (chunker or _chunker()).chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        elements,
        metadata=_METADATA,
    )


def test_headings_open_sections_but_never_become_chunks() -> None:
    """标题仅开启 section，重复标题文字由 heading_index 区分。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    chunks = _chunk(
        [
            _element(
                "heading-1",
                ElementKind.HEADING,
                "same",
                heading_path=("same",),
                heading_index=1,
            ),
            _element(
                "paragraph-1",
                ElementKind.PARAGRAPH,
                "first body",
                heading_path=("same",),
                heading_index=1,
                paragraph_index=1,
            ),
            _element(
                "heading-2",
                ElementKind.HEADING,
                "same",
                heading_path=("same",),
                heading_index=2,
            ),
            _element(
                "paragraph-2",
                ElementKind.PARAGRAPH,
                "second body",
                heading_path=("same",),
                heading_index=2,
                paragraph_index=2,
            ),
        ]
    )

    assert [chunk.text for chunk in chunks] == [
        "first body",
        "second body",
    ]
    assert all(chunk.chunk_role == ChunkRole.TEXT for chunk in chunks)
    assert chunks[0].section_id != chunks[1].section_id
    assert chunks[0].next_chunk_id is None
    assert chunks[1].previous_chunk_id is None


def test_root_section_and_empty_parent_heading_are_stable() -> None:
    """首标题前正文进入 root，空父 section 不产生空 chunk。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    chunks = _chunk(
        [
            _element(
                "root-paragraph",
                ElementKind.PARAGRAPH,
                "root body",
                paragraph_index=1,
            ),
            _element(
                "parent-heading",
                ElementKind.HEADING,
                "parent",
                heading_path=("parent",),
                heading_index=1,
            ),
            _element(
                "child-heading",
                ElementKind.HEADING,
                "child",
                heading_path=("parent", "child"),
                heading_index=2,
            ),
            _element(
                "child-paragraph",
                ElementKind.PARAGRAPH,
                "child body",
                heading_path=("parent", "child"),
                heading_index=2,
                paragraph_index=2,
            ),
        ]
    )

    assert [chunk.text for chunk in chunks] == [
        "root body",
        "child body",
    ]
    assert chunks[0].section_id != chunks[1].section_id
    assert chunks[1].embedding_text.startswith("parent > child\n")


def test_short_paragraphs_and_list_items_pack_with_exact_separators() -> None:
    """同 section 连续正文按段落和列表规则合并。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    chunks = _chunk(
        [
            _element(
                "paragraph-1",
                ElementKind.PARAGRAPH,
                "alpha",
                paragraph_index=1,
            ),
            _element(
                "list-1",
                ElementKind.PARAGRAPH,
                "item one",
                paragraph_index=2,
                list_level=0,
            ),
            _element(
                "list-2",
                ElementKind.PARAGRAPH,
                "item two",
                paragraph_index=3,
                list_level=0,
            ),
        ],
        _chunker(target=128, hard_max=160),
    )

    assert len(chunks) == 1
    assert chunks[0].text == "alpha\n\nitem one\nitem two"
    assert [chunks[0].text[span.start_char : span.end_char] for span in (
        chunks[0].source_spans
    )] == ["alpha", "item one", "item two"]


def test_table_and_ocr_each_form_isolated_neighbor_group() -> None:
    """表格和 OCR 图片不得与相邻正文或彼此合并。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    chunks = _chunk(
        [
            _element(
                "paragraph-1",
                ElementKind.PARAGRAPH,
                "before",
                paragraph_index=1,
            ),
            _element(
                "table-1",
                ElementKind.TABLE,
                "name | value\nalpha | 1\nbeta | 2",
                table_index=1,
            ),
            _element(
                "image-1",
                ElementKind.IMAGE,
                "line one\nline two",
                image_index=1,
                ocr_state=OcrState.SUCCEEDED,
                ocr_confidence=0.96,
            ),
            _element(
                "paragraph-2",
                ElementKind.PARAGRAPH,
                "after",
                paragraph_index=2,
            ),
        ],
        _chunker(target=256, hard_max=320),
    )

    assert [chunk.chunk_role for chunk in chunks] == [
        ChunkRole.TEXT,
        ChunkRole.TABLE,
        ChunkRole.OCR,
        ChunkRole.TEXT,
    ]
    assert len({chunk.neighbor_group_id for chunk in chunks}) == 4
    assert all(
        chunk.previous_chunk_id is None and chunk.next_chunk_id is None
        for chunk in chunks
    )


def test_pending_and_failed_ocr_never_become_evidence() -> None:
    """pending/failed OCR 图片继续不形成证据。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    chunks = _chunk(
        [
            _element(
                "pending",
                ElementKind.IMAGE,
                "untrusted",
                image_index=1,
                ocr_state=OcrState.PENDING,
            ),
            _element(
                "failed",
                ElementKind.IMAGE,
                "untrusted",
                image_index=2,
                ocr_state=OcrState.FAILED,
            ),
        ]
    )

    assert chunks == []


def test_long_atom_prefers_semantic_boundary_and_never_forces_overlap() -> None:
    """长原子优先语义边界，无完整语义后缀时允许零 overlap。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    text = "first sentence. second sentence. " + ("x" * 30)
    chunks = _chunk(
        [
            _element(
                "paragraph-1",
                ElementKind.PARAGRAPH,
                text,
                paragraph_index=1,
            )
        ],
        _chunker(target=24, hard_max=32, overlap=8),
    )

    assert len(chunks) >= 3
    assert chunks[0].text.endswith(".")
    assert all(
        Utf8TokenCounter().count(chunk.text) <= 32 for chunk in chunks
    )
    assert all(
        chunk.neighbor_group_id == chunks[0].neighbor_group_id
        for chunk in chunks
    )
    assert all(
        left.next_chunk_id == right.chunk_id
        and right.previous_chunk_id == left.chunk_id
        for left, right in pairwise(chunks)
    )


def test_table_repeats_header_without_splitting_normal_rows() -> None:
    """表格 segment 重复表头且普通数据行保持完整。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    chunks = _chunk(
        [
            _element(
                "table-1",
                ElementKind.TABLE,
                "h1 | h2\nrow-one | 1\nrow-two | 2\nrow-three | 3",
                table_index=1,
            )
        ],
        _chunker(target=28, hard_max=32, overlap=4),
    )

    assert len(chunks) >= 2
    assert all(chunk.text.startswith("h1 | h2\n") for chunk in chunks)
    assert all(
        any(row in chunk.text for chunk in chunks)
        for row in ("row-one | 1", "row-two | 2", "row-three | 3")
    )
    assert all(
        sum(row in chunk.text for chunk in chunks) == 1
        for row in ("row-one | 1", "row-two | 2", "row-three | 3")
    )
    assert chunks[0].source_spans[0].is_repeated is False
    assert all(
        chunk.source_spans[0].is_repeated is True
        for chunk in chunks[1:]
    )
