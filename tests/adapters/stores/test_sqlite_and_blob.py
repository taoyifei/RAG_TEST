from __future__ import annotations

import hashlib
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rag_app.adapters.stores import (
    FilesystemBlobStore,
    MigrationRunner,
    SqliteConnectionFactory,
)
from rag_app.core.errors import Conflict, ValidationFailed
from rag_app.core.ports import BlobPutResult, BlobWriteRequest


def _request(content: bytes = b"shared-content") -> BlobWriteRequest:
    digest = hashlib.sha256(content).hexdigest()
    return BlobWriteRequest(
        blob_id=f"sha256:{digest}",
        content_sha256=digest,
        media_type="application/octet-stream",
        content=content,
    )


def test_migrations_are_repeatable_and_detect_checksum_drift(
    tmp_path: Path,
) -> None:
    source = (
        Path(__file__).resolve().parents[3] / "migrations" / "universal_rag"
    )
    migrations = tmp_path / "migrations"
    shutil.copytree(source, migrations)
    connections = SqliteConnectionFactory(
        tmp_path / "control.sqlite3", journal_mode="DELETE"
    )
    runner = MigrationRunner(connections, migrations)

    first = runner.migrate()
    second = runner.migrate()

    assert [item.version for item in first] == list(range(1, 15))
    assert second == first
    path = migrations / "0005_embedding_cache_gc.sql"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8"
    )
    with pytest.raises(ValidationFailed, match="checksum"):
        runner.migrate()


def test_sqlite_transactions_enforce_fk_and_rollback(tmp_path: Path) -> None:
    connections = SqliteConnectionFactory(
        tmp_path / "control.sqlite3", journal_mode="WAL"
    )
    migrations = (
        Path(__file__).resolve().parents[3] / "migrations" / "universal_rag"
    )
    MigrationRunner(connections, migrations).migrate()
    with (
        pytest.raises(sqlite3.IntegrityError),
        connections.transaction(write=True) as connection,
    ):
        connection.execute(
            "INSERT INTO knowledge_bases("
            "knowledge_base_id, project_id, name, normalized_name, "
            "profile_id, created_at, updated_at) "
            "VALUES ('kb_bad', 'prj_missing', 'x', 'x', 'p', 't', 't')"
        )
    with connections.transaction() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM knowledge_bases"
            ).fetchone()[0]
            == 0
        )


def test_filesystem_blob_is_content_addressed_and_concurrent(
    tmp_path: Path,
) -> None:
    store = FilesystemBlobStore(tmp_path)
    request = _request()
    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = tuple(
            executor.map(lambda _: store.put_if_absent(request), range(8))
        )
    assert outcomes.count(BlobPutResult.CREATED) == 1
    assert outcomes.count(BlobPutResult.EXISTING) == 7
    assert store.read(request.blob_id).content == request.content  # type: ignore[union-attr]
    assert store.locator(request.blob_id).startswith("blobs/sha256/")


def test_filesystem_blob_rejects_identity_and_existing_content_mismatch(
    tmp_path: Path,
) -> None:
    store = FilesystemBlobStore(tmp_path)
    request = _request()
    store.put_if_absent(request)
    with pytest.raises(ValidationFailed):
        store.put_if_absent(
            request.model_copy(update={"content_sha256": "0" * 64})
        )
    physical = tmp_path / store.locator(request.blob_id)
    physical.write_bytes(b"corrupt")
    with pytest.raises(Conflict):
        store.put_if_absent(request)
