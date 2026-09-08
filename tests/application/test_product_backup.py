"""Product 统一备份的离线结构、安全和恢复回归。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from rag_app.adapters.parsers.docx import DocxOoxmlV4Parser
from rag_app.cli import main
from rag_app.core.errors import InvalidDocument
from rag_app.core.models import ParseSource
from rag_app.product import backup
from rag_app.product.backup import create_backup, restore_backup, verify_backup
from tests.adapters.parsers.docx.fixtures import context, policy
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_trust_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path / "controlled-data"))


@pytest.mark.parametrize(
    "operation", ["verify", "restore", "cli_verify", "cli_restore"]
)
def test_unknown_archive_is_rejected_before_sqlite_or_target_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    database = payload / "database.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
    compatibility = payload / "compatibility-manifest.json"
    compatibility.write_text("{}", encoding="utf-8")
    manifest = {
        "format_version": 1,
        "secrets_included": False,
        "database": database.name,
        "compatibility_manifest": compatibility.name,
        "collections": {},
        "files": [
            {
                "path": file.name,
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "size": file.stat().st_size,
            }
            for file in (database, compatibility)
        ],
    }
    (payload / "backup-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    archive = tmp_path / "externally-forged.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for file in payload.iterdir():
            bundle.add(file, arcname=file.name)

    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("未知归档不应打开 SQLite。")

    monkeypatch.setattr(backup.sqlite3, "connect", forbidden_connect)
    with pytest.raises(ValueError, match="可信来源"):
        if operation == "verify":
            verify_backup(archive)
        elif operation == "restore":
            restore_backup(
                archive_path=archive,
                target_data_dir=tmp_path / "target",
                qdrant_url="http://127.0.0.1:6333",
                qdrant_api_key_file=tmp_path / "nonexistent-secret",
            )
        elif operation == "cli_verify":
            main(["backup", "verify", "--archive", str(archive)])
        else:
            main(
                [
                    "restore",
                    "--archive",
                    str(archive),
                    "--target-data-dir",
                    str(tmp_path / "target"),
                    "--qdrant-url",
                    "http://127.0.0.1:6333",
                    "--qdrant-api-key-file",
                    str(tmp_path / "nonexistent-secret"),
                ]
            )
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("extension", [".sqlite3", ".docx"])
def test_sqlite_document_or_disguised_docx_never_opens_sqlite(
    extension: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("文档解析不得打开 SQLite。")

    monkeypatch.setattr(backup.sqlite3, "connect", forbidden_connect)
    source = ParseSource(
        display_name=f"untrusted{extension}",
        extension=extension,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content=b"SQLite format 3\x00" + b"\x00" * 4096,
    )
    with pytest.raises(InvalidDocument):
        DocxOoxmlV4Parser().parse(source, policy(), context())


def _secret(path: Path) -> Path:
    path.write_text("synthetic-qdrant-key", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_backup_verifies_and_restores_without_secret_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_product_harness(tmp_path / "source")
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        failed = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer",
            json={"query": "备份前的公开 Trace 样例"},
            headers=harness.write_headers,
        )
        assert failed.status_code == 409
        trace_id = failed.json()["error"]["trace_id"]
        assert (
            harness.runtime.traces.detail(trace_id).trace.trace_id == trace_id
        )
        content = b"synthetic-public-blob"
        digest = hashlib.sha256(content).hexdigest()
        blob = harness.runtime.data_dir / "blobs" / "sha256" / digest[:2]
        blob.mkdir(parents=True, exist_ok=True)
        (blob / digest).write_bytes(content)
        monkeypatch.setattr(
            "rag_app.product.backup._snapshot_collections",
            lambda *_args, **_kwargs: ({}, "1.18.3"),
        )
        archive = tmp_path / "product-backup.tar.gz"
        key = _secret(tmp_path / "qdrant-api-key")
        report = create_backup(
            data_dir=harness.runtime.data_dir,
            output=archive,
            compatibility_manifest=(
                _REPOSITORY_ROOT / "compatibility-manifest.json"
            ),
            qdrant_url="http://127.0.0.1:6333",
            qdrant_api_key_file=key,
        )
    finally:
        harness.close()

    verified = verify_backup(archive)
    restored = restore_backup(
        archive_path=archive,
        target_data_dir=tmp_path / "restored",
        qdrant_url="http://127.0.0.1:6333",
        qdrant_api_key_file=key,
    )
    with tarfile.open(archive, "r:gz") as bundle:
        names = tuple(item.name for item in bundle.getmembers())

    assert report == verified == restored
    assert report.sqlite_integrity == "ok"
    assert not any(
        name.endswith(("master-key", "admin-bootstrap-token", "qdrant-api-key"))
        for name in names
    )
    assert (tmp_path / "restored" / "universal-rag.sqlite3").is_file()
    trace_database = tmp_path / "restored" / "product-traces.sqlite3"
    assert trace_database.is_file()
    with sqlite3.connect(trace_database) as connection:
        assert connection.execute(
            "SELECT status FROM traces WHERE trace_id=?", (trace_id,)
        ).fetchone() == ("FAILED",)
    assert (
        tmp_path / "restored" / "blobs" / "sha256" / digest[:2] / digest
    ).read_bytes() == content


def test_verify_rejects_archive_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "payload"
    source.write_text("unsafe", encoding="utf-8")
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname="../outside")

    with pytest.raises(ValueError, match="可信来源"):
        verify_backup(archive)
    with pytest.raises(ValueError, match="不安全相对路径"):
        backup._safe_extract(archive, tmp_path / "extracted")


@pytest.fixture
def trusted_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    harness = build_product_harness(tmp_path / "source")
    try:
        monkeypatch.setattr(
            backup,
            "_snapshot_collections",
            lambda *_args, **_kwargs: ({}, "test"),
        )
        archive = tmp_path / "trusted.tar.gz"
        key = _secret(tmp_path / "qdrant-api-key")
        create_backup(
            data_dir=harness.runtime.data_dir,
            output=archive,
            compatibility_manifest=_REPOSITORY_ROOT
            / "compatibility-manifest.json",
            qdrant_url="http://127.0.0.1:6333",
            qdrant_api_key_file=key,
        )
    finally:
        harness.close()
    return archive, key


def test_tampered_archive_rejected_before_sqlite(
    trusted_archive: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _ = trusted_archive
    with archive.open("ab") as stream:
        stream.write(b"tampered-even-if-tar-ignores-trailing-bytes")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("完整归档被篡改时不得打开 SQLite。")

    monkeypatch.setattr(backup.sqlite3, "connect", forbidden)
    with pytest.raises(ValueError, match="可信来源"):
        verify_backup(archive)


def test_restore_uses_verified_copy_when_original_is_replaced(
    trusted_archive: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, key = trusted_archive
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    original_require = backup._require_creation_receipt

    def replace_after_check(value: str) -> None:
        original_require(value)
        archive.write_bytes(b"replacement must never be opened as SQLite")

    monkeypatch.setattr(
        backup, "_require_creation_receipt", replace_after_check
    )
    report = restore_backup(
        archive_path=archive,
        target_data_dir=tmp_path / "restored",
        qdrant_url="http://127.0.0.1:6333",
        qdrant_api_key_file=key,
    )
    assert report.archive_sha256 == digest
    database = tmp_path / "restored" / "universal-rag.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == (
            "ok",
        )
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == (
            "wal",
        )
        connection.execute("CREATE VIRTUAL TABLE trial_fts USING fts5(content)")
        connection.execute("INSERT INTO trial_fts VALUES ('synthetic backup')")
        assert connection.execute(
            "SELECT content FROM trial_fts WHERE trial_fts MATCH 'backup'"
        ).fetchone() == ("synthetic backup",)


@pytest.mark.parametrize("component", ["archive", "parent", "receipt"])
def test_symlinks_cannot_supply_archive_or_trust(
    trusted_archive: tuple[Path, Path], tmp_path: Path, component: str
) -> None:
    archive, _ = trusted_archive
    if component == "receipt":
        receipt = next(backup._trust_directory().iterdir())
        moved = tmp_path / "external-receipt"
        receipt.rename(moved)
        receipt.symlink_to(moved)
        candidate = archive
    elif component == "parent":
        parent = tmp_path / "link-parent"
        parent.symlink_to(archive.parent, target_is_directory=True)
        candidate = parent / archive.name
    else:
        candidate = tmp_path / "link-archive"
        candidate.symlink_to(archive)
    with pytest.raises(ValueError, match=r"symlink|可信来源"):
        verify_backup(candidate)


@pytest.mark.parametrize("component", ["directory", "receipt"])
def test_trust_store_requires_private_permissions(
    trusted_archive: tuple[Path, Path], component: str
) -> None:
    archive, _ = trusted_archive
    directory = backup._trust_directory()
    target = (
        directory if component == "directory" else next(directory.iterdir())
    )
    target.chmod(0o755 if component == "directory" else 0o644)
    with pytest.raises(ValueError, match="可信来源"):
        verify_backup(archive)


def test_restore_rejects_nonempty_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_product_harness(tmp_path / "source")
    try:
        monkeypatch.setattr(
            "rag_app.product.backup._snapshot_collections",
            lambda *_args, **_kwargs: ({}, "1.18.3"),
        )
        archive = tmp_path / "product-backup.tar.gz"
        key = _secret(tmp_path / "qdrant-api-key")
        create_backup(
            data_dir=harness.runtime.data_dir,
            output=archive,
            compatibility_manifest=(
                _REPOSITORY_ROOT / "compatibility-manifest.json"
            ),
            qdrant_url="http://127.0.0.1:6333",
            qdrant_api_key_file=key,
        )
    finally:
        harness.close()
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="必须为空"):
        restore_backup(
            archive_path=archive,
            target_data_dir=target,
            qdrant_url="http://127.0.0.1:6333",
            qdrant_api_key_file=key,
        )
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
