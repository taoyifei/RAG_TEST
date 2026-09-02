import hashlib

from rag_app.adapters.legacy.contracts import (
    core_chunk_to_legacy,
    legacy_chunk_to_core,
)
from rag_app.adapters.legacy.query import legacy_evidence_to_core
from rag_app.adapters.legacy.stores import InMemoryVectorStore
from rag_app.contracts import (
    Chunk,
    ChunkRole,
    ChunkSourceSpan,
    ElementKind,
    Locator,
)
from rag_app.core.models import (
    IndexRevisionRef,
    IndexRevisionState,
    VectorSearchRequest,
    VectorWriteRequest,
)
from rag_app.generation.evidence import EvidenceItem as LegacyEvidenceItem


def _legacy_chunk() -> Chunk:
    locator = Locator(
        file_path="renamed.docx",
        paragraph_index=1,
        fragment="abc",
    )
    span = ChunkSourceSpan(
        element_id="paragraph-1",
        locator=locator,
        start_char=0,
        end_char=3,
        source_start_char=0,
        source_end_char=3,
    )
    return Chunk(
        chunk_id=f"chunk_{'1' * 32}",
        source_id=f"src_{'2' * 32}",
        doc_version=f"sha256:{'3' * 64}",
        pipeline_fingerprint=f"sha256:{'4' * 64}",
        section_id=f"section_{'5' * 32}",
        neighbor_group_id=f"group_{'6' * 32}",
        chunk_role=ChunkRole.TEXT,
        source_spans=(span,),
        text="abc",
        embedding_text="abc",
        element_kind=ElementKind.PARAGRAPH,
        locators=(locator,),
        content_sha256=hashlib.sha256(b"abc").hexdigest(),
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )


def _revision() -> IndexRevisionRef:
    return IndexRevisionRef(
        project_id=f"prj_{'a' * 32}",
        knowledge_base_id=f"kb_{'b' * 32}",
        index_revision_id=f"irev_{'c' * 32}",
        index_fingerprint=f"sha256:{'d' * 64}",
        state=IndexRevisionState.ACTIVE,
    )


def test_legacy_chunk_mapping_reports_omitted_file_path() -> None:
    legacy = _legacy_chunk()
    mapped, warnings = legacy_chunk_to_core(legacy)
    assert mapped.citation_text == "abc"
    assert mapped.embedding_text == "abc"
    assert mapped.source_spans[0].structural_path
    assert "LEGACY_FILE_PATH_OMITTED" in warnings
    assert "renamed.docx" not in repr(mapped)
    assert mapped.version.document_version_id != legacy.doc_version


def test_chunk_v3_can_be_explicitly_mapped_back_to_legacy() -> None:
    mapped, _ = legacy_chunk_to_core(_legacy_chunk())
    restored, warnings = core_chunk_to_legacy(
        mapped,
        display_name="restored.docx",
    )
    assert restored.text == "abc"
    assert restored.embedding_text == "abc"
    assert restored.locators[0].file_path == "restored.docx"
    assert warnings == ()


def test_equal_dimensions_do_not_allow_cross_slot_store_search() -> None:
    mapped, _ = legacy_chunk_to_core(_legacy_chunk())
    store = InMemoryVectorStore()
    revision = _revision()
    store.write(
        VectorWriteRequest(
            revision=revision,
            slot_id="primary",
            vector_name="dense_primary",
            chunks=(mapped,),
            vectors=((1.0, 0.0),),
        )
    )
    store.write(
        VectorWriteRequest(
            revision=revision,
            slot_id="standby",
            vector_name="dense_standby",
            chunks=(mapped,),
            vectors=((0.0, 1.0),),
        )
    )
    primary = store.search(
        VectorSearchRequest(
            revision=revision,
            slot_id="primary",
            vector_name="dense_primary",
            query_vector=(1.0, 0.0),
            limit=1,
        )
    )
    standby = store.search(
        VectorSearchRequest(
            revision=revision,
            slot_id="standby",
            vector_name="dense_standby",
            query_vector=(1.0, 0.0),
            limit=1,
        )
    )
    assert primary[0].score == 1.0
    assert standby[0].score == 0.0
    assert primary[0].channels == ("dense:primary",)
    assert standby[0].channels == ("dense:standby",)


def test_legacy_evidence_conversion_is_explicit_and_safe() -> None:
    chunk = _legacy_chunk()
    legacy = LegacyEvidenceItem(
        evidence_id="E1",
        chunk_id=chunk.chunk_id,
        text=chunk.text,
        locators=chunk.locators,
        source_spans=chunk.source_spans,
        low_confidence_ocr=False,
        source_id=chunk.source_id,
        neighbor_group_id=chunk.neighbor_group_id,
        rerank_rank=1,
        rerank_score=0.9,
    )
    mapped = legacy_evidence_to_core(legacy)
    assert mapped.evidence_id == "E1"
    assert mapped.citation_text == "abc"
    assert "citation_text=" not in repr(mapped)
