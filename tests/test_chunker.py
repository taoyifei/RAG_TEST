import pytest

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
_DOC_VERSION = "sha256:" + "a" * 64
_METADATA = DocumentMetadata(
    document_status="active",
    authority_level="official",
    effective_from=None,
    effective_to=None,
)


def _element(
    text: str,
    *,
    kind: ElementKind = ElementKind.PARAGRAPH,
    ocr_state: OcrState = OcrState.NOT_APPLICABLE,
    ocr_confidence: float | None = None,
) -> Element:
    return Element(
        element_id="element-1",
        kind=kind,
        text=text,
        locator=Locator(
            file_path="资料.docx",
            heading_path=("章节",),
            heading_index=1,
            paragraph_index=1 if kind == ElementKind.PARAGRAPH else None,
            image_index=1 if kind == ElementKind.IMAGE else None,
            fragment=text[:240] or "image.png",
        ),
        content_sha256="a" * 64,
        media_type="image/png" if kind == ElementKind.IMAGE else None,
        media_name="image.png" if kind == ElementKind.IMAGE else None,
        ocr_state=ocr_state,
        ocr_confidence=ocr_confidence,
    )


def test_chunker_respects_hard_token_limit() -> None:
    chunker = Chunker(
        ChunkerConfig(
            target_tokens=12,
            hard_max_tokens=12,
            overlap_tokens=3,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )

    chunks = chunker.chunk(
        source_id=_SOURCE_ID,
        doc_version=_DOC_VERSION,
        elements=[_element("abcdefghijklmnopqrst")],
        metadata=_METADATA,
    )

    assert len(chunks) == 2
    assert all(Utf8TokenCounter().count(chunk.text) <= 12 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == "abcdefghijklmnopqrst"
    assert chunks[0].text[-3:] != chunks[1].text[:3]
    assert [chunk.locators[0].segment_index for chunk in chunks] == [1, 2]


def test_chunk_ids_ignore_file_rename() -> None:
    chunker = Chunker(
        ChunkerConfig(
            target_tokens=100,
            hard_max_tokens=100,
            overlap_tokens=10,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    old_element = _element("稳定证据")
    new_element = old_element.model_copy(
        update={
            "locator": old_element.locator.model_copy(
                update={"file_path": "重命名.docx"}
            )
        }
    )

    old_chunk = chunker.chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        [old_element],
        metadata=_METADATA,
    )[0]
    new_chunk = chunker.chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        [new_element],
        metadata=_METADATA,
    )[0]

    assert old_chunk.chunk_id == new_chunk.chunk_id


def test_pending_ocr_image_does_not_become_evidence() -> None:
    chunker = Chunker(
        ChunkerConfig(
            target_tokens=100,
            hard_max_tokens=100,
            overlap_tokens=10,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )

    chunks = chunker.chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        [_element("", kind=ElementKind.IMAGE, ocr_state=OcrState.PENDING)],
        metadata=_METADATA,
    )

    assert chunks == []


def test_chunker_rejects_whitespace_only_element() -> None:
    chunker = Chunker(
        ChunkerConfig(
            target_tokens=100,
            hard_max_tokens=100,
            overlap_tokens=10,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    element = _element("\t\n").model_copy(
        update={
            "locator": Locator(
                file_path="blank.docx",
                paragraph_index=1,
                fragment="空白控制符",
            )
        }
    )

    chunks = chunker.chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        [element],
        metadata=_METADATA,
    )

    assert chunks == []


def test_chunker_requires_explicit_document_metadata() -> None:
    chunker = Chunker(
        ChunkerConfig(
            target_tokens=100,
            hard_max_tokens=100,
            overlap_tokens=10,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )

    with pytest.raises(TypeError):
        chunker.chunk(
            _SOURCE_ID,
            _DOC_VERSION,
            [_element("显式元数据")],
        )


def test_low_confidence_ocr_is_explicitly_tagged() -> None:
    chunker = Chunker(
        ChunkerConfig(
            target_tokens=100,
            hard_max_tokens=100,
            overlap_tokens=10,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )

    chunks = chunker.chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        [
            _element(
                "图片中的流程说明",
                kind=ElementKind.IMAGE,
                ocr_state=OcrState.LOW_CONFIDENCE,
                ocr_confidence=0.61,
            )
        ],
        metadata=_METADATA,
    )

    assert len(chunks) == 1
    assert chunks[0].contains_ocr is True
    assert chunks[0].minimum_ocr_confidence == 0.61
    assert chunks[0].locators[0].segment_index == 1


def test_repeated_identical_segments_have_unique_stable_ids() -> None:
    chunker = Chunker(
        ChunkerConfig(
            target_tokens=4,
            hard_max_tokens=4,
            overlap_tokens=2,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    element = _element("aaaaaaaaaaaa")

    first = chunker.chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        [element],
        metadata=_METADATA,
    )
    second = chunker.chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        [element],
        metadata=_METADATA,
    )

    assert first == second
    assert len(first) > 2
    assert len({chunk.chunk_id for chunk in first}) == len(first)
    assert [chunk.locators[0].segment_index for chunk in first] == list(
        range(1, len(first) + 1)
    )


def test_duplicate_media_references_with_same_ocr_text_are_unique() -> None:
    chunker = Chunker(
        ChunkerConfig(
            target_tokens=100,
            hard_max_tokens=100,
            overlap_tokens=10,
        ),
        Utf8TokenCounter(),
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    first_image = _element(
        "相同 OCR 文本",
        kind=ElementKind.IMAGE,
        ocr_state=OcrState.SUCCEEDED,
        ocr_confidence=0.95,
    )
    second_image = first_image.model_copy(
        update={
            "element_id": "element-2",
            "locator": first_image.locator.model_copy(
                update={"image_index": 2}
            ),
        }
    )

    chunks = chunker.chunk(
        _SOURCE_ID,
        _DOC_VERSION,
        [first_image, second_image],
        metadata=_METADATA,
    )

    assert [chunk.text for chunk in chunks] == [
        "相同 OCR 文本",
        "相同 OCR 文本",
    ]
    assert len({chunk.chunk_id for chunk in chunks}) == 2
    assert [chunk.locators[0].image_index for chunk in chunks] == [1, 2]
