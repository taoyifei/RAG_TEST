from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from rag_app.adapters.stores import SqliteConnectionFactory
from rag_app.application.revision_builder import IngestionDocument
from rag_app.core.identifiers import deterministic_id, document_version_id
from rag_app.core.models import DocumentRef
from tests.adapters.parsers.docx_fixtures import TABLE, build_docx
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _assert_integrity(
    connections: SqliteConnectionFactory,
    statement: str,
    values: Sequence[object],
) -> None:
    with (
        pytest.raises(sqlite3.IntegrityError),
        connections.transaction(write=True) as connection,
    ):
        connection.execute(statement, values)


def test_raw_sql_rejects_every_cross_scope_relationship(
    tmp_path: Path,
) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    content = build_docx(TABLE)
    document = IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id("doc", "scope-primary"),
            display_name="scope-primary.docx",
        ),
        content=content,
        media_type=_MEDIA_TYPE,
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(document,),
            idempotency_key="scope-primary",
            budgets=runtime.default_budgets(),
        )
        other_project = deterministic_id("prj", "scope-other")
        other_kb = deterministic_id("kb", other_project, "scope-other")
        runtime.control.put_project(other_project, "Other Project")
        runtime.control.put_knowledge_base(
            other_kb,
            other_project,
            "Other KB",
            profile_id=runtime.components.profile.profile_id,
        )
        other_document = DocumentRef(
            project_id=other_project,
            knowledge_base_id=other_kb,
            document_id=deterministic_id("doc", "scope-other"),
            display_name="scope-other.docx",
        )
        runtime.control.upsert_document(other_document)
        digest = hashlib.sha256(b"other-version").hexdigest()
        other_version = document_version_id(other_document.document_id, digest)
        runtime.control.put_document_version(
            other_document.document_id,
            other_version,
            digest,
            f"sha256:{digest}",
            len(b"other-version"),
            _MEDIA_TYPE,
        )
        connections = SqliteConnectionFactory(
            tmp_path / "universal-rag.sqlite3"
        )
        chunk = runtime.control.chunk_rows(result.revision_id)[0]

        _assert_integrity(
            connections,
            "INSERT INTO documents(document_id, project_id, "
            "knowledge_base_id, display_name, status, created_at, "
            "updated_at) VALUES (?, ?, ?, 'bad', 'active', ?, ?)",
            (
                deterministic_id("doc", "bad-scope"),
                project_id,
                other_kb,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        _assert_integrity(
            connections,
            "UPDATE index_revisions SET project_id=? "
            "WHERE index_revision_id=?",
            (other_project, result.revision_id),
        )
        _assert_integrity(
            connections,
            "UPDATE revision_documents SET document_version_id=? "
            "WHERE revision_id=? AND document_id=?",
            (
                other_version,
                result.revision_id,
                document.document.document_id,
            ),
        )
        _assert_integrity(
            connections,
            "UPDATE chunks SET document_version_id=? WHERE row_id=?",
            (other_version, _chunk_row_id(connections, chunk.chunk_id)),
        )
        _assert_integrity(
            connections,
            "INSERT INTO ingestion_jobs(job_id, project_id, "
            "knowledge_base_id, idempotency_key, state, stage, attempt, "
            "retryable, created_at, updated_at) VALUES (?, ?, ?, ?, "
            "'pending', 'created', 0, 0, ?, ?)",
            (
                deterministic_id("job", "bad-scope"),
                project_id,
                other_kb,
                "bad-scope",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        _assert_integrity(
            connections,
            "UPDATE knowledge_bases SET active_revision_id=? "
            "WHERE knowledge_base_id=?",
            (result.revision_id, other_kb),
        )
        _assert_integrity(
            connections,
            "UPDATE active_revision_history SET knowledge_base_id=? "
            "WHERE new_revision_id=?",
            (other_kb, result.revision_id),
        )
    finally:
        runtime.close()


def _chunk_row_id(
    connections: SqliteConnectionFactory,
    chunk_id: str,
) -> int:
    with connections.transaction() as connection:
        row = connection.execute(
            "SELECT row_id FROM chunks WHERE chunk_id=?", (chunk_id,)
        ).fetchone()
    assert row is not None
    return int(row["row_id"])
