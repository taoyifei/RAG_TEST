from datetime import UTC, datetime

import pytest

from rag_app.contracts import (
    IndexManifest,
    PipelineSpec,
    SourceRecord,
    allocate_source_id,
    content_doc_version,
)


def _pipeline_spec() -> PipelineSpec:
    return PipelineSpec(
        schema_version="1",
        parser_revision="docx-parser-v2",
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
        sparse_model="bm25-chinese",
        sparse_revision="pending-benchmark",
        index_revision="qdrant-v1.18.3",
        reranker_model="Qwen3-Reranker-0.6B",
        reranker_revision="model-sha",
        llm_revisions=(("Qwen3-8B-AWQ", "pending-remote-revision"),),
        prompt_revision="strict-answer-v1",
    )


def test_pipeline_fingerprint_is_canonical_and_revision_sensitive() -> None:
    pipeline = _pipeline_spec()
    reordered = pipeline.model_copy(
        update={
            "chunker_parameters": tuple(reversed(pipeline.chunker_parameters))
        }
    )
    changed = pipeline.model_copy(update={"parser_revision": "docx-parser-v3"})
    changed_index = pipeline.model_copy(
        update={"index_revision": "qdrant-v1.19.0"}
    )

    assert pipeline.fingerprint() == reordered.fingerprint()
    assert pipeline.fingerprint() != changed.fingerprint()
    assert pipeline.fingerprint() != changed_index.fingerprint()


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
