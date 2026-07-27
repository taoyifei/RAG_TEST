import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rag_app.contracts import (
    IndexManifest,
    PipelineSpec,
    SourceRecord,
)
from rag_app.manifest import ManifestRepository, ManifestState


def _pipeline() -> PipelineSpec:
    return PipelineSpec(
        schema_version="1",
        parser_revision="docx-parser-v1",
        ocr_model="pending-selection",
        ocr_revision="not-deployed",
        chunker_revision="structural-v1",
        chunker_parameters=(("target", "384"), ("hard_max", "512")),
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_revision="sha256:" + "e" * 64,
        embedding_dimension=1024,
        sparse_model="qdrant-bm25",
        sparse_revision="pending-benchmark",
        index_revision="qdrant-v1.18.3",
        reranker_model="Qwen3-Reranker-0.6B",
        reranker_revision="sha256:" + "r" * 64,
        llm_revisions=(("llm-58-8000", "unknown"),),
        prompt_revision="strict-citations-v1",
    )


def _manifest(collection_name: str) -> IndexManifest:
    pipeline = _pipeline()
    return IndexManifest(
        manifest_version="1",
        collection_name=collection_name,
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
        pipeline=pipeline,
        pipeline_fingerprint=pipeline.fingerprint(),
        sources=(
            SourceRecord(
                source_id="src_" + "1" * 32,
                current_path="规范.docx",
                content_sha256="a" * 64,
                doc_version="sha256:" + "a" * 64,
            ),
        ),
    )


def test_manifest_history_activation_and_atomic_export(
    tmp_path: Path,
) -> None:
    repository = ManifestRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    first = _manifest("rag-index-first")
    staged = repository.stage(
        first,
        snapshot_name="first.snapshot",
        snapshot_checksum="b" * 64,
    )

    assert staged.state == ManifestState.STAGING
    assert repository.get_active() is None

    repository.activate(first.collection_name)
    active = repository.get_active()
    assert active is not None
    assert active.manifest == first

    second = _manifest("rag-index-second")
    repository.stage(
        second,
        snapshot_name="second.snapshot",
        snapshot_checksum="c" * 64,
    )
    assert repository.get_active() == active

    export_path = tmp_path / "index-manifest.json"
    digest = repository.export_active(export_path)
    assert hashlib.sha256(export_path.read_bytes()).hexdigest() == digest
    reopened = ManifestRepository(tmp_path / "state.sqlite3")
    reopened.initialize()
    assert reopened.get_active() == active


def test_manifest_rejects_incompatible_runtime(tmp_path: Path) -> None:
    repository = ManifestRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    manifest = _manifest("rag-index")
    repository.stage(
        manifest,
        snapshot_name="index.snapshot",
        snapshot_checksum="d" * 64,
    )
    repository.activate(manifest.collection_name)

    repository.require_compatible(
        collection_name="rag-index",
        pipeline_fingerprint=manifest.pipeline_fingerprint,
    )
    with pytest.raises(ValueError, match="pipeline"):
        repository.require_compatible(
            collection_name="rag-index",
            pipeline_fingerprint="sha256:" + "0" * 64,
        )
    with pytest.raises(ValueError, match="collection"):
        repository.require_compatible(
            collection_name="rag-other",
            pipeline_fingerprint=manifest.pipeline_fingerprint,
        )
