import pytest
from pydantic import ValidationError

from rag_app.contracts import (
    Chunk,
    DocumentMetadata,
    Element,
    ElementKind,
    Locator,
    OcrState,
    stable_chunk_id,
    stable_doc_id,
)


def test_locator_is_traceable_without_page_number() -> None:
    locator = Locator(
        file_path="资料/规范.docx",
        heading_path=("第一章", "范围"),
        heading_index=2,
        paragraph_index=7,
        segment_index=1,
        fragment="所有交付物必须复核",
    )

    assert locator.display() == (
        "资料/规范.docx > 第一章 > 范围 > 标题2 > 段落7 > 片段1"
        " > 所有交付物必须复核"
    )
    assert not hasattr(locator, "page")


def test_stable_ids_survive_file_rename() -> None:
    content_sha256 = "a" * 64
    old_doc_id = stable_doc_id(content_sha256)
    new_doc_id = stable_doc_id(content_sha256)
    old_locator = Locator(
        file_path="旧名称.docx",
        heading_path=("标题",),
        heading_index=1,
        paragraph_index=1,
        segment_index=1,
        fragment="稳定内容",
    )
    new_locator = old_locator.model_copy(update={"file_path": "新名称.docx"})

    assert old_doc_id == new_doc_id
    assert stable_chunk_id(old_doc_id, old_locator, "稳定内容") == (
        stable_chunk_id(new_doc_id, new_locator, "稳定内容")
    )


def test_locator_logical_key_distinguishes_heading_and_segment() -> None:
    locator = Locator(
        file_path="规范.docx",
        heading_path=("重复标题",),
        heading_index=1,
        paragraph_index=1,
        segment_index=1,
        fragment="相同内容",
    )

    assert locator.logical_key() != locator.model_copy(
        update={"heading_index": 2}
    ).logical_key()
    assert locator.logical_key() != locator.model_copy(
        update={"segment_index": 2}
    ).logical_key()


def test_contracts_round_trip() -> None:
    locator = Locator(
        file_path="规范.docx",
        heading_path=("标题",),
        heading_index=1,
        table_index=2,
        segment_index=1,
        fragment="字段: 值",
    )
    element = Element(
        element_id="element-1",
        kind=ElementKind.TABLE,
        text="字段: 值",
        locator=locator,
        content_sha256="b" * 64,
        ocr_state=OcrState.NOT_APPLICABLE,
    )
    chunk = Chunk(
        chunk_id="chunk-1",
        source_id="src_" + "1" * 32,
        doc_version="sha256:" + "b" * 64,
        pipeline_fingerprint="sha256:" + "f" * 64,
        text=element.text,
        embedding_text="标题\n" + element.text,
        element_kind=ElementKind.TABLE,
        locators=(locator,),
        content_sha256=element.content_sha256,
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )

    assert Element.model_validate_json(element.model_dump_json()) == element
    assert Chunk.model_validate_json(chunk.model_dump_json()) == chunk


@pytest.mark.parametrize("field", ("text", "embedding_text"))
def test_chunk_rejects_whitespace_only_content(field: str) -> None:
    locator = Locator(
        file_path="blank.docx",
        paragraph_index=1,
        segment_index=1,
        fragment="空白控制符",
    )
    payload = {
        "chunk_id": "chunk-blank",
        "source_id": "src_" + "1" * 32,
        "doc_version": "sha256:" + "b" * 64,
        "pipeline_fingerprint": "sha256:" + "f" * 64,
        "text": "有效文本",
        "embedding_text": "有效文本",
        "element_kind": ElementKind.PARAGRAPH,
        "locators": (locator,),
        "content_sha256": "b" * 64,
        "document_status": "active",
        "authority_level": "official",
        "effective_from": None,
        "effective_to": None,
    }
    payload[field] = "\t\n"

    with pytest.raises(ValidationError):
        Chunk.model_validate(payload)


@pytest.mark.parametrize(
    "metadata",
    (
        {
            "document_status": "published",
            "authority_level": "official",
            "effective_from": None,
            "effective_to": None,
        },
        {
            "document_status": "active",
            "authority_level": "trusted",
            "effective_from": None,
            "effective_to": None,
        },
        {
            "document_status": "active",
            "authority_level": "official",
            "effective_from": 1_700_000_000,
            "effective_to": None,
        },
        {
            "document_status": "active",
            "authority_level": "official",
            "effective_from": "2026-01-01 00:00:00Z",
            "effective_to": None,
        },
    ),
)
def test_document_metadata_rejects_illegal_vocab_and_dates(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DocumentMetadata.model_validate(metadata)


def test_document_metadata_and_chunk_require_explicit_metadata() -> None:
    with pytest.raises(ValidationError):
        DocumentMetadata.model_validate({})

    locator = Locator(
        file_path="missing.docx",
        paragraph_index=1,
        segment_index=1,
        fragment="显式元数据",
    )
    chunk_payload: dict[str, object] = {
        "chunk_id": "chunk-missing-metadata",
        "source_id": "src_" + "1" * 32,
        "doc_version": "sha256:" + "b" * 64,
        "pipeline_fingerprint": "sha256:" + "f" * 64,
        "text": "显式元数据",
        "embedding_text": "显式元数据",
        "element_kind": ElementKind.PARAGRAPH,
        "locators": (locator,),
        "content_sha256": "b" * 64,
    }
    with pytest.raises(ValidationError):
        Chunk.model_validate(chunk_payload)
