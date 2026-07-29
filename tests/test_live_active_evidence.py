from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from evaluation import active_state, metrics
from rag_app import active_evidence, manifest


def _arguments(database_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        qdrant_url="http://127.0.0.1:1",
        qdrant_alias="rag-active",
        manifest_database=database_path,
        qdrant_api_key_env="RAG_QDRANT_API_KEY",
        active_evidence_output=None,
    )


def test_public_trust_minting_symbols_are_removed() -> None:
    """拒绝用公开 Python 名称把传输清单铸造成可信对象。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    assert not hasattr(active_evidence, "TrustedActiveEvidence")
    assert not hasattr(active_evidence, "_TRUST_MARKER")
    assert not hasattr(
        active_evidence,
        "verify_exported_active_evidence",
    )


def test_missing_readonly_database_is_not_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产现场读取在数据库缺失时不得创建文件或目录。

    Args:
        tmp_path: pytest 提供的临时根目录。
        monkeypatch: pytest 提供的环境变量隔离器。

    Returns:
        无返回值。

    """
    database_path = tmp_path / "missing" / "state.sqlite3"
    monkeypatch.setenv("RAG_QDRANT_API_KEY", "test-only")
    with pytest.raises((ValueError, LookupError, sqlite3.Error)):
        active_state.load_live_active_evidence(
            _arguments(database_path)
        )

    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_readonly_manifest_connection_enables_query_only(
    tmp_path: Path,
) -> None:
    """只读 manifest 入口必须使用 SQLite query_only。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        无返回值。

    """
    database_path = tmp_path / "state.sqlite3"
    database_path.touch()
    repository_type = getattr(
        manifest,
        "ReadOnlyManifestRepository",
        None,
    )

    assert repository_type is not None
    repository = repository_type(database_path)
    with repository._connect() as connection:
        query_only = connection.execute("PRAGMA query_only").fetchone()
    assert query_only is not None
    assert int(query_only[0]) == 1


def test_readonly_manifest_rejects_incomplete_schema_without_sidecars(
    tmp_path: Path,
) -> None:
    """只读 manifest 入口拒绝不完整 schema 且不创建 WAL 边车。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        无返回值。

    """
    database_path = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    repository = manifest.ReadOnlyManifestRepository(database_path)
    with pytest.raises(ValueError, match="schema"):
        repository.get_active()

    assert not database_path.with_name(
        f"{database_path.name}-wal"
    ).exists()
    assert not database_path.with_name(
        f"{database_path.name}-shm"
    ).exists()


def test_readonly_manifest_query_does_not_create_wal(
    tmp_path: Path,
) -> None:
    """只读查询现有完整 schema 时不得创建 WAL。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        无返回值。

    """
    database_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE index_manifests (
                collection_name TEXT PRIMARY KEY,
                pipeline_fingerprint TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                snapshot_name TEXT NOT NULL,
                snapshot_checksum TEXT NOT NULL,
                created_at TEXT NOT NULL,
                activated_at TEXT
            )
            """
        )
    wal_path = database_path.with_name(f"{database_path.name}-wal")
    shm_path = database_path.with_name(f"{database_path.name}-shm")
    assert not wal_path.exists()
    assert not shm_path.exists()

    readonly_repository = manifest.ReadOnlyManifestRepository(database_path)
    assert readonly_repository.get_active() is None

    assert not wal_path.exists()
    assert not shm_path.exists()


def test_metrics_do_not_export_audit_manifest_loader() -> None:
    """生产评测模块不得暴露 audit JSON 回灌入口。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    assert "load_active_evidence_manifest" not in metrics.__all__
    assert not hasattr(metrics, "TrustedActiveEvidence")
