import uuid
from datetime import UTC, datetime
from pathlib import Path

from qdrant_client import QdrantClient

from rag_app.contracts import IndexManifest, PipelineSpec
from rag_app.index import FullIndexPublisher, PublishState, QdrantIndex
from rag_app.manifest import ManifestRepository, ManifestState

_API_KEY = "test-only-qdrant-key"
_FINGERPRINT_SUFFIX = "f" * 64


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _pipeline() -> PipelineSpec:
    return PipelineSpec(
        schema_version="1",
        parser_revision="docx-parser-v1",
        ocr_model="pending-selection",
        ocr_revision="not-deployed",
        chunker_revision="structural-v1",
        chunker_parameters=(("target", "384"),),
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_revision="unknown",
        embedding_dimension=1024,
        sparse_model="qdrant-bm25",
        sparse_revision="pending-benchmark",
        index_revision="qdrant-v1.18.3",
        reranker_model="Qwen3-Reranker-0.6B",
        reranker_revision="unknown",
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
        sources=(),
    )


def test_full_publish_switches_alias_and_recovers_interrupted_confirmation(
    tmp_path: Path,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-active-publish-{suffix}"
    first_name = f"rag-publish-first-{suffix}"
    second_name = f"rag-publish-second-{suffix}"
    repository = ManifestRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    first_manifest = _manifest(first_name)
    second_manifest = _manifest(second_name)
    first = QdrantIndex(
        client,
        collection_name=first_name,
        dense_dimension=1024,
        pipeline_fingerprint=first_manifest.pipeline_fingerprint,
    )
    second = QdrantIndex(
        client,
        collection_name=second_name,
        dense_dimension=1024,
        pipeline_fingerprint=second_manifest.pipeline_fingerprint,
    )
    try:
        first.create_collection()
        second.create_collection()
        initial = FullIndexPublisher(repository, first, alias_name=alias)
        assert initial.publish(first_manifest).state == PublishState.PUBLISHED
        assert first.alias_target(alias) == first_name

        snapshot = second.create_snapshot()
        assert snapshot.checksum is not None
        repository.stage(
            second_manifest,
            snapshot_name=snapshot.name,
            snapshot_checksum=snapshot.checksum,
        )
        second.switch_alias(alias)

        recovered = FullIndexPublisher(
            repository,
            second,
            alias_name=alias,
        ).publish(second_manifest)

        assert recovered.state == PublishState.RECOVERED
        assert second.alias_target(alias) == second_name
        active = repository.get_active()
        assert active is not None
        assert active.state == ManifestState.ACTIVE
        assert active.manifest.collection_name == second_name

        repeated = FullIndexPublisher(
            repository,
            second,
            alias_name=alias,
        ).publish(second_manifest)
        assert repeated.state == PublishState.UNCHANGED
    finally:
        if client.collection_exists(first_name):
            client.delete_collection(first_name)
        if client.collection_exists(second_name):
            client.delete_collection(second_name)
