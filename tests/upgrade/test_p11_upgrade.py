"""P08.5—P10.5 数据升级到 P11 Schema 的回归。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rag_app.adapters.stores import MigrationRunner, SqliteConnectionFactory
from rag_app.core.errors import ValidationFailed
from rag_app.product.credential_store import CredentialStore
from rag_app.product.crypto import SecretCipher, initialize_master_key

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = _ROOT / "migrations" / "universal_rag"


def _migration_subset(target: Path, maximum: int) -> Path:
    target.mkdir()
    for source in sorted(_MIGRATIONS.glob("*.sql")):
        if int(source.name[:4]) <= maximum:
            shutil.copy2(source, target / source.name)
    return target


def _seed_control_rows(connections: SqliteConnectionFactory) -> None:
    now = "2026-09-04T00:00:00+00:00"
    with connections.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO projects(project_id, name, created_at, updated_at) "
            "VALUES ('prj_upgrade', '升级保留项目', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_bases("
            "knowledge_base_id, project_id, name, normalized_name, "
            "description, profile_id, created_at, updated_at) "
            "VALUES ('kb_upgrade', 'prj_upgrade', '升级知识库', "
            "'升级知识库', '公开合成数据', 'legacy-profile', ?, ?)",
            (now, now),
        )


@pytest.mark.parametrize("starting_version", [9, 10, 13, 14])
def test_supported_phase_data_upgrades_monotonically(
    tmp_path: Path,
    starting_version: int,
) -> None:
    """P08.5、P09、P10、P10.5 数据均原位升级且不丢控制面行。"""
    database = tmp_path / f"schema-{starting_version}.sqlite3"
    connections = SqliteConnectionFactory(database, journal_mode="DELETE")
    MigrationRunner(
        connections,
        _migration_subset(tmp_path / "old", starting_version),
    ).migrate()
    _seed_control_rows(connections)

    applied = MigrationRunner(connections, _MIGRATIONS).migrate()

    with connections.transaction() as connection:
        project = connection.execute(
            "SELECT name FROM projects WHERE project_id='prj_upgrade'"
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert [item.version for item in applied] == list(range(1, 17))
    assert project is not None and project[0] == "升级保留项目"
    assert "provider_operation_events" in tables
    assert "provider_daily_budgets" in tables


def test_legacy_fts_v1_is_preserved_and_requires_explicit_reindex(
    tmp_path: Path,
) -> None:
    """旧 FTS V1 行不冒充 V2，升级只保留并等待显式重建。"""
    connections = SqliteConnectionFactory(
        tmp_path / "legacy-fts.sqlite3", journal_mode="DELETE"
    )
    MigrationRunner(
        connections,
        _migration_subset(tmp_path / "old", 9),
    ).migrate()
    with connections.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO chunks_fts("
            "chunk_id, revision_id, knowledge_base_id, document_id, title, "
            "heading, identifiers, lexical_text) "
            "VALUES ('chunk_legacy', 'irev_legacy', 'kb_legacy', "
            "'doc_legacy', '旧标题', '', '', '旧索引文本')"
        )

    MigrationRunner(connections, _MIGRATIONS).migrate()

    with connections.transaction() as connection:
        legacy_count = int(
            connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        )
        v2_count = int(
            connection.execute(
                "SELECT count(*) FROM chunks_fts_v2"
            ).fetchone()[0]
        )
    assert legacy_count == 1
    assert v2_count == 0


def test_p10_5_encrypted_provider_secret_survives_upgrade(
    tmp_path: Path,
) -> None:
    """0014 的 AES-GCM Credential 可由 0015 后相同主密钥解密。"""
    connections = SqliteConnectionFactory(
        tmp_path / "secret.sqlite3", journal_mode="DELETE"
    )
    MigrationRunner(
        connections,
        _migration_subset(tmp_path / "old", 14),
    ).migrate()
    master_key = initialize_master_key(tmp_path / "master-key")
    provider_value = "synthetic-pre-p11-provider-key"
    store = CredentialStore(connections, SecretCipher(master_key))
    credential = store.create_encrypted("jina", provider_value)

    MigrationRunner(connections, _MIGRATIONS).migrate()

    upgraded = CredentialStore(connections, SecretCipher(master_key))
    value, key_version = upgraded.resolve(credential.credential_id)
    assert value == provider_value
    assert key_version == 1


def test_failed_migration_rolls_back_without_advancing_schema(
    tmp_path: Path,
) -> None:
    """失败 SQL 不写 migration 记录，也不损伤已提交数据。"""
    connections = SqliteConnectionFactory(
        tmp_path / "failed.sqlite3", journal_mode="DELETE"
    )
    migrations = tmp_path / "migrations"
    shutil.copytree(_MIGRATIONS, migrations)
    MigrationRunner(connections, migrations).migrate()
    _seed_control_rows(connections)
    (migrations / "0017_synthetic_failure.sql").write_text(
        "CREATE TABLE must_rollback(value TEXT);\nINVALID SQL;\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationFailed, match="已回滚"):
        MigrationRunner(connections, migrations).migrate()

    with connections.transaction() as connection:
        migration_count = int(
            connection.execute(
                "SELECT count(*) FROM schema_migrations"
            ).fetchone()[0]
        )
        project_count = int(
            connection.execute(
                "SELECT count(*) FROM projects WHERE project_id='prj_upgrade'"
            ).fetchone()[0]
        )
        rollback_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name='must_rollback'"
        ).fetchone()
    assert migration_count == 16
    assert project_count == 1
    assert rollback_table is None
