from rag_app.contracts import (
    Chunk,
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
        paragraph_index=7,
        fragment="所有交付物必须复核",
    )

    assert locator.display() == (
        "资料/规范.docx > 第一章 > 范围 > 段落7 > 所有交付物必须复核"
    )
    assert not hasattr(locator, "page")


def test_stable_ids_survive_file_rename() -> None:
    content_sha256 = "a" * 64
    old_doc_id = stable_doc_id(content_sha256)
    new_doc_id = stable_doc_id(content_sha256)
    old_locator = Locator(
        file_path="旧名称.docx",
        heading_path=("标题",),
        paragraph_index=1,
        fragment="稳定内容",
    )
    new_locator = old_locator.model_copy(update={"file_path": "新名称.docx"})

    assert old_doc_id == new_doc_id
    assert stable_chunk_id(old_doc_id, old_locator, "稳定内容") == (
        stable_chunk_id(new_doc_id, new_locator, "稳定内容")
    )


def test_contracts_round_trip() -> None:
    locator = Locator(
        file_path="规范.docx",
        heading_path=("标题",),
        table_index=2,
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
    )

    assert Element.model_validate_json(element.model_dump_json()) == element
    assert Chunk.model_validate_json(chunk.model_dump_json()) == chunk
