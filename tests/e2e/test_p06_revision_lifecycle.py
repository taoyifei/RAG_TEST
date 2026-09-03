from __future__ import annotations

import hashlib
from pathlib import Path

from rag_app.adapters.stores import SqliteConnectionFactory, SqliteFtsStore
from rag_app.adapters.stores.sqlite_fts5 import normalize_identifier
from rag_app.application.revision_builder import IngestionDocument
from rag_app.core.identifiers import deterministic_id, document_version_id
from rag_app.core.models import (
    DocumentRef,
    LexicalSearchRequest,
    vector_point_id,
)
from tests.adapters.parsers.docx_fixtures import (
    TABLE,
    build_docx,
)
from tests.persistence.helpers import runtime_with_kb

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _document(
    project_id: str, knowledge_base_id: str, suffix: str, content: bytes
) -> IngestionDocument:
    return IngestionDocument(
        document=DocumentRef(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            document_id=deterministic_id(
                "doc", project_id, knowledge_base_id, suffix
            ),
            display_name=f"{suffix}.docx",
        ),
        content=content,
        media_type=_MEDIA_TYPE,
    )


def test_p06_revision_lifecycle_survives_reopen(tmp_path: Path) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(tmp_path)
    content = build_docx(TABLE)
    documents = (
        _document(project_id, knowledge_base_id, "alpha", content),
        _document(project_id, knowledge_base_id, "beta", content),
    )
    result = runtime.builder.build_and_activate(
        project_id=project_id,
        knowledge_base_id=knowledge_base_id,
        documents=documents,
        idempotency_key="same-bytes-two-documents",
        budgets=runtime.default_budgets(),
    )
    repeated = runtime.builder.build_and_activate(
        project_id=project_id,
        knowledge_base_id=knowledge_base_id,
        documents=documents,
        idempotency_key="same-bytes-two-documents",
        budgets=runtime.default_budgets(),
    )
    assert repeated == result
    digest = hashlib.sha256(content).hexdigest()
    assert (
        len(
            {
                document_version_id(item.document.document_id, digest)
                for item in documents
            }
        )
        == 2
    )
    assert runtime.control.reference_count(f"sha256:{digest}") == 2
    chunks = runtime.control.chunk_rows(result.revision_id)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    runtime.close()

    reopened, _, _ = runtime_with_kb(tmp_path)
    try:
        assert (
            reopened.control.active_revision_id(knowledge_base_id)
            == result.revision_id
        )
        assert (
            reopened.recovery.backfill(result.revision_id) == result.chunk_count
        )
        spec = reopened.control.revision_vector_spec(result.revision_id)
        evidence = reopened.validator.validate(
            spec,
            current_index_fingerprint=reopened.components.index_fingerprint,
        )
        assert evidence.chunk_count == result.chunk_count
        hits = reopened.components.lexical_store.search(
            LexicalSearchRequest(
                revision=spec.revision,
                query="A B",
                limit=10,
            )
        )
        assert {hit.chunk.version.document_id for hit in hits} == {
            item.document.document_id for item in documents
        }
        assert (
            reopened.components.lexical_store.search(
                LexicalSearchRequest(
                    revision=spec.revision,
                    query='" OR *',
                    limit=10,
                )
            )
            == ()
        )
        lexical = reopened.components.lexical_store
        assert isinstance(lexical, SqliteFtsStore)
        connections = SqliteConnectionFactory(
            tmp_path / "universal-rag.sqlite3"
        )
        with connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO exact_identifiers("
                "revision_id, chunk_id, identifier, normalized_identifier) "
                "VALUES (?, ?, ?, ?)",
                (
                    result.revision_id,
                    chunks[0].chunk_id,
                    "ABC_123",
                    normalize_identifier("ABC_123"),
                ),
            )
        assert lexical.search_exact(result.revision_id, "abc-123") == (
            chunks[0].chunk_id,
        )
        points = reopened.components.vector_store.fetch_points(
            spec,
            tuple(
                vector_point_id(result.revision_id, chunk.chunk_id)
                for chunk in chunks
            ),
        )
        assert len(points) == result.chunk_count
        query_vector = points[0].vector_map()["dense_primary"]
        dense_hits = reopened.components.vector_store.search_named(
            spec,
            slot_id="primary",
            vector_name="dense_primary",
            query_vector=query_vector,
            limit=1,
        )
        assert dense_hits[0].point_id == points[0].point_id
    finally:
        reopened.close()


def test_hot_standby_requires_both_named_vectors(tmp_path: Path) -> None:
    runtime, project_id, knowledge_base_id = runtime_with_kb(
        tmp_path,
        profile_name="dev-p06-dual-memory.json",
    )
    try:
        result = runtime.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                _document(
                    project_id,
                    knowledge_base_id,
                    "dual",
                    build_docx(TABLE),
                ),
            ),
            idempotency_key="dual",
            budgets=runtime.default_budgets(),
        )
        assert dict(result.evidence.vector_counts) == {
            "dense_primary": result.chunk_count,
            "dense_standby": result.chunk_count,
        }
    finally:
        runtime.close()
