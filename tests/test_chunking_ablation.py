from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import evaluation.chunking_ablation as ablation
from evaluation.chunking_ablation import (
    DEFAULT_SECTION_CANDIDATES,
    RetrievalEnvironment,
    load_tuning_cases_only,
    parse_candidate,
    summarize_section_candidate,
)
from evaluation.dataset import EvaluationCase
from rag_app.chunking import Chunker, ChunkerConfig, Utf8TokenCounter
from rag_app.contracts import (
    Chunk,
    DocumentMetadata,
    Element,
    ElementKind,
    Locator,
    OcrState,
)
from rag_app.freeze_evidence import canonical_tuning_digest
from rag_app.index.build import OcrElementProcessor
from rag_app.ocr.models import DEFAULT_OCR_REVISION, OcrLine, OcrResponse
from rag_app.runtime import load_pipeline
from rag_app.state import StateStore


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


def test_retrieval_environment_requires_real_ocr_endpoint() -> None:
    with pytest.raises(ValueError, match="OCR"):
        RetrievalEnvironment(
            qdrant_url="http://qdrant.test",
            qdrant_api_key="test-key",
            embedding_endpoints=("http://embedding.test",),
            reranker_endpoints=("http://reranker.test",),
            ocr_endpoints=(),
            embedding_api_token=None,
            reranker_api_token=None,
            ocr_api_token=None,
            document_paths={"synthetic": "synthetic.docx"},
        )


class _CalibrationOcrClient:
    def recognize(
        self,
        media_bytes: bytes,
        *,
        media_type: str,
        media_sha256: str,
    ) -> OcrResponse:
        assert media_bytes == b"synthetic-image"
        assert media_type == "image/png"
        return OcrResponse(
            media_sha256=media_sha256,
            ocr_revision=DEFAULT_OCR_REVISION,
            text="校准 OCR 证据",
            confidence=0.97,
            lines=(
                OcrLine(
                    text="校准 OCR 证据",
                    confidence=0.97,
                    bbox=(0, 0, 16, 16),
                ),
            ),
            width=16,
            height=16,
            elapsed_ms=1,
        )


def test_calibration_uses_shared_ocr_processor_before_chunking(
    tmp_path: Path,
) -> None:
    pipeline = load_pipeline(
        Path(__file__).parents[1] / "deployment/config/pipeline.json"
    )
    locator = Locator(
        file_path="synthetic.docx",
        heading_path=("公开标题",),
        heading_index=1,
        image_index=1,
        fragment="image.png",
    )
    document = ablation._ParsedDocument(
        source_id="src_" + ("a" * 32),
        source_path="synthetic.docx",
        doc_version="sha256:" + ("b" * 64),
        elements=(
            Element(
                element_id="image-1",
                kind=ElementKind.IMAGE,
                text="",
                locator=locator,
                content_sha256="c" * 64,
                media_type="image/png",
                media_name="image.png",
                binary_data=b"synthetic-image",
                ocr_state=OcrState.PENDING,
            ),
        ),
        metadata=DocumentMetadata(
            document_status="active",
            authority_level="official",
            effective_from=None,
            effective_to=None,
        ),
    )
    case = EvaluationCase.model_validate(
        {
            "id": "Q001",
            "split": "tuning",
            "categories": ["ocr"],
            "question": "图片写了什么？",
            "validation_state": "blocked_gpu_ocr",
            "expected": {
                "answerable": None,
                "required_facts": [],
                "evidence": [
                    {
                        "document": "synthetic",
                        "locator_contains": "图片1",
                        "quote": None,
                    }
                ],
            },
        }
    )
    state = StateStore(tmp_path / "calibration.sqlite3")
    state.initialize()
    processor = OcrElementProcessor(
        state=state,
        ocr_client=_CalibrationOcrClient(),
        ocr_revision=DEFAULT_OCR_REVISION,
        minimum_confidence=0.8,
    )

    documents, ocr_states = ablation._enrich_documents_with_ocr(
        (document,),
        (case,),
        {"synthetic": "synthetic.docx"},
        processor=processor,
        pipeline_fingerprint=pipeline.fingerprint(),
    )
    chunks = ablation._retrieval_chunks(
        DEFAULT_SECTION_CANDIDATES[0],
        pipeline=pipeline,
        counter=Utf8TokenCounter(),
        documents=documents,
    )

    assert ocr_states == {
        "succeeded": 1,
        "low_confidence": 0,
        "failed": 0,
        "pending": 0,
    }
    assert any(chunk.text == "校准 OCR 证据" for chunk in chunks)


class _ForcedOcrProcessor:
    def __init__(self, state: OcrState) -> None:
        self._state = state

    def process(
        self,
        elements: list[Element],
        version: object,
    ) -> list[Element]:
        del version
        return [
            element.model_copy(
                update={
                    "ocr_state": self._state,
                    "ocr_error": f"forced-{self._state.value}",
                }
            )
            for element in elements
        ]


@pytest.mark.parametrize(
    ("state", "message"),
    (
        (OcrState.PENDING, "pending"),
        (OcrState.FAILED, "识别失败"),
    ),
)
def test_calibration_rejects_pending_or_required_failed_ocr(
    state: OcrState,
    message: str,
) -> None:
    document = ablation._ParsedDocument(
        source_id="src_" + ("d" * 32),
        source_path="synthetic.docx",
        doc_version="sha256:" + ("e" * 64),
        elements=(
            Element(
                element_id="image-required",
                kind=ElementKind.IMAGE,
                text="",
                locator=Locator(
                    file_path="synthetic.docx",
                    heading_path=("公开标题",),
                    heading_index=1,
                    image_index=1,
                    fragment="required.png",
                ),
                content_sha256="f" * 64,
                media_type="image/png",
                media_name="required.png",
                binary_data=b"required-image",
                ocr_state=OcrState.PENDING,
            ),
        ),
        metadata=DocumentMetadata(
            document_status="active",
            authority_level="official",
            effective_from=None,
            effective_to=None,
        ),
    )
    case = EvaluationCase.model_validate(
        {
            "id": "Q002",
            "split": "tuning",
            "categories": ["ocr"],
            "question": "校准图片问题",
            "validation_state": "blocked_gpu_ocr",
            "expected": {
                "answerable": None,
                "required_facts": [],
                "evidence": [
                    {
                        "document": "synthetic",
                        "locator_contains": "图片1",
                        "quote": None,
                    }
                ],
            },
        }
    )

    with pytest.raises(ValueError, match=message):
        ablation._enrich_documents_with_ocr(
            (document,),
            (case,),
            {"synthetic": "synthetic.docx"},
            processor=cast(
                OcrElementProcessor,
                _ForcedOcrProcessor(state),
            ),
            pipeline_fingerprint="sha256:" + ("1" * 64),
        )


def test_tuning_ocr_document_match_is_not_raw_suffix() -> None:
    image = Element(
        element_id="image-suffix",
        kind=ElementKind.IMAGE,
        text="OCR 文本",
        locator=Locator(
            file_path="barfoo.docx",
            heading_path=("公开标题",),
            heading_index=1,
            image_index=1,
            fragment="required.png",
        ),
        content_sha256="a" * 64,
        media_type="image/png",
        media_name="required.png",
        binary_data=b"required-image",
        ocr_state=OcrState.SUCCEEDED,
        ocr_confidence=0.99,
    )
    document = ablation._ParsedDocument(
        source_id="src_" + "b" * 32,
        source_path="barfoo.docx",
        doc_version="sha256:" + "c" * 64,
        elements=(image,),
        metadata=DocumentMetadata(
            document_status="active",
            authority_level="official",
            effective_from=None,
            effective_to=None,
        ),
    )
    case = EvaluationCase.model_validate(
        {
            "id": "Q003",
            "split": "tuning",
            "categories": ["ocr"],
            "question": "校准图片问题",
            "validation_state": "blocked_gpu_ocr",
            "expected": {
                "answerable": None,
                "required_facts": [],
                "evidence": [
                    {
                        "document": "synthetic",
                        "locator_contains": "图片1",
                        "quote": None,
                    }
                ],
            },
        }
    )

    with pytest.raises(ValueError, match="locator"):
        ablation._require_tuning_ocr_evidence(
            (document,),
            (case,),
            {"synthetic": "foo.docx"},
        )


class _CalibrationQdrant:
    def __init__(self) -> None:
        self.collections = {"pre-existing"}
        self.deleted: list[str] = []

    def collection_exists(self, collection: str) -> bool:
        return collection in self.collections

    def delete_collection(self, collection: str) -> None:
        self.deleted.append(collection)
        self.collections.remove(collection)


class _CalibrationIndex:
    def __init__(
        self,
        qdrant: _CalibrationQdrant,
        *,
        collection_name: str,
        dense_dimension: int,
        pipeline_fingerprint: str,
    ) -> None:
        del dense_dimension, pipeline_fingerprint
        self._qdrant = qdrant
        self.collection_name = collection_name

    def create_collection(self) -> None:
        self._qdrant.collections.add(self.collection_name)


@pytest.mark.parametrize("raises", (False, True))
def test_temporary_candidate_collection_is_always_isolated_and_deleted(
    monkeypatch: pytest.MonkeyPatch,
    raises: bool,
) -> None:
    pipeline = load_pipeline(
        Path(__file__).parents[1] / "deployment/config/pipeline.json"
    )
    qdrant = _CalibrationQdrant()
    monkeypatch.setattr(ablation, "QdrantIndex", _CalibrationIndex)

    def exercise() -> str:
        with ablation._temporary_candidate_index(
            cast(object, qdrant),
            pipeline,
            DEFAULT_SECTION_CANDIDATES[0],
        ) as index:
            assert index.collection_name.startswith("rag-ablation-")
            assert "pre-existing" in qdrant.collections
            if raises:
                raise RuntimeError("synthetic calibration failure")
            return index.collection_name

    if raises:
        with pytest.raises(RuntimeError, match="synthetic"):
            exercise()
        created = qdrant.deleted[0]
    else:
        created = exercise()

    assert qdrant.collections == {"pre-existing"}
    assert qdrant.deleted == [created]


def test_tuning_digest_ignores_holdout_label_enrichment() -> None:
    documents = {"synthetic": "synthetic.docx"}
    tuning_case = {
        "id": "Q001",
        "split": "tuning",
        "question": "公开调参问题",
    }
    first = canonical_tuning_digest(documents, (tuning_case,))
    second = canonical_tuning_digest(
        documents,
        (
            tuning_case,
            {
                "id": "Q058",
                "split": "holdout",
                "question": "补齐后的 OCR 问题",
                "validation_state": "verified_text",
            },
        ),
    )

    assert first == second
