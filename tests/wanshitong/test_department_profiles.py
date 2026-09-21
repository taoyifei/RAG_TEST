"""阶段 04 部门 Profile 构建、版本绑定与原子持久化门禁。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.identifiers import canonical_sha256
from rag_app.wanshitong.department_profiles import (
    UNKNOWN_DEPARTMENT_KEY,
    DepartmentEmbeddingIdentity,
    DepartmentProfileLoadError,
    DepartmentProfileSourceDocument,
    DepartmentProfileSourceReader,
    DepartmentProfileSourceSnapshot,
    DepartmentProfileStore,
    build_department_profiles,
)

_PROJECT_ID = "prj_" + "1" * 32
_KNOWLEDGE_BASE_ID = "kb_" + "2" * 32
_INDEX_REVISION_ID = "irev_" + "3" * 32
_BUILT_AT = datetime(2026, 9, 21, tzinfo=UTC)


def _document(
    suffix: str,
    *,
    department_key: str | None,
    department_name: str | None,
    title: str,
) -> DepartmentProfileSourceDocument:
    return DepartmentProfileSourceDocument(
        document_id="doc_" + suffix * 32,
        document_version_id="dver_" + suffix * 32,
        department_key=department_key,
        department_name=department_name,
        category_path=("制度",),
        document_title=title,
        topic_keys=("流程",),
        metadata_revision="wanshitong-document-metadata-v1",
    )


def _snapshot(
    documents: tuple[DepartmentProfileSourceDocument, ...],
) -> DepartmentProfileSourceSnapshot:
    return DepartmentProfileSourceSnapshot(
        project_id=_PROJECT_ID,
        knowledge_base_id=_KNOWLEDGE_BASE_ID,
        scope_id=canonical_sha256(
            {
                "project_id": _PROJECT_ID,
                "knowledge_base_id": _KNOWLEDGE_BASE_ID,
            }
        ),
        index_revision_id=_INDEX_REVISION_ID,
        metadata_revision=canonical_sha256(
            [document.model_dump(mode="json") for document in documents]
        ),
        documents=documents,
    )


def test_build_keeps_duplicate_names_separate_and_uses_unknown() -> None:
    snapshot = _snapshot(
        (
            _document(
                "4",
                department_key="department-a",
                department_name="联合部门",
                title="文件甲",
            ),
            _document(
                "5",
                department_key="department-b",
                department_name="联合部门",
                title="文件乙",
            ),
            _document(
                "6",
                department_key=None,
                department_name=None,
                title="无部门文件",
            ),
        )
    )

    profiles = build_department_profiles(snapshot, built_at=_BUILT_AT)

    assert tuple(item.department_key for item in profiles.profiles) == (
        UNKNOWN_DEPARTMENT_KEY,
        "department-a",
        "department-b",
    )
    same_name = tuple(
        item for item in profiles.profiles if item.department_name == "联合部门"
    )
    assert len(same_name) == 2
    assert profiles.embedding_identity is None
    assert all(item.vector is None for item in profiles.profiles)


def test_profile_store_uses_exact_version_and_rejects_corruption(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        (
            _document(
                "4",
                department_key="research",
                department_name="科研部",
                title="项目办法",
            ),
        )
    )
    profiles = build_department_profiles(snapshot, built_at=_BUILT_AT)
    store = DepartmentProfileStore(tmp_path)

    target = store.save(profiles)

    assert target.stat().st_mode & 0o777 == 0o600
    assert (
        store.load(
            profiles.scope_id,
            profiles.index_revision_id,
            profiles.metadata_revision,
        )
        == profiles
    )
    assert (
        store.load(
            profiles.scope_id,
            "irev_" + "9" * 32,
            profiles.metadata_revision,
        )
        is None
    )
    target.write_text("{broken", encoding="utf-8")
    with pytest.raises(DepartmentProfileLoadError):
        store.load(
            profiles.scope_id,
            profiles.index_revision_id,
            profiles.metadata_revision,
        )


def test_invalid_vector_build_does_not_replace_valid_profile(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        (
            _document(
                "4",
                department_key="research",
                department_name="科研部",
                title="项目办法",
            ),
        )
    )
    store = DepartmentProfileStore(tmp_path)
    original = build_department_profiles(snapshot, built_at=_BUILT_AT)
    store.save(original)
    identity = DepartmentEmbeddingIdentity(
        slot_id="primary",
        provider_id="embedding",
        model="model",
        vector_name="dense_primary",
        dimension=2,
        normalization="l2",
        adapter_revision="1",
    )

    with pytest.raises(ValueError):
        build_department_profiles(
            snapshot,
            embedding_identity=identity,
            vectors={"research": (1.0,)},
            built_at=_BUILT_AT,
        )

    assert (
        store.load(
            original.scope_id,
            original.index_revision_id,
            original.metadata_revision,
        )
        == original
    )


def test_source_reader_binds_version_metadata_to_active_revision(
    tmp_path: Path,
) -> None:
    connections = SqliteConnectionFactory(tmp_path / "universal-rag.sqlite3")
    with connections.transaction(write=True) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_bases (
              knowledge_base_id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              active_revision_id TEXT,
              deleted_at TEXT
            );
            CREATE TABLE index_revisions (
              index_revision_id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              knowledge_base_id TEXT NOT NULL,
              state TEXT NOT NULL
            );
            CREATE TABLE documents (
              document_id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              knowledge_base_id TEXT NOT NULL,
              status TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              deleted_at TEXT
            );
            CREATE TABLE revision_documents (
              revision_id TEXT NOT NULL,
              document_id TEXT NOT NULL,
              document_version_id TEXT NOT NULL
            );
            CREATE TABLE wanshitong_document_version_metadata (
              document_version_id TEXT PRIMARY KEY,
              metadata_json TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO knowledge_bases VALUES (?, ?, ?, NULL)",
            (_KNOWLEDGE_BASE_ID, _PROJECT_ID, _INDEX_REVISION_ID),
        )
        connection.execute(
            "INSERT INTO index_revisions VALUES (?, ?, ?, 'active')",
            (_INDEX_REVISION_ID, _PROJECT_ID, _KNOWLEDGE_BASE_ID),
        )
        document_id = "doc_" + "4" * 32
        document_version_id = "dver_" + "5" * 32
        connection.execute(
            "INSERT INTO documents VALUES (?, ?, ?, 'active', '{}', NULL)",
            (document_id, _PROJECT_ID, _KNOWLEDGE_BASE_ID),
        )
        connection.execute(
            "INSERT INTO revision_documents VALUES (?, ?, ?)",
            (_INDEX_REVISION_ID, document_id, document_version_id),
        )
        metadata = {
            "department_key": "research",
            "department_name": "科研部",
            "category_path": ["项目管理"],
            "document_title": "科研项目管理办法",
            "topic_keys": ["申报"],
            "metadata_revision": "wanshitong-document-metadata-v1",
        }
        connection.execute(
            "INSERT INTO wanshitong_document_version_metadata VALUES (?, ?)",
            (document_version_id, json.dumps(metadata, ensure_ascii=False)),
        )

    snapshot = DepartmentProfileSourceReader(connections).snapshot(
        _PROJECT_ID, _KNOWLEDGE_BASE_ID
    )

    assert snapshot.index_revision_id == _INDEX_REVISION_ID
    assert len(snapshot.documents) == 1
    assert snapshot.documents[0].document_version_id == document_version_id
    assert snapshot.documents[0].department_key == "research"
