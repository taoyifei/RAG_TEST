import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.http import models

from rag_app.contracts import (
    Chunk,
    ChunkRole,
    ChunkSourceSpan,
    ElementKind,
    Locator,
)
from rag_app.index import (
    IndexCoordinator,
    IndexedChunk,
    IndexResultState,
    QdrantIndex,
)
from rag_app.state import JobKind, SourceVersion, StateStore, VersionState

_API_KEY = "test-only-qdrant-key"
_DIMENSION = 1024
_PIPELINE_FINGERPRINT = "sha256:" + "f" * 64
_SOURCE_ID = "src_" + "1" * 32


def _client() -> QdrantClient:
    return QdrantClient(
        url="http://127.0.0.1:6333",
        api_key=_API_KEY,
        timeout=10,
        check_compatibility=False,
    )


def _indexed_chunk(
    version_hex: str,
    text: str,
    term_index: int,
) -> IndexedChunk:
    locator = Locator(
        file_path="规范.docx",
        heading_path=("总则",),
        paragraph_index=1,
        segment_index=1,
        fragment=text,
    )
    chunk = Chunk(
        chunk_id=f"chunk_{uuid.uuid5(uuid.NAMESPACE_URL, text).hex}",
        source_id=_SOURCE_ID,
        doc_version="sha256:" + version_hex * 64,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        section_id="section_" + "a" * 32,
        neighbor_group_id="group_" + "b" * 32,
        chunk_role=ChunkRole.TEXT,
        source_spans=(
            ChunkSourceSpan(
                element_id="element-indexed",
                locator=locator,
                start_char=0,
                end_char=len(text),
                source_start_char=0,
                source_end_char=len(text),
            ),
        ),
        text=text,
        embedding_text=f"总则\n{text}",
        element_kind=ElementKind.PARAGRAPH,
        locators=(locator,),
        content_sha256=version_hex * 64,
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    dense = [0.0] * _DIMENSION
    dense[0] = 1.0
    return IndexedChunk(
        chunk=chunk,
        dense=dense,
        sparse=models.SparseVector(
            indices=[term_index],
            values=[1.0],
        ),
    )


def _version_chunk(
    version: SourceVersion,
    text: str,
    *,
    chunk_id: str = "chunk_shared",
) -> IndexedChunk:
    locator = Locator(
        file_path=version.source_path,
        paragraph_index=1,
        segment_index=1,
        fragment=text,
    )
    chunk = Chunk(
        chunk_id=chunk_id,
        source_id=version.source_id,
        doc_version=version.doc_version,
        pipeline_fingerprint=version.pipeline_fingerprint,
        section_id="section_" + "a" * 32,
        neighbor_group_id="group_" + "b" * 32,
        chunk_role=ChunkRole.TEXT,
        source_spans=(
            ChunkSourceSpan(
                element_id="element-version",
                locator=locator,
                start_char=0,
                end_char=len(text),
                source_start_char=0,
                source_end_char=len(text),
            ),
        ),
        text=text,
        embedding_text=text,
        element_kind=ElementKind.PARAGRAPH,
        locators=(locator,),
        content_sha256=version.content_sha256,
        document_status="active",
        authority_level="official",
        effective_from=None,
        effective_to=None,
    )
    return IndexedChunk(
        chunk=chunk,
        dense=[1.0] + [0.0] * 1023,
        sparse=models.SparseVector(indices=[1], values=[1.0]),
    )


def _state_store(tmp_path: Path) -> StateStore:
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    return state


def test_real_qdrant_staging_activation_and_failure_cleanup() -> None:
    client = _client()
    collection = f"rag-index-test-{uuid.uuid4().hex}"
    index = QdrantIndex(
        client,
        collection_name=collection,
        dense_dimension=_DIMENSION,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    try:
        index.create_collection()
        info = client.get_collection(collection)
        assert info.config.metadata is not None
        assert info.config.metadata["payload_schema_version"] == "2"
        index.stage_chunks(
            [
                _indexed_chunk("a", "旧版本证据一", 1),
                _indexed_chunk("a", "旧版本证据二", 2),
            ]
        )
        assert (
            index.count_version(
                _SOURCE_ID,
                "sha256:" + "a" * 64,
                "staging",
            )
            == 2
        )
        index.activate_source_version(_SOURCE_ID, "sha256:" + "a" * 64)

        active = index.query_dense([1.0] + [0.0] * 1023, limit=10)
        assert active[0].payload["section_id"] == "section_" + "a" * 32
        assert active[0].payload["neighbor_group_id"] == "group_" + "b" * 32
        assert active[0].payload["chunk_role"] == "text"
        assert active[0].payload["source_spans"][0]["element_id"] == (
            "element-indexed"
        )
        assert {item.payload["text"] for item in active} == {
            "旧版本证据一",
            "旧版本证据二",
        }

        index.stage_chunks(
            [
                _indexed_chunk("b", "旧版本证据一", 1),
                _indexed_chunk("b", "新版本证据", 3),
            ]
        )
        active_during_staging = index.query_dense(
            [1.0] + [0.0] * 1023,
            limit=10,
        )
        assert {item.payload["text"] for item in active_during_staging} == {
            "旧版本证据一",
            "旧版本证据二",
        }
        index.activate_source_version(_SOURCE_ID, "sha256:" + "b" * 64)
        active = index.query_dense([1.0] + [0.0] * 1023, limit=10)
        assert {item.payload["text"] for item in active} == {
            "旧版本证据一",
            "新版本证据",
        }

        index.stage_chunks([_indexed_chunk("c", "失败版本证据", 4)])
        index.delete_staging(_SOURCE_ID, "sha256:" + "c" * 64)
        active = index.query_dense([1.0] + [0.0] * 1023, limit=10)
        assert {item.payload["text"] for item in active} == {
            "旧版本证据一",
            "新版本证据",
        }
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)


def test_real_qdrant_schema_alias_and_snapshot() -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    first_collection = f"rag-index-first-{suffix}"
    second_collection = f"rag-index-second-{suffix}"
    alias = f"rag-active-{suffix}"
    first = QdrantIndex(
        client,
        collection_name=first_collection,
        dense_dimension=_DIMENSION,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    second = QdrantIndex(
        client,
        collection_name=second_collection,
        dense_dimension=_DIMENSION,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    try:
        first.create_collection()
        second.create_collection()
        second.stage_chunks([_indexed_chunk("a", "快照证据", 1)])
        second.activate_source_version(
            _SOURCE_ID,
            "sha256:" + "a" * 64,
        )
        first.switch_alias(alias)
        second.switch_alias(alias)

        aliases = client.get_aliases().aliases
        target = next(
            item.collection_name
            for item in aliases
            if item.alias_name == alias
        )
        assert target == second_collection

        snapshot = second.create_snapshot()
        assert snapshot.name.endswith(".snapshot")
        assert snapshot.size is not None
        assert snapshot.size > 0
        assert snapshot.checksum is not None

        client.delete_collection(second_collection)
        second.recover_snapshot(
            snapshot_name=snapshot.name,
            checksum=snapshot.checksum,
        )
        restored = second.query_dense(
            [1.0] + [0.0] * 1023,
            limit=10,
        )
        assert [point.payload["text"] for point in restored] == ["快照证据"]
        second.switch_alias(alias)
        target = next(
            item.collection_name
            for item in client.get_aliases().aliases
            if item.alias_name == alias
        )
        assert target == second_collection
    finally:
        if client.collection_exists(first_collection):
            client.delete_collection(first_collection)
        if client.collection_exists(second_collection):
            client.delete_collection(second_collection)


def test_coordinator_is_idempotent_and_preserves_old_on_failure(
    tmp_path: Path,
) -> None:
    client = _client()
    collection = f"rag-index-coordinator-{uuid.uuid4().hex}"
    index = QdrantIndex(
        client,
        collection_name=collection,
        dense_dimension=_DIMENSION,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    state = _state_store(tmp_path)
    coordinator = IndexCoordinator(state, index)
    try:
        index.create_collection()
        first_job = state.create_job(
            idempotency_key="incremental:first",
            kind=JobKind.INCREMENTAL,
            pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        )
        first = coordinator.index_source(
            job_id=first_job.job_id,
            source_path="规范.docx",
            content_sha256="a" * 64,
            build_chunks=lambda version: [_version_chunk(version, "稳定证据")],
        )
        repeated = coordinator.index_source(
            job_id=first_job.job_id,
            source_path="规范.docx",
            content_sha256="a" * 64,
            build_chunks=lambda version: [_version_chunk(version, "稳定证据")],
        )

        assert first.state == IndexResultState.ACTIVATED
        assert repeated.state == IndexResultState.UNCHANGED
        assert client.count(collection, exact=True).count == 1
        index.rename_source(first.source_id, "新规范.docx")
        renamed_source_id = state.apply_rename_if_unique(
            new_path="新规范.docx",
            content_sha256="a" * 64,
        )
        assert renamed_source_id == first.source_id
        renamed_points = index.query_dense(
            [1.0] + [0.0] * 1023,
            limit=10,
        )
        assert renamed_points[0].payload["source_path"] == "新规范.docx"
        assert (
            renamed_points[0].payload["locators"][0]["file_path"]
            == "新规范.docx"
        )
        assert (
            renamed_points[0].payload["source_spans"][0]["locator"][
                "file_path"
            ]
            == "新规范.docx"
        )

        failed_job = state.create_job(
            idempotency_key="incremental:failed-update",
            kind=JobKind.INCREMENTAL,
            pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        )

        def fail_build(_: SourceVersion) -> list[IndexedChunk]:
            raise TimeoutError("test-only")

        try:
            coordinator.index_source(
                job_id=failed_job.job_id,
                source_path="新规范.docx",
                content_sha256="b" * 64,
                build_chunks=fail_build,
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("构建失败必须传播。")

        active = index.query_dense([1.0] + [0.0] * 1023, limit=10)
        assert [point.payload["text"] for point in active] == ["稳定证据"]
        failed = state.get_source_version(
            first.source_id,
            "sha256:" + "b" * 64,
        )
        assert failed.state == VersionState.FAILED

        coordinator.delete_source(first.source_id)
        coordinator.delete_source(first.source_id)
        assert index.query_dense(
            [1.0] + [0.0] * 1023,
            limit=10,
        ) == []
        assert state.list_active_sources() == ()
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)


def test_real_qdrant_rejects_collection_without_payload_schema_v2() -> None:
    client = _client()
    collection = f"rag-index-legacy-{uuid.uuid4().hex}"
    try:
        client.create_collection(
            collection_name=collection,
            vectors_config={
                "dense": models.VectorParams(
                    size=_DIMENSION,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                "bm25": models.SparseVectorParams(
                    modifier=models.Modifier.IDF,
                )
            },
            metadata={"pipeline_fingerprint": _PIPELINE_FINGERPRINT},
        )
        index = QdrantIndex(
            client,
            collection_name=collection,
            dense_dimension=_DIMENSION,
            pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        )

        try:
            index.require_compatible_collection()
        except ValueError as error:
            assert "payload schema" in str(error)
        else:
            raise AssertionError("缺少 payload schema v2 必须失败关闭。")
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)


def test_alias_bound_runtime_rejects_old_payload_schema() -> None:
    client = _client()
    suffix = uuid.uuid4().hex
    collection = f"rag-index-runtime-legacy-{suffix}"
    alias = f"rag-index-runtime-alias-{suffix}"
    try:
        client.create_collection(
            collection_name=collection,
            vectors_config={
                "dense": models.VectorParams(
                    size=_DIMENSION,
                    distance=models.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                "bm25": models.SparseVectorParams(
                    modifier=models.Modifier.IDF,
                )
            },
            metadata={"pipeline_fingerprint": _PIPELINE_FINGERPRINT},
        )
        client.update_collection_aliases(
            [
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(
                        collection_name=collection,
                        alias_name=alias,
                    )
                )
            ]
        )
        runtime_index = QdrantIndex(
            client,
            collection_name=alias,
            dense_dimension=_DIMENSION,
            pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        )

        try:
            runtime_index.alias_target(alias)
        except ValueError as error:
            assert "payload schema" in str(error)
        else:
            raise AssertionError(
                "runtime 绑定 alias 时必须拒绝旧 payload schema。"
            )
    finally:
        aliases = {
            item.alias_name for item in client.get_aliases().aliases
        }
        if alias in aliases:
            client.update_collection_aliases(
                [
                    models.DeleteAliasOperation(
                        delete_alias=models.DeleteAlias(alias_name=alias)
                    )
                ]
            )
        if client.collection_exists(collection):
            client.delete_collection(collection)


def test_coordinator_recovers_qdrant_activation_after_process_crash(
    tmp_path: Path,
) -> None:
    client = _client()
    collection = f"rag-index-recovery-{uuid.uuid4().hex}"
    index = QdrantIndex(
        client,
        collection_name=collection,
        dense_dimension=_DIMENSION,
        pipeline_fingerprint=_PIPELINE_FINGERPRINT,
    )
    state = _state_store(tmp_path)
    try:
        index.create_collection()
        job = state.create_job(
            idempotency_key="incremental:crash",
            kind=JobKind.INCREMENTAL,
            pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        )
        version = state.stage_source_version(
            job_id=job.job_id,
            source_path="恢复.docx",
            content_sha256="c" * 64,
            pipeline_fingerprint=_PIPELINE_FINGERPRINT,
        )
        index.stage_chunks([_version_chunk(version, "崩溃前已写入")])
        state.record_staged_chunk_count(
            version.source_id,
            version.doc_version,
            1,
        )
        index.activate_source_version(
            version.source_id,
            version.doc_version,
        )

        def must_not_rebuild(_: SourceVersion) -> list[IndexedChunk]:
            raise AssertionError("恢复路径不应重复解析或编码。")

        result = IndexCoordinator(state, index).index_source(
            job_id=job.job_id,
            source_path="恢复.docx",
            content_sha256="c" * 64,
            build_chunks=must_not_rebuild,
        )

        assert result.state == IndexResultState.RECOVERED
        active = state.get_active_source(version.source_id)
        assert active is not None
        assert active.doc_version == version.doc_version
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
