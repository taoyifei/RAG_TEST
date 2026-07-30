import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from rag_app.contracts import IndexManifest, PipelineSpec
from rag_app.index import FullIndexPublisher, PublishState, QdrantIndex
from rag_app.manifest import ManifestRepository, ManifestState
from rag_app.state.lease import LeaseLostError

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
        schema_version="2",
        parser_revision="docx-parser-v1",
        ocr_model="pending-selection",
        ocr_revision="not-deployed",
        chunker_revision="structural-v1",
        chunker_parameters=(
            ("target_tokens", "384"),
            ("hard_max_tokens", "512"),
            ("overlap_tokens", "64"),
        ),
        embedding_model="Qwen3-Embedding-0.6B",
        embedding_revision="unknown",
        embedding_dimension=1024,
        sparse_model="qdrant-bm25",
        sparse_revision="pending-benchmark",
        index_revision="qdrant-v1.18.3",
        reranker_model="Qwen3-Reranker-0.6B",
        reranker_revision="unknown",
        llm_model="llm-58-8000",
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


def test_publish_recovers_staged_snapshot_with_one_alias_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-stage-before-alias-{suffix}"
    base_name = f"rag-stage-base-{suffix}"
    target_name = f"rag-stage-target-{suffix}"
    repository = ManifestRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    base_manifest = _manifest(base_name)
    target_manifest = _manifest(target_name)
    base = QdrantIndex(
        client,
        collection_name=base_name,
        dense_dimension=1024,
        pipeline_fingerprint=base_manifest.pipeline_fingerprint,
    )
    target = QdrantIndex(
        client,
        collection_name=target_name,
        dense_dimension=1024,
        pipeline_fingerprint=target_manifest.pipeline_fingerprint,
    )
    try:
        base.create_collection()
        target.create_collection()
        FullIndexPublisher(repository, base, alias_name=alias).publish(
            base_manifest
        )
        snapshot = target.create_snapshot()
        assert snapshot.checksum is not None
        repository.stage(
            target_manifest,
            snapshot_name=snapshot.name,
            snapshot_checksum=snapshot.checksum,
        )
        switches: list[tuple[str, str]] = []
        original_switch = QdrantIndex.switch_alias

        def record_switch(index: QdrantIndex, alias_name: str) -> None:
            switches.append((index.collection_name, alias_name))
            original_switch(index, alias_name)

        monkeypatch.setattr(QdrantIndex, "switch_alias", record_switch)
        publisher = FullIndexPublisher(repository, target, alias_name=alias)

        assert (
            publisher.publish(target_manifest).state
            == PublishState.PUBLISHED
        )
        assert switches == [(target_name, alias)]
        assert (
            publisher.publish(target_manifest).state
            == PublishState.UNCHANGED
        )
        assert switches == [(target_name, alias)]
    finally:
        if client.collection_exists(base_name):
            client.delete_collection(base_name)
        if client.collection_exists(target_name):
            client.delete_collection(target_name)


@pytest.mark.parametrize("loss_point", ["before_alias", "after_alias"])
def test_publish_lease_loss_restores_old_alias_and_manifest(
    tmp_path: Path,
    loss_point: str,
) -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    alias = f"rag-lease-publish-{suffix}"
    base_name = f"rag-lease-base-{suffix}"
    target_name = f"rag-lease-target-{suffix}"
    repository = ManifestRepository(tmp_path / "state.sqlite3")
    repository.initialize()
    base_manifest = _manifest(base_name)
    target_manifest = _manifest(target_name)
    base = QdrantIndex(
        client,
        collection_name=base_name,
        dense_dimension=1024,
        pipeline_fingerprint=base_manifest.pipeline_fingerprint,
    )
    target = QdrantIndex(
        client,
        collection_name=target_name,
        dense_dimension=1024,
        pipeline_fingerprint=target_manifest.pipeline_fingerprint,
    )
    try:
        base.create_collection()
        target.create_collection()
        FullIndexPublisher(repository, base, alias_name=alias).publish(
            base_manifest
        )

        def reject_publish() -> None:
            staged = repository.get(target_name)
            alias_target = target.alias_target(alias)
            if loss_point == "before_alias" and staged is not None:
                raise LeaseLostError("LEASE_LOST")
            if loss_point == "after_alias" and alias_target == target_name:
                raise LeaseLostError("LEASE_LOST")

        with pytest.raises(LeaseLostError, match="LEASE_LOST"):
            FullIndexPublisher(
                repository,
                target,
                alias_name=alias,
            ).publish(target_manifest, lease_guard=reject_publish)

        active = repository.get_active()
        assert active is not None
        assert active.manifest == base_manifest
        assert target.alias_target(alias) == base_name
        staged = repository.get(target_name)
        assert staged is not None
        assert staged.state == ManifestState.STAGING
    finally:
        if client.collection_exists(base_name):
            client.delete_collection(base_name)
        if client.collection_exists(target_name):
            client.delete_collection(target_name)
