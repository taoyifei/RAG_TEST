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

from rag_app.active_evidence import (
    ActiveEvidenceExporter,
    ActiveEvidenceRecord,
)
from rag_app.contracts import (
    Chunk,
    ChunkIdentity,
    ChunkRole,
    ChunkSourceSpan,
    ElementKind,
    IndexManifest,
    Locator,
    PipelineSpec,
    SourceRecord,
    stable_chunk_id,
)
from rag_app.index.qdrant import IndexedChunk, QdrantIndex
from rag_app.manifest import ManifestRepository, StoredManifest

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
        first_text = f"可信证据第 {index} 条"
        second_text = f"补充条款 {index}"
        text = f"{first_text}；{second_text}"
        first_locator = Locator(
            file_path="规范.docx",
            heading_path=("总则",),
            heading_index=1,
            paragraph_index=(index * 2) - 1,
            segment_index=1,
            fragment=first_text,
        )
        second_locator = Locator(
            file_path="规范.docx",
            heading_path=("总则",),
            heading_index=1,
            paragraph_index=index * 2,
            segment_index=1,
            fragment=second_text,
        )
        section_id = "section_" + "a" * 32
        group_id = "group_" + "b" * 32
        source_spans = (
            ChunkSourceSpan(
                element_id=f"element-{index}",
                locator=first_locator,
                start_char=0,
                end_char=len(first_text),
                source_start_char=0,
                source_end_char=len(first_text),
            ),
            ChunkSourceSpan(
                element_id=f"element-{index}-second",
                locator=second_locator,
                start_char=len(first_text) + 1,
                end_char=len(text),
                source_start_char=0,
                source_end_char=len(second_text),
            ),
        )
        chunk = Chunk(
            chunk_id=stable_chunk_id(
                _SOURCE_ID,
                ChunkIdentity(
                    section_id=section_id,
                    neighbor_group_id=group_id,
                    chunk_role=ChunkRole.TEXT,
                    source_spans=source_spans,
                ),
                text,
            ),
            source_id=_SOURCE_ID,
            doc_version=_DOC_VERSION,
            pipeline_fingerprint=pipeline_fingerprint,
            section_id=section_id,
            neighbor_group_id=group_id,
            chunk_role=ChunkRole.TEXT,
            source_spans=source_spans,
            text=text,
            embedding_text=text,
            element_kind=ElementKind.PARAGRAPH,
            locators=(first_locator, second_locator),
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
        manifest = ActiveEvidenceExporter(
            active.client,
            active.repository,
            alias_name=active.alias_name,
            page_size=2,
        ).export()

        assert manifest.point_count == 5
        assert manifest.schema_version == "2"
        assert len(manifest.records) == 5
        assert {
            record.chunk_id for record in manifest.records
        } == set(active.chunk_ids)
        assert len(manifest.records[0].source_spans) == 2
        assert len(manifest.records[0].locators) == 2


@pytest.mark.parametrize(
    "field",
    [
        "locators",
        "source_spans_second_locator",
        "source_spans_later_range",
        "section_id",
        "neighbor_group_id",
        "chunk_role",
        "text",
        "content_sha256",
    ],
)
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
        elif field == "source_spans_second_locator":
            source_spans = list(point.payload["source_spans"])
            second = dict(source_spans[1])
            second_locator = dict(second["locator"])
            second_locator["paragraph_index"] = 99
            second["locator"] = second_locator
            source_spans[1] = second
            value = source_spans
            field = "source_spans"
        elif field == "source_spans_later_range":
            source_spans = list(point.payload["source_spans"])
            second = dict(source_spans[1])
            second["source_start_char"] = (
                int(second["source_start_char"]) + 1
            )
            second["source_end_char"] = int(second["source_end_char"]) + 1
            source_spans[1] = second
            value = source_spans
            field = "source_spans"
        elif field == "section_id":
            value = "section_" + "f" * 32
        elif field == "neighbor_group_id":
            value = "group_" + "f" * 32
        elif field == "chunk_role":
            value = "ocr"
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


def test_real_qdrant_export_rejects_wrong_payload_schema_metadata(
    tmp_path: Path,
) -> None:
    with _active_index(tmp_path) as active:
        active.client.update_collection(
            collection_name=active.index.collection_name,
            metadata={
                "pipeline_fingerprint": _pipeline().fingerprint(),
                "schema_version": "1",
                "payload_schema_version": "1",
            },
        )

        with pytest.raises(ValueError, match="payload schema"):
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

        manifest = ActiveEvidenceExporter(
            active.client,
            active.repository,
            alias_name=active.alias_name,
            page_size=2,
        ).export()

        assert manifest.point_count == 4
        assert retired_id not in {
            record.chunk_id for record in manifest.records
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


def test_real_qdrant_export_rejects_alias_switch_during_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实扫描结束前切换 alias 时必须失败关闭。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的局部替换器。

    Returns:
        无返回值。

    """
    with _active_index(tmp_path) as active:
        alternate = QdrantIndex(
            active.client,
            collection_name=f"rag-evidence-alt-{uuid.uuid4().hex}",
            dense_dimension=_DIMENSION,
            pipeline_fingerprint=_pipeline().fingerprint(),
        )
        alternate.create_collection()
        exporter = ActiveEvidenceExporter(
            active.client,
            active.repository,
            alias_name=active.alias_name,
            page_size=2,
        )
        original_scan = exporter._scroll_records

        def scan_then_switch(
            collection_name: str,
            stored: StoredManifest,
        ) -> tuple[ActiveEvidenceRecord, ...]:
            """执行真实分页后切换 alias。

            Args:
                collection_name: 扫描开始时解析出的物理 collection。
                stored: 扫描开始时读取的活动 manifest。

            Returns:
                真实 Qdrant 分页返回的证据记录。

            """
            records = original_scan(collection_name, stored)
            active.index.switch_alias_to(
                active.alias_name,
                alternate.collection_name,
            )
            return records

        monkeypatch.setattr(exporter, "_scroll_records", scan_then_switch)
        try:
            with pytest.raises(ValueError, match=r"collection|状态"):
                exporter.export()
        finally:
            active.index.switch_alias(active.alias_name)
            if active.client.collection_exists(
                alternate.collection_name
            ):
                active.client.delete_collection(
                    alternate.collection_name
                )


def test_real_qdrant_export_rejects_manifest_switch_during_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实扫描期间 ACTIVE manifest 摘要变化时必须失败关闭。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的局部替换器。

    Returns:
        无返回值。

    """
    with _active_index(tmp_path) as active:
        exporter = ActiveEvidenceExporter(
            active.client,
            active.repository,
            alias_name=active.alias_name,
            page_size=2,
        )
        original_scan = exporter._scroll_records

        def scan_then_change_manifest(
            collection_name: str,
            stored: StoredManifest,
        ) -> tuple[ActiveEvidenceRecord, ...]:
            """执行真实分页后写入新的活动 manifest revision。

            Args:
                collection_name: 扫描开始时解析出的物理 collection。
                stored: 扫描开始时读取的活动 manifest。

            Returns:
                真实 Qdrant 分页返回的证据记录。

            """
            records = original_scan(collection_name, stored)
            changed = stored.manifest.model_copy(
                update={
                    "created_at": datetime(
                        2026,
                        7,
                        29,
                        tzinfo=UTC,
                    )
                }
            )
            active.repository.record_active_revision(
                changed,
                snapshot_name="changed.snapshot",
                snapshot_checksum="c" * 64,
            )
            return records

        monkeypatch.setattr(
            exporter,
            "_scroll_records",
            scan_then_change_manifest,
        )

        with pytest.raises(ValueError, match="状态"):
            exporter.export()


def test_real_qdrant_export_rejects_point_count_change_after_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实扫描结束后活动 point 数变化时必须失败关闭。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的局部替换器。

    Returns:
        无返回值。

    """
    with _active_index(tmp_path) as active:
        points, _ = active.client.scroll(
            collection_name=active.index.collection_name,
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        exporter = ActiveEvidenceExporter(
            active.client,
            active.repository,
            alias_name=active.alias_name,
            page_size=2,
        )
        original_scan = exporter._scroll_records

        def scan_then_delete_point(
            collection_name: str,
            stored: StoredManifest,
        ) -> tuple[ActiveEvidenceRecord, ...]:
            """执行真实分页后删除一个活动 point。

            Args:
                collection_name: 扫描开始时解析出的物理 collection。
                stored: 扫描开始时读取的活动 manifest。

            Returns:
                删除前真实 Qdrant 分页返回的证据记录。

            """
            records = original_scan(collection_name, stored)
            active.client.delete(
                collection_name=collection_name,
                points_selector=models.PointIdsList(
                    points=[points[0].id]
                ),
                wait=True,
            )
            return records

        monkeypatch.setattr(
            exporter,
            "_scroll_records",
            scan_then_delete_point,
        )

        with pytest.raises(ValueError, match=r"状态|计数"):
            exporter.export()
