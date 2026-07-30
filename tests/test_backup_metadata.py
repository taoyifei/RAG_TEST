from __future__ import annotations

import json
from pathlib import Path

from tests.test_backup_script import _prepare_sandbox, _run_backup


def test_backup_metadata_records_safe_active_identity(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_backup(sandbox)

    assert completed.returncode == 0, completed.stderr
    metadata = json.loads(
        (
            sandbox.backups / "backup-a/BACKUP_METADATA.json"
        ).read_text(encoding="utf-8")
    )
    assert metadata["schema_version"] == "1"
    assert metadata["release_id"] == "release-a"
    assert metadata["source_revision"] == "1" * 40
    assert set(metadata["images"]) == {"app", "ocr", "qdrant"}
    assert len(metadata["active_env_sha256"]) == 64
    assert metadata["active_manifest"] == {
        "collection_name": "rag-docx-active-release-a",
        "manifest_sha256": "a" * 64,
    }
    assert set(metadata["archives"]) == {"state", "qdrant"}
    assert "TOKEN=preserve" not in json.dumps(metadata)


def test_unreadable_active_manifest_is_explicit_null(
    tmp_path: Path,
) -> None:
    sandbox = _prepare_sandbox(tmp_path)
    (sandbox.root / "data/state/manifest.sqlite3").write_bytes(
        b"not-sqlite"
    )

    completed = _run_backup(sandbox)

    assert completed.returncode == 0, completed.stderr
    metadata = json.loads(
        (
            sandbox.backups / "backup-a/BACKUP_METADATA.json"
        ).read_text(encoding="utf-8")
    )
    assert metadata["active_manifest"] is None
