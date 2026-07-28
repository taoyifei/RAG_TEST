import hashlib
from datetime import UTC, datetime

from rag_app.active_evidence import (
    ActiveEvidenceManifest,
    ActiveEvidenceRecord,
    TrustedActiveEvidence,
    verify_exported_active_evidence,
)
from rag_app.contracts import IndexManifest, PipelineSpec, SourceRecord
from rag_app.manifest import (
    ManifestState,
    StoredManifest,
    index_manifest_sha256,
)


def active_evidence_record(
    *,
    chunk_id: str,
    source_path: str,
    locator: str,
    text: str,
) -> ActiveEvidenceRecord:
    source_digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()
    return ActiveEvidenceRecord(
        chunk_id=chunk_id,
        source_id=f"src_{source_digest[:32]}",
        source_path=source_path,
        doc_version=f"sha256:{source_digest}",
        locator=locator,
        text=text,
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def trusted_active_evidence(
    records: tuple[ActiveEvidenceRecord, ...],
) -> TrustedActiveEvidence:
    pipeline = _pipeline()
    sources_by_id = {
        record.source_id: SourceRecord(
            source_id=record.source_id,
            current_path=record.source_path,
            content_sha256=record.doc_version.removeprefix("sha256:"),
            doc_version=record.doc_version,
        )
        for record in records
    }
    index_manifest = IndexManifest(
        manifest_version="1",
        collection_name="test-active-collection",
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
        pipeline=pipeline,
        pipeline_fingerprint=pipeline.fingerprint(),
        sources=tuple(
            sorted(sources_by_id.values(), key=lambda item: item.source_id)
        ),
    )
    index_digest = index_manifest_sha256(index_manifest)
    manifest = ActiveEvidenceManifest.create(
        collection_name=index_manifest.collection_name,
        index_manifest_sha256=index_digest,
        pipeline_fingerprint=index_manifest.pipeline_fingerprint,
        records=records,
    )
    stored = StoredManifest(
        manifest=index_manifest,
        manifest_sha256=index_digest,
        state=ManifestState.ACTIVE,
        snapshot_name="test.snapshot",
        snapshot_checksum="a" * 64,
    )
    return verify_exported_active_evidence(manifest, stored)


def _pipeline() -> PipelineSpec:
    return PipelineSpec(
        schema_version="2",
        parser_revision="test-docx-parser",
        ocr_model="test-ocr",
        ocr_revision="test-ocr-revision",
        chunker_revision="test-chunker",
        chunker_parameters=(
            ("target_tokens", "64"),
            ("hard_max_tokens", "64"),
            ("overlap_tokens", "8"),
        ),
        embedding_model="test-embedding",
        embedding_revision="test-embedding-revision",
        embedding_dimension=4,
        sparse_model="test-bm25",
        sparse_revision="test-bm25-revision",
        index_revision="test-index-revision",
        reranker_model="test-reranker",
        reranker_revision="test-reranker-revision",
        llm_model="test-llm",
        llm_revisions=(("test-llm", "test-revision"),),
        prompt_revision="test-prompt",
    )
