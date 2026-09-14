"""单调、校验 checksum 且事务执行的 SQLite migration runner。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import ValidationFailed

_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4})_[a-z0-9_]+\.sql$")
_FOREIGN_KEYS_OFF_DIRECTIVE = "-- migration: foreign_keys=off"


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """已确认写入 schema_migrations 的版本证据。"""

    version: int
    checksum: str


class MigrationRunner:
    """只向前执行仓库内不可变 SQL migration。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        migrations_directory: str | Path,
    ) -> None:
        """保存受控连接与 migration 目录。

        Args:
            connections: P06 数据库连接工厂。
            migrations_directory: 只读 migration SQL 目录。

        Returns:
            无返回值。

        """
        self._connections = connections
        self._directory = Path(migrations_directory).resolve(strict=True)

    def migrate(self) -> tuple[AppliedMigration, ...]:
        """校验并应用全部未执行 migration。

        Args:
            无参数；扫描当前 migration 目录。

        Returns:
            数据库当前全部版本及 checksum。

        Raises:
            ValidationFailed: 文件名、顺序、checksum 或 SQL 无效。

        """
        migrations = self._load_migrations()
        self._bootstrap_table()
        applied = self._applied()
        known_versions = {version for version, _, _ in migrations}
        if any(version not in known_versions for version in applied):
            raise ValidationFailed(
                "数据库包含当前程序未知的更高 migration。",
                stage="sqlite.migration",
            )
        for version, checksum, _ in migrations:
            existing = applied.get(version)
            if existing is not None and existing != checksum:
                raise ValidationFailed(
                    "已应用 migration 的 checksum 已变化。",
                    stage="sqlite.migration",
                    details={"version": version},
                )
        for version, checksum, sql in migrations:
            if version in applied:
                continue
            self._apply(version, checksum, sql)
        return tuple(
            AppliedMigration(version=version, checksum=checksum)
            for version, checksum in sorted(self._applied().items())
        )

    def _load_migrations(self) -> tuple[tuple[int, str, str], ...]:
        found: list[tuple[int, str, str]] = []
        for path in sorted(self._directory.glob("*.sql")):
            match = _MIGRATION_NAME.fullmatch(path.name)
            if match is None or path.is_symlink():
                raise ValidationFailed(
                    "Migration 文件名或路径不安全。",
                    stage="sqlite.migration",
                )
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                raise ValidationFailed(
                    "Migration 文件禁止为空。",
                    stage="sqlite.migration",
                )
            found.append(
                (
                    int(match.group("version")),
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    content,
                )
            )
        versions = [version for version, _, _ in found]
        if versions != list(range(1, len(versions) + 1)):
            raise ValidationFailed(
                "Migration 版本必须从 0001 连续递增。",
                stage="sqlite.migration",
            )
        return tuple(found)

    def _bootstrap_table(self) -> None:
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
                "applied_at TEXT NOT NULL)"
            )

    def _applied(self) -> dict[int, str]:
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT version, checksum FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        return {int(row["version"]): str(row["checksum"]) for row in rows}

    def _apply(self, version: int, checksum: str, sql: str) -> None:
        connection = self._connections.connect()
        applied_at = datetime.now(UTC).isoformat()
        foreign_keys_off = sql.lstrip().startswith(_FOREIGN_KEYS_OFF_DIRECTIVE)
        try:
            if foreign_keys_off:
                self._apply_parent_table_rebuild(
                    connection,
                    version=version,
                    checksum=checksum,
                    applied_at=applied_at,
                    sql=sql,
                )
            else:
                script = (
                    "BEGIN IMMEDIATE;\n"
                    f"{sql}\n"
                    "INSERT INTO schema_migrations(version, checksum, "
                    "applied_at) "
                    f"VALUES ({version}, '{checksum}', '{applied_at}');\n"
                    "COMMIT;"
                )
                connection.executescript(script)
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise ValidationFailed(
                "SQLite migration 执行失败并已回滚。",
                stage="sqlite.migration",
                details={
                    "version": version,
                    "error_type": type(error).__name__,
                },
            ) from None
        finally:
            connection.close()

    @staticmethod
    def _apply_parent_table_rebuild(
        connection: sqlite3.Connection,
        *,
        version: int,
        checksum: str,
        applied_at: str,
        sql: str,
    ) -> None:
        """在事务外关闭外键，重建后逐表核对再提交。

        SQLite 不允许在事务中切换 ``foreign_keys``。只有带仓库内显式
        directive 的迁移能走此路径；任一外键不一致都会回滚表重建和版本记录。

        Args:
            connection: 当前迁移独占连接。
            version: 待写入版本号。
            checksum: 已校验的迁移文件摘要。
            applied_at: UTC 应用时间。
            sql: 包含显式 directive 的迁移正文。

        Returns:
            完整提交时无返回值。

        Raises:
            sqlite3.IntegrityError: 重建后的任一外键不成立。

        """
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + sql)
            violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    "migration foreign_key_check failed"
                )
            connection.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at) "
                "VALUES (?, ?, ?)",
                (version, checksum, applied_at),
            )
            connection.commit()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.execute("PRAGMA foreign_keys=ON")


__all__ = ["AppliedMigration", "MigrationRunner"]
