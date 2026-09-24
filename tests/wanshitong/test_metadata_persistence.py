"""WB-06 元数据登记、Job 快照与索引传播门禁。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import monotonic, sleep
from typing import cast

import httpx

from rag_app.adapters.stores import MigrationRunner, SqliteConnectionFactory
from rag_app.core.models import Chunk, DocumentIR, Job, vector_point_id
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.document_metadata import (
    DOCUMENT_METADATA_REVISION,
    WanshitongDocumentMetadata,
    WanshitongDocumentMetadataStore,
)
from rag_app.wanshitong.upload_validation import DOCX_MEDIA_TYPE
from tests.adapters.parsers.docx.fixtures import build_package
from tests.wanshitong.support import PublicHarness

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "migrations" / "universal_rag"


def _docx(text: str) -> bytes:
    return build_package(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>")


def _upload(
    harness: PublicHarness,
    *,
    content: bytes,
    metadata: dict[str, object],
    idempotency_key: str,
    document_id: str | None = None,
) -> httpx.Response:
    suffix = (
        "/documents"
        if document_id is None
        else f"/documents/{document_id}/versions"
    )
    return harness.client.post(
        ADMIN_BASE_PATH + suffix,
        params={"metadata": json.dumps(metadata, ensure_ascii=False)},
        headers={
            **harness.product.write_headers,
            "Content-Type": DOCX_MEDIA_TYPE,
            "Idempotency-Key": idempotency_key,
        },
        content=content,
    )


def _wait_for_job(harness: PublicHarness, job_id: str) -> Job:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        job = harness.product.runtime.sdk.get_job(job_id)
        if job.state.value not in {"queued", "running"}:
            return job
        sleep(0.01)
    raise AssertionError(f"Job 未在期限内结束：{job_id}")


def _migration_subset(target: Path, maximum: int) -> Path:
    target.mkdir()
    for source in sorted(_MIGRATIONS.glob("*.sql")):
        if int(source.name[:4]) <= maximum:
            shutil.copy2(source, target / source.name)
    return target


def _assert_registration_and_job_snapshot(
    harness: PublicHarness,
    *,
    job: Job,
    document_id: str,
    source_path: str,
) -> WanshitongDocumentMetadata:
    reopened = WanshitongDocumentMetadataStore(
        SqliteConnectionFactory(
            harness.product.runtime.connections.database_path
        )
    )
    stored = reopened.get(document_id)
    assert stored is not None
    assert stored.department_name == "科研管理"
    assert stored.category_path == ("项目管理", "复盘")
    assert stored.document_title == "项目&版本复盘"
    assert stored.source_relative_path == source_path
    assert stored.topic_keys == ("policy", "review")
    assert stored.visibility_scope == "all_internal"
    assert stored.allowed_roles == ()
    assert stored.allowed_groups == ()
    assert stored.metadata_revision == DOCUMENT_METADATA_REVISION
    assert (
        reopened.get_version_snapshot(cast(str, job.document_version_id))
        == stored
    )
    queued = harness.product.runtime.p09.store.ingestion_request(job.job_id)
    queued_document = next(
        item.document
        for item in queued.documents
        if item.document.document_id == document_id
    )
    assert dict(queued_document.metadata) == dict(stored.index_metadata())
    return stored


def _assert_ir_chunk_vector_and_admin_metadata(
    harness: PublicHarness,
    *,
    stored: WanshitongDocumentMetadata,
    revision_id: str,
    source_path: str,
) -> None:
    with harness.product.runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT document_ir_json FROM revision_documents "
            "WHERE revision_id=? AND document_id=?",
            (revision_id, stored.document_id),
        ).fetchone()
        chunk_rows = connection.execute(
            "SELECT chunk_id, chunk_json FROM chunks "
            "WHERE revision_id=? AND document_id=? ORDER BY chunk_id",
            (revision_id, stored.document_id),
        ).fetchall()
    assert row is not None
    document_ir = DocumentIR.model_validate_json(str(row["document_ir_json"]))
    expected_metadata = dict(stored.index_metadata())
    ir_metadata = dict(document_ir.metadata)
    assert {
        key: ir_metadata[key] for key in expected_metadata
    } == expected_metadata
    assert chunk_rows
    chunks = tuple(
        Chunk.model_validate_json(str(row["chunk_json"])) for row in chunk_rows
    )
    assert all(
        {key: dict(chunk.metadata)[key] for key in expected_metadata}
        == expected_metadata
        for chunk in chunks
    )
    assert all(
        "科研管理" not in chunk.embedding_text
        and source_path not in chunk.embedding_text
        for chunk in chunks
    )
    persistence = harness.product.runtime.retrieval_runtime.persistence
    vector_spec = persistence.control.revision_vector_spec(revision_id)
    points = persistence.components.vector_store.fetch_points(
        vector_spec,
        tuple(vector_point_id(revision_id, chunk.chunk_id) for chunk in chunks),
    )
    assert len(points) == len(chunks)
    assert all(
        point.payload.department_key == stored.department_key
        for point in points
    )
    assert all(
        point.payload.category_path == stored.category_path for point in points
    )
    assert all(
        point.payload.visibility_scope == "all_internal" for point in points
    )
    assert all(
        point.payload.source_relative_path == source_path for point in points
    )
    detail = harness.client.get(
        ADMIN_BASE_PATH + f"/documents/{stored.document_id}"
    )
    assert detail.status_code == 200
    assert detail.json()["department_name"] == "科研管理"
    assert detail.json()["source_relative_path"] == source_path


def test_metadata_persists_in_document_version_job_ir_chunk_and_vector(
    public_harness: PublicHarness,
) -> None:
    source_path = "01 科管/02 科研项目管理/版本&amp;复盘.docx"
    response = _upload(
        public_harness,
        content=_docx("WB06METADATA 唯一合成检索内容。"),
        metadata={
            "source_relative_path": source_path,
            "department_name": "科研管理",
            "category_path": ["项目管理", "复盘"],
            "document_title": "项目&版本复盘",
            "topic_keys": ["policy", "review"],
        },
        idempotency_key="metadata-pipeline",
    )

    assert response.status_code == 202
    receipt = response.json()
    job = _wait_for_job(public_harness, receipt["job"]["job_id"])
    assert job.state.value == "succeeded"
    document_id = str(receipt["document"]["document_id"])
    document = public_harness.product.runtime.sdk.get_document(
        job.project_id, job.knowledge_base_id, document_id
    )
    revision_id = cast(str, document.active_index_revision_id)
    stored = _assert_registration_and_job_snapshot(
        public_harness,
        job=job,
        document_id=document_id,
        source_path=source_path,
    )
    _assert_ir_chunk_vector_and_admin_metadata(
        public_harness,
        stored=stored,
        revision_id=revision_id,
        source_path=source_path,
    )


def test_new_version_inherits_corrections_and_freezes_its_own_snapshot(
    public_harness: PublicHarness,
) -> None:
    source_path = "04 开发中心/自动分类/流程.docx"
    created = _upload(
        public_harness,
        content=_docx("版本一合成内容。"),
        metadata={
            "source_relative_path": source_path,
            "department_name": "管理员修正部门",
            "category_path": ["管理员修正分类"],
            "document_title": "管理员修正标题",
            "topic_keys": ["process"],
        },
        idempotency_key="metadata-version-one",
    )
    assert created.status_code == 202
    created_payload = created.json()
    first_job = _wait_for_job(public_harness, created_payload["job"]["job_id"])
    assert first_job.state.value == "succeeded"
    document_id = str(created_payload["document"]["document_id"])

    second = public_harness.client.post(
        ADMIN_BASE_PATH + f"/documents/{document_id}/versions",
        headers={
            **public_harness.product.write_headers,
            "Content-Type": DOCX_MEDIA_TYPE,
            "Idempotency-Key": "metadata-version-two",
        },
        content=_docx("版本二合成内容。"),
    )
    assert second.status_code == 202
    second_job = _wait_for_job(public_harness, second.json()["job"]["job_id"])
    assert second_job.state.value == "succeeded"

    store = cast(
        WanshitongDocumentMetadataStore,
        public_harness.app.state.wanshitong_document_metadata,
    )
    current = store.get(document_id)
    first_snapshot = store.get_version_snapshot(
        cast(str, first_job.document_version_id)
    )
    second_snapshot = store.get_version_snapshot(
        cast(str, second_job.document_version_id)
    )
    assert current is not None
    assert first_snapshot is not None
    assert second_snapshot is not None
    assert current.department_name == "管理员修正部门"
    assert current.category_path == ("管理员修正分类",)
    assert current.document_title == "管理员修正标题"
    assert current.topic_keys == ("process",)
    assert first_snapshot == second_snapshot == current


def test_deleted_document_metadata_cannot_leak_to_another_document(
    public_harness: PublicHarness,
) -> None:
    source_path = "03 综合/02 供应链管理/共享路径.docx"
    first = _upload(
        public_harness,
        content=_docx("第一份逻辑文档。"),
        metadata={
            "source_relative_path": source_path,
            "department_name": "第一部门",
            "topic_keys": ["first"],
        },
        idempotency_key="metadata-isolation-first",
    )
    assert first.status_code == 202
    first_id = str(first.json()["document"]["document_id"])
    assert (
        _wait_for_job(public_harness, first.json()["job"]["job_id"]).state.value
        == "succeeded"
    )
    deleted = public_harness.client.delete(
        ADMIN_BASE_PATH + f"/documents/{first_id}",
        headers=public_harness.product.write_headers,
    )
    assert deleted.status_code == 204

    second = _upload(
        public_harness,
        content=_docx("第二份逻辑文档。"),
        metadata={
            "source_relative_path": source_path,
            "department_name": "第二部门",
            "topic_keys": ["second"],
        },
        idempotency_key="metadata-isolation-second",
    )
    assert second.status_code == 202
    second_id = str(second.json()["document"]["document_id"])
    assert first_id != second_id

    store = cast(
        WanshitongDocumentMetadataStore,
        public_harness.app.state.wanshitong_document_metadata,
    )
    first_metadata = store.get(first_id)
    second_metadata = store.get(second_id)
    assert first_metadata is not None
    assert second_metadata is not None
    assert first_metadata.department_name == "第一部门"
    assert second_metadata.department_name == "第二部门"


def test_wb05_metadata_rows_upgrade_forward_without_reclassification(
    tmp_path: Path,
) -> None:
    connections = SqliteConnectionFactory(
        tmp_path / "wb05.sqlite3", journal_mode="DELETE"
    )
    MigrationRunner(
        connections, _migration_subset(tmp_path / "wb05-migrations", 31)
    ).migrate()
    timestamp = "2026-09-15T00:00:00+00:00"
    document_id = "doc_" + "1" * 32
    version_id = "dver_" + "4" * 32
    job_id = "job_" + "5" * 32
    with connections.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO projects(project_id, name, created_at, updated_at) "
            "VALUES (?, '合成项目', ?, ?)",
            ("prj_" + "2" * 32, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO knowledge_bases(knowledge_base_id, project_id, "
            "name, normalized_name, description, profile_id, created_at, "
            "updated_at) VALUES (?, ?, '合成库', '合成库', '', "
            "'legacy-profile', ?, ?)",
            (
                "kb_" + "3" * 32,
                "prj_" + "2" * 32,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO documents(document_id, project_id, "
            "knowledge_base_id, display_name, status, created_at, updated_at) "
            "VALUES (?, ?, ?, '历史制度.docx', 'active', ?, ?)",
            (
                document_id,
                "prj_" + "2" * 32,
                "kb_" + "3" * 32,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO wanshitong_document_metadata(document_id, "
            "project_id, knowledge_base_id, relative_path, department, "
            "category_path_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                "prj_" + "2" * 32,
                "kb_" + "3" * 32,
                "01 科管/02 科研项目管理/历史制度.docx",
                "01 科管",
                '["02 科研项目管理"]',
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO document_versions(document_version_id, document_id, "
            "content_sha256, source_artifact_id, size_bytes, media_type, "
            "created_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (
                version_id,
                document_id,
                "a" * 64,
                "sha256:" + "a" * 64,
                DOCX_MEDIA_TYPE,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE documents SET current_version_id=? WHERE document_id=?",
            (version_id, document_id),
        )
        connection.execute(
            "INSERT INTO ingestion_jobs(job_id, project_id, "
            "knowledge_base_id, document_id, document_version_id, "
            "idempotency_key, state, stage, attempt, retryable, created_at, "
            "updated_at, finished_at) VALUES (?, ?, ?, ?, ?, "
            "'wb05-upload', 'completed', 'completed', 0, 0, ?, ?, ?)",
            (
                job_id,
                "prj_" + "2" * 32,
                "kb_" + "3" * 32,
                document_id,
                version_id,
                timestamp,
                timestamp,
                timestamp,
            ),
        )

    applied = MigrationRunner(connections, _MIGRATIONS).migrate()
    store = WanshitongDocumentMetadataStore(connections)
    assert store.synchronize_legacy_rows() == 1
    stored = store.get(document_id)
    snapshot = store.get_version_snapshot(version_id)
    with connections.transaction() as connection:
        document_row = connection.execute(
            "SELECT metadata_json FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()

    assert any(item.version == 32 for item in applied)
    assert document_row is not None
    assert stored is not None
    assert json.loads(str(document_row["metadata_json"])) == dict(
        stored.index_metadata()
    )
    assert snapshot == stored
    assert stored.department_name == "01 科管"
    assert stored.category_path == ("02 科研项目管理",)
    assert stored.document_title == "历史制度"
    assert (
        stored.source_relative_path == "01 科管/02 科研项目管理/历史制度.docx"
    )
    assert stored.visibility_scope == "all_internal"
