import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import rag_app.contracts as contracts_module
from rag_app.contracts import (
    IndexManifest,
    PipelineSpec,
    SourceRecord,
    allocate_source_id,
    content_doc_version,
)
from rag_app.runtime import load_pipeline
from rag_app.settings import ConfigurationState, RetrievalSettings

_LLM_TOKENIZER_SHA256 = (
    "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
)
_EMBEDDING_TOKENIZER_SHA256 = (
    "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"
)


def _pipeline_spec() -> PipelineSpec:
    return PipelineSpec(
        schema_version="2",
        parser_revision="docx-parser-v3",
        ocr_model="server-gpu-ocr-unselected",
        ocr_revision="unselected",
        chunker_revision="structural-v1",
        chunker_parameters=(
            ("hard_max_tokens", "512"),
            ("overlap_tokens", "64"),
            ("target_tokens", "384"),
        ),
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_revision="model-sha",
        embedding_dimension=1024,
        embedding_tokenizer_sha256=_EMBEDDING_TOKENIZER_SHA256,
        document_embedding_instruction="",
        sparse_model="bm25-chinese",
        sparse_revision="pending-benchmark",
        sparse_tokenizer="multilingual",
        sparse_language="none",
        index_revision="qdrant-v1.18.3",
        corpus_policy_sha256="c" * 64,
        ocr_minimum_confidence=0.8,
        reranker_model="Qwen3-Reranker-0.6B",
        reranker_revision="model-sha",
        llm_model="Qwen/Qwen3-8B-AWQ",
        llm_revisions=(("Qwen3-8B-AWQ", "pending-remote-revision"),),
        prompt_revision="strict-answer-v1",
        llm_tokenizer_sha256=_LLM_TOKENIZER_SHA256,
    )


def _retrieval_settings(
    *,
    status: ConfigurationState = ConfigurationState.FROZEN,
) -> RetrievalSettings:
    return RetrievalSettings(
        status=status,
        dense_limit=40,
        bm25_limit=40,
        rrf_rank_constant=60,
        candidate_limit=24,
        final_limit=6,
        max_final_limit=8,
        query_instruction="检索相关规范",
        max_history_turns=3,
        history_token_budget=512,
        max_question_tokens=512,
        rewrite_output_tokens=128,
        max_evidence_tokens=4096,
        low_ocr_threshold=0.8,
        answer_output_tokens=1024,
        repair_output_tokens=1024,
        conversation_ttl_seconds=1800,
        bm25_tokenizer="multilingual",
        bm25_language="none",
        allowed_statuses=("active",),
        allowed_authority_levels=("official",),
    )


def test_index_fingerprint_is_canonical_and_has_exact_boundary() -> None:
    pipeline = _pipeline_spec()
    reordered = pipeline.model_copy(
        update={
            "chunker_parameters": tuple(reversed(pipeline.chunker_parameters))
        }
    )
    changed = pipeline.model_copy(update={"parser_revision": "docx-parser-v4"})
    changed_index = pipeline.model_copy(
        update={"index_revision": "qdrant-v1.19.0"}
    )
    serving_only_changes = (
        {"prompt_revision": "strict-answer-v2"},
        {"reranker_revision": "reranker-v2"},
        {"llm_revisions": (("Qwen3-8B-AWQ", "llm-v2"),)},
        {"llm_tokenizer_sha256": "d" * 64},
    )
    index_changes = (
        {"schema_version": "3"},
        {"parser_revision": "docx-parser-v4"},
        {"ocr_model": "ocr-v2"},
        {"ocr_revision": "ocr-v2"},
        {"ocr_minimum_confidence": 0.9},
        {"chunker_revision": "structural-v2"},
        {
            "chunker_parameters": (
                ("hard_max_tokens", "512"),
                ("overlap_tokens", "32"),
                ("target_tokens", "384"),
            )
        },
        {"embedding_revision": "embedding-v2"},
        {"embedding_dimension": 2048},
        {"embedding_tokenizer_sha256": "e" * 64},
        {"document_embedding_instruction": "为文档建立向量"},
        {"sparse_model": "bm25-v2"},
        {"sparse_revision": "bm25-v2"},
        {"sparse_tokenizer": "word"},
        {"sparse_language": "zh"},
        {"index_revision": "qdrant-v1.19.0"},
        {"corpus_policy_sha256": "f" * 64},
    )

    assert pipeline.fingerprint() == reordered.fingerprint()
    assert pipeline.fingerprint() != changed.fingerprint()
    assert pipeline.fingerprint() != changed_index.fingerprint()
    assert all(
        pipeline.model_copy(update=change).fingerprint()
        == pipeline.fingerprint()
        for change in serving_only_changes
    )
    assert all(
        pipeline.model_copy(update=change).fingerprint()
        != pipeline.fingerprint()
        for change in index_changes
    )


def test_index_fingerprint_covers_metadata_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = _pipeline_spec()
    fingerprint = pipeline.fingerprint()

    monkeypatch.setattr(
        contracts_module,
        "DOCUMENT_STATUS_VALUES",
        frozenset({"active", "draft", "retired", "archived"}),
    )

    assert pipeline.fingerprint() != fingerprint


def test_pipeline_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / "deployment/config/pipeline.json").read_text(
        encoding="utf-8"
    )
    content = content.replace(
        '"parser_revision": "docx-parser-v3",',
        (
            '"parser_revision": "docx-parser-v3",'
            '"parser_revision": "docx-parser-v3",'
        ),
        1,
    )
    path = tmp_path / "pipeline.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="重复") as error:
        load_pipeline(path)
    message = str(error.value)
    assert "parser_revision" not in message
    assert "docx-parser-v3" not in message
    assert str(path) not in message


def test_retrieval_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / "deployment/config/retrieval.json").read_text(
        encoding="utf-8"
    )
    content = content.replace(
        '"dense_limit": 40,',
        '"dense_limit": 40,"dense_limit": 40,',
        1,
    )
    path = tmp_path / "retrieval.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="重复"):
        RetrievalSettings.load(path)


@pytest.mark.parametrize(
    "change",
    (
        {
            "chunker_parameters": (
                ("target_tokens", "384"),
                ("hard_max_tokens", "512"),
                ("target_tokens", "64"),
            )
        },
        {
            "llm_revisions": (
                ("llm-a", "revision-a"),
                ("llm-a", "revision-b"),
            )
        },
        {"embedding_revision": ""},
    ),
)
def test_pipeline_rejects_duplicate_or_empty_revision_fields(
    change: dict[str, object],
) -> None:
    payload = _pipeline_spec().model_dump(mode="python")
    payload.update(change)

    with pytest.raises(ValidationError):
        PipelineSpec.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("allowed_statuses", ()),
        ("allowed_statuses", ("active", "active")),
        ("allowed_statuses", ("published",)),
        ("allowed_authority_levels", ()),
        ("allowed_authority_levels", ("official", "official")),
        ("allowed_authority_levels", ("trusted",)),
    ),
)
def test_retrieval_rejects_invalid_metadata_filters(
    field: str,
    value: tuple[str, ...],
) -> None:
    payload = _retrieval_settings().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        RetrievalSettings.model_validate(payload)


def test_serving_fingerprint_covers_serving_fields_but_not_status() -> None:
    pipeline = _pipeline_spec()
    retrieval = _retrieval_settings()
    provisional = _retrieval_settings(status=ConfigurationState.PROVISIONAL)

    fingerprint = retrieval.serving_fingerprint(pipeline)

    assert fingerprint == provisional.serving_fingerprint(pipeline)
    assert fingerprint != retrieval.model_copy(
        update={"dense_limit": 41}
    ).serving_fingerprint(pipeline)
    assert fingerprint != retrieval.serving_fingerprint(
        pipeline.model_copy(update={"llm_tokenizer_sha256": "d" * 64})
    )


def test_manifest_rejects_mismatched_pipeline_fingerprint() -> None:
    pipeline = _pipeline_spec()
    source = SourceRecord(
        source_id=allocate_source_id("a.docx", "a" * 64),
        current_path="a.docx",
        content_sha256="a" * 64,
        doc_version=content_doc_version("a" * 64),
    )

    with pytest.raises(ValueError, match="pipeline_fingerprint"):
        IndexManifest(
            manifest_version="1",
            collection_name="rag-index-v1",
            created_at=datetime(2026, 7, 27, tzinfo=UTC),
            pipeline=pipeline,
            pipeline_fingerprint="sha256:" + "0" * 64,
            sources=(source,),
        )


def test_manifest_rejects_legacy_all_fields_fingerprint() -> None:
    pipeline = _pipeline_spec()
    legacy_payload = {
        name: getattr(pipeline, name)
        for name in (
            "schema_version",
            "parser_revision",
            "ocr_model",
            "ocr_revision",
            "chunker_revision",
            "chunker_parameters",
            "embedding_model",
            "embedding_revision",
            "embedding_dimension",
            "sparse_model",
            "sparse_revision",
            "index_revision",
            "reranker_model",
            "reranker_revision",
            "llm_revisions",
            "prompt_revision",
        )
    }
    legacy_payload["chunker_parameters"] = sorted(
        legacy_payload["chunker_parameters"]
    )
    legacy_payload["llm_revisions"] = sorted(
        legacy_payload["llm_revisions"]
    )
    canonical = json.dumps(
        legacy_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    legacy_fingerprint = (
        "sha256:"
        f"{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    )
    source = SourceRecord(
        source_id=allocate_source_id("a.docx", "a" * 64),
        current_path="a.docx",
        content_sha256="a" * 64,
        doc_version=content_doc_version("a" * 64),
    )

    with pytest.raises(ValueError, match="pipeline_fingerprint"):
        IndexManifest(
            manifest_version="1",
            collection_name="legacy-index",
            created_at=datetime(2026, 7, 27, tzinfo=UTC),
            pipeline=pipeline,
            pipeline_fingerprint=legacy_fingerprint,
            sources=(source,),
        )


def test_checked_in_pipeline_pins_both_tokenizer_digests() -> None:
    root = Path(__file__).resolve().parents[1]
    pipeline = load_pipeline(root / "deployment/config/pipeline.json")

    assert pipeline.llm_tokenizer_sha256 == _LLM_TOKENIZER_SHA256
    assert (
        pipeline.embedding_tokenizer_sha256
        == _EMBEDDING_TOKENIZER_SHA256
    )


def test_source_id_survives_manifest_path_update() -> None:
    source_id = allocate_source_id("旧名称.docx", "a" * 64)
    old_source = SourceRecord(
        source_id=source_id,
        current_path="旧名称.docx",
        content_sha256="a" * 64,
        doc_version=content_doc_version("a" * 64),
    )
    renamed_source = old_source.model_copy(
        update={"current_path": "新名称.docx"}
    )

    assert renamed_source.source_id == source_id
    assert renamed_source.doc_version == "sha256:" + "a" * 64
