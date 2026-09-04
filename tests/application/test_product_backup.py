"""Product 统一备份的离线结构、安全和恢复回归。"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

import pytest

from rag_app.product.backup import create_backup, restore_backup, verify_backup
from tests.product_support import build_product_harness

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


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
        harness.runtime.sdk.create_project("备份项目")
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
    assert (
        tmp_path / "restored" / "blobs" / "sha256" / digest[:2] / digest
    ).read_bytes() == content


def test_verify_rejects_archive_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "payload"
    source.write_text("unsafe", encoding="utf-8")
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname="../outside")

    with pytest.raises(ValueError, match="不安全相对路径"):
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
