from rag_app.chunking import Chunker, ChunkerConfig, Utf8TokenCounter
from rag_app.contracts import Element, ElementKind, Locator, OcrState

_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64
_SOURCE_ID = "src_" + "1" * 32
_DOC_VERSION = "sha256:" + "a" * 64


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
    )

    assert len(chunks) == 2
    assert all(Utf8TokenCounter().count(chunk.text) <= 12 for chunk in chunks)
    assert chunks[0].text[-3:] == chunks[1].text[:3]


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

    old_chunk = chunker.chunk(_SOURCE_ID, _DOC_VERSION, [old_element])[0]
    new_chunk = chunker.chunk(_SOURCE_ID, _DOC_VERSION, [new_element])[0]

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
    )

    assert chunks == []


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
    )

    assert len(chunks) == 1
    assert chunks[0].contains_ocr is True
    assert chunks[0].minimum_ocr_confidence == 0.61
