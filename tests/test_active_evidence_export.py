import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.active_evidence import ActiveEvidenceExporter
from rag_app.contracts import (
    Chunk,
    ElementKind,
    IndexManifest,
    Locator,
    PipelineSpec,
    SourceRecord,
    stable_chunk_id,
)
from rag_app.index.qdrant import IndexedChunk, QdrantIndex
from rag_app.manifest import ManifestRepository

_API_KEY = "test-only-qdrant-key"
_DIMENSION = 4
_SOURCE_ID = "src_" + ("9" * 32)
_DOC_HEX = "d" * 64
_DOC_VERSION = f"sha256:{_DOC_HEX}"


@dataclass(frozen=True)
class _ActiveIndex:
    client: QdrantClient
    index: QdrantIndex
    repository: ManifestRepository
    alias_name: str
    chunk_ids: tuple[str, ...]


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _pipeline(revision: str = "pipeline-a") -> PipelineSpec:
    return PipelineSpec(
        schema_version="2",
        parser_revision="docx-parser-v2",
        ocr_model="paddleocr",
        ocr_revision="paddleocr-v1",
        chunker_revision="chunker-v1",
        chunker_parameters=(
            ("target_tokens", "64"),
            ("hard_max_tokens", "64"),
            ("overlap_tokens", "8"),
        ),
        embedding_model="embedding",
        embedding_revision=revision,
        embedding_dimension=_DIMENSION,
        sparse_model="bm25",
        sparse_revision="bm25-v1",
        index_revision="qdrant-v1",
        reranker_model="reranker",
        reranker_revision="reranker-v1",
        llm_model="llm",
        llm_revisions=(("llm", "llm-v1"),),
        prompt_revision="prompt-v1",
    )


def _manifest(
    collection_name: str,
    pipeline: PipelineSpec,
) -> IndexManifest:
    return IndexManifest(
        manifest_version="1",
        collection_name=collection_name,
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
        pipeline=pipeline,
        pipeline_fingerprint=pipeline.fingerprint(),
        sources=(
            SourceRecord(
                source_id=_SOURCE_ID,
                current_path="规范.docx",
                content_sha256=_DOC_HEX,
                doc_version=_DOC_VERSION,
            ),
        ),
    )


def _indexed_chunks(
    pipeline_fingerprint: str,
    count: int = 5,
) -> tuple[IndexedChunk, ...]:
    indexed = []
    for index in range(1, count + 1):
        text = f"可信证据第 {index} 条"
        locator = Locator(
            file_path="规范.docx",
            heading_path=("总则",),
            heading_index=1,
            paragraph_index=index,
            segment_index=1,
            fragment=text,
        )
        chunk = Chunk(
            chunk_id=stable_chunk_id(_SOURCE_ID, locator, text),
            source_id=_SOURCE_ID,
            doc_version=_DOC_VERSION,
            pipeline_fingerprint=pipeline_fingerprint,
            text=text,
            embedding_text=text,
            element_kind=ElementKind.PARAGRAPH,
            locators=(locator,),
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            document_status="active",
            authority_level="official",
            effective_from=None,
            effective_to=None,
        )
        indexed.append(
            IndexedChunk(
                chunk=chunk,
                dense=[1.0, 0.0, 0.0, 0.0],
                sparse=models.SparseVector(
                    indices=[index],
                    values=[1.0],
                ),
            )
        )
    return tuple(indexed)


@contextmanager
def _active_index(
    tmp_path: Path,
    *,
    manifest_pipeline: PipelineSpec | None = None,
) -> Iterator[_ActiveIndex]:
    client = _client()
    suffix = uuid.uuid4().hex
    collection_name = f"rag-evidence-{suffix}"
    alias_name = f"rag-evidence-active-{suffix}"
    indexed_pipeline = _pipeline()
    index = QdrantIndex(
        client,
        collection_name=collection_name,
        dense_dimension=_DIMENSION,
        pipeline_fingerprint=indexed_pipeline.fingerprint(),
    )
    repository = ManifestRepository(tmp_path / f"{suffix}.sqlite3")
    try:
        index.create_collection()
        chunks = _indexed_chunks(indexed_pipeline.fingerprint())
        index.stage_chunks(chunks)
        index.activate_source_version(_SOURCE_ID, _DOC_VERSION)
        index.switch_alias(alias_name)
        repository.initialize()
        manifest = _manifest(
            collection_name,
            manifest_pipeline or indexed_pipeline,
        )
        repository.stage(
            manifest,
            snapshot_name="active.snapshot",
            snapshot_checksum="a" * 64,
        )
        repository.activate(collection_name)
        yield _ActiveIndex(
            client=client,
            index=index,
            repository=repository,
            alias_name=alias_name,
            chunk_ids=tuple(item.chunk.chunk_id for item in chunks),
        )
    finally:
        index.delete_alias(alias_name)
        if client.collection_exists(collection_name):
            client.delete_collection(collection_name)


def test_real_qdrant_export_paginates_without_duplicates(
    tmp_path: Path,
) -> None:
    with _active_index(tmp_path) as active:
        trusted = ActiveEvidenceExporter(
            active.client,
            active.repository,
            alias_name=active.alias_name,
            page_size=2,
        ).export()

        assert trusted.manifest.point_count == 5
        assert len(trusted.manifest.records) == 5
        assert {
            record.chunk_id for record in trusted.manifest.records
        } == set(active.chunk_ids)


@pytest.mark.parametrize("field", ["locators", "text", "content_sha256"])
def test_real_qdrant_export_rejects_tampered_payload(
    tmp_path: Path,
    field: str,
) -> None:
    with _active_index(tmp_path) as active:
        points, _ = active.client.scroll(
            collection_name=active.index.collection_name,
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        point = points[0]
        assert point.payload is not None
        if field == "locators":
            locators = list(point.payload["locators"])
            locators[0] = {**locators[0], "paragraph_index": 99}
            value: object = locators
        elif field == "text":
            value = "被篡改的文本"
        else:
            value = "0" * 64
        active.client.set_payload(
            collection_name=active.index.collection_name,
            payload={field: value},
            points=[point.id],
            wait=True,
        )

        with pytest.raises(ValueError):
            ActiveEvidenceExporter(
                active.client,
                active.repository,
                alias_name=active.alias_name,
            ).export()


def test_real_qdrant_export_rejects_old_collection(
    tmp_path: Path,
) -> None:
    with _active_index(tmp_path) as active:
        old_manifest = _manifest("rag-old-collection", _pipeline())
        active.repository.stage(
            old_manifest,
            snapshot_name="old.snapshot",
            snapshot_checksum="b" * 64,
        )
        active.repository.activate(old_manifest.collection_name)

        with pytest.raises(ValueError, match="collection"):
            ActiveEvidenceExporter(
                active.client,
                active.repository,
                alias_name=active.alias_name,
            ).export()


def test_real_qdrant_export_rejects_old_pipeline(
    tmp_path: Path,
) -> None:
    with _active_index(
        tmp_path,
        manifest_pipeline=_pipeline("pipeline-b"),
    ) as active, pytest.raises(ValueError, match="pipeline"):
        ActiveEvidenceExporter(
            active.client,
            active.repository,
            alias_name=active.alias_name,
        ).export()


def test_real_qdrant_export_excludes_retired_point(
    tmp_path: Path,
) -> None:
    with _active_index(tmp_path) as active:
        points, _ = active.client.scroll(
            collection_name=active.index.collection_name,
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        retired_id = str(points[0].payload["chunk_id"])
        active.client.set_payload(
            collection_name=active.index.collection_name,
            payload={"version_state": "retired"},
            points=[points[0].id],
            wait=True,
        )

        trusted = ActiveEvidenceExporter(
            active.client,
            active.repository,
            alias_name=active.alias_name,
            page_size=2,
        ).export()

        assert trusted.manifest.point_count == 4
        assert retired_id not in {
            record.chunk_id for record in trusted.manifest.records
        }


def test_real_qdrant_export_rejects_legacy_locator(
    tmp_path: Path,
) -> None:
    with _active_index(tmp_path) as active:
        points, _ = active.client.scroll(
            collection_name=active.index.collection_name,
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        point = points[0]
        assert point.payload is not None
        locators = list(point.payload["locators"])
        assert "heading_index" in locators[0]
        assert "segment_index" in locators[0]
        locators[0].pop("heading_index")
        locators[0].pop("segment_index")
        active.client.set_payload(
            collection_name=active.index.collection_name,
            payload={"locators": locators},
            points=[point.id],
            wait=True,
        )

        with pytest.raises(ValueError):
            ActiveEvidenceExporter(
                active.client,
                active.repository,
                alias_name=active.alias_name,
            ).export()
