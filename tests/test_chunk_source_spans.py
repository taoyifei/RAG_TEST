import pytest
from pydantic import ValidationError

from rag_app.contracts import (
    Chunk,
    ChunkIdentity,
    ChunkRole,
    ChunkSourceSpan,
    ElementKind,
    Locator,
    stable_chunk_id,
)

_SOURCE_ID = "src_" + "1" * 32


def _locator(
    paragraph_index: int,
    *,
    file_path: str = "old.docx",
) -> Locator:
    """构造稳定逻辑位置。

    Args:
        paragraph_index: 段落序号。
        file_path: 仅用于展示的文件路径。

    Returns:
        带 segment 序号的 locator。

    """
    return Locator(
        file_path=file_path,
        heading_path=("section",),
        heading_index=1,
        paragraph_index=paragraph_index,
        segment_index=1,
        fragment=f"paragraph-{paragraph_index}",
    )


def _span(
    element_id: str,
    locator: Locator,
    start: int,
    end: int,
) -> ChunkSourceSpan:
    """构造来源 span。

    Args:
        element_id: 原始元素 ID。
        locator: 原始元素逻辑位置。
        start: chunk 文本起始字符。
        end: chunk 文本结束字符。

    Returns:
        同时记录 chunk 与 element 字符范围的 span。

    """
    return ChunkSourceSpan(
        element_id=element_id,
        locator=locator,
        start_char=start,
        end_char=end,
        source_start_char=0,
        source_end_char=end - start,
    )


def _chunk_payload() -> dict[str, object]:
    text = "alpha\n\nbeta"
    first = _span("element-1", _locator(1), 0, 5)
    second = _span("element-2", _locator(2), 7, 11)
    spans = (first, second)
    section_id = "section_" + "a" * 32
    group_id = "group_" + "b" * 32
    return {
        "chunk_id": stable_chunk_id(
            _SOURCE_ID,
            ChunkIdentity(
                section_id=section_id,
                neighbor_group_id=group_id,
                chunk_role=ChunkRole.TEXT,
                source_spans=spans,
            ),
            text,
        ),
        "source_id": _SOURCE_ID,
        "doc_version": "sha256:" + "c" * 64,
        "pipeline_fingerprint": "sha256:" + "f" * 64,
        "section_id": section_id,
        "neighbor_group_id": group_id,
        "chunk_role": ChunkRole.TEXT,
        "source_spans": spans,
        "text": text,
        "embedding_text": f"section\n{text}",
        "element_kind": ElementKind.PARAGRAPH,
        "locators": (first.locator, second.locator),
        "content_sha256": "0" * 64,
        "document_status": "active",
        "authority_level": "official",
        "effective_from": None,
        "effective_to": None,
    }


def test_chunk_spans_cover_sources_but_not_inserted_separator() -> None:
    """Source spans 精确覆盖原文并跳过人工段落分隔符。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    payload = _chunk_payload()
    text = str(payload["text"])
    payload["content_sha256"] = __import__("hashlib").sha256(
        text.encode("utf-8")
    ).hexdigest()

    chunk = Chunk.model_validate(payload)

    assert [
        chunk.text[span.start_char : span.end_char]
        for span in chunk.source_spans
    ] == ["alpha", "beta"]
    assert all(
        not (span.start_char <= 5 < span.end_char)
        for span in chunk.source_spans
    )


@pytest.mark.parametrize(
    "mutation",
    (
        {"start_char": 5, "end_char": 5},
        {"start_char": 0, "end_char": 99},
    ),
)
def test_chunk_rejects_empty_or_out_of_range_span(
    mutation: dict[str, int],
) -> None:
    """空 span 或越界 span 必须拒绝。

    Args:
        mutation: 替换首 span 的非法字符范围。

    Returns:
        无返回值。

    """
    payload = _chunk_payload()
    spans = list(payload["source_spans"])
    spans[0] = spans[0].model_copy(update=mutation)
    payload["source_spans"] = spans

    with pytest.raises(ValidationError):
        Chunk.model_validate(payload)


def test_chunk_locators_must_equal_ordered_unique_span_locators() -> None:
    """Chunk.locators 不得脱离 source spans 独立声明。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    payload = _chunk_payload()
    payload["locators"] = (payload["locators"][0],)

    with pytest.raises(ValidationError):
        Chunk.model_validate(payload)


def test_stable_id_uses_all_spans_and_ignores_file_rename() -> None:
    """文件纯重命名不改 ID，第二个 locator 篡改必须改 ID。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    payload = _chunk_payload()
    spans = tuple(payload["source_spans"])
    original = str(payload["chunk_id"])
    renamed = tuple(
        span.model_copy(
            update={
                "locator": span.locator.model_copy(
                    update={"file_path": "renamed.docx"}
                )
            }
        )
        for span in spans
    )
    tampered = (
        spans[0],
        spans[1].model_copy(
            update={
                "locator": spans[1].locator.model_copy(
                    update={"paragraph_index": 3}
                )
            }
        ),
    )

    assert stable_chunk_id(
        _SOURCE_ID,
        ChunkIdentity(
            section_id=str(payload["section_id"]),
            neighbor_group_id=str(payload["neighbor_group_id"]),
            chunk_role=ChunkRole.TEXT,
            source_spans=renamed,
        ),
        str(payload["text"]),
    ) == original
    assert stable_chunk_id(
        _SOURCE_ID,
        ChunkIdentity(
            section_id=str(payload["section_id"]),
            neighbor_group_id=str(payload["neighbor_group_id"]),
            chunk_role=ChunkRole.TEXT,
            source_spans=tampered,
        ),
        str(payload["text"]),
    ) != original
