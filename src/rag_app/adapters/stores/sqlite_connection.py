"""SQLite 路径、PRAGMA、事务和锁错误边界。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from rag_app.core.errors import ProviderUnavailable, ValidationFailed

_MAX_BUSY_TIMEOUT_MS = 60_000


class SqliteConnectionFactory:
    """为每个事务创建具有相同安全 PRAGMA 的独立连接。"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        journal_mode: str = "WAL",
    ) -> None:
        """验证受控数据库路径并保存连接配置。

        Args:
            database_path: 新 P06 数据目录内的 SQLite 文件。
            busy_timeout_ms: 有界锁等待毫秒数。
            journal_mode: WAL、DELETE 或 MEMORY。

        Returns:
            无返回值。

        Raises:
            ValidationFailed: 路径、超时或 journal mode 不安全。

        """
        path = Path(database_path)
        if path.exists() and path.is_symlink():
            raise ValidationFailed(
                "SQLite 数据库路径禁止 symlink。",
                stage="sqlite.path",
            )
        if not 1 <= busy_timeout_ms <= _MAX_BUSY_TIMEOUT_MS:
            raise ValidationFailed(
                "SQLite busy timeout 必须在 1 到 60000ms。",
                stage="sqlite.config",
            )
        normalized_mode = journal_mode.upper()
        if normalized_mode not in {"WAL", "DELETE", "MEMORY"}:
            raise ValidationFailed(
                "SQLite journal mode 不受支持。",
                stage="sqlite.config",
            )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise ValidationFailed(
                "SQLite 数据库目录禁止 symlink。",
                stage="sqlite.path",
            )
        self.database_path = path.resolve(strict=False)
        self.busy_timeout_ms = busy_timeout_ms
        self.journal_mode = normalized_mode

    def connect(self) -> sqlite3.Connection:
        """创建并配置一个独立连接。

        Args:
            无参数；读取当前工厂配置。

        Returns:
            已启用 FK、busy timeout 和 row factory 的连接。

        Raises:
            ProviderUnavailable: SQLite 正忙或锁定。

        """
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            connection.execute(f"PRAGMA journal_mode={self.journal_mode}")
            return connection
        except sqlite3.OperationalError as error:
            if _is_lock_error(error):
                raise ProviderUnavailable(
                    "SQLite 暂时被其他写事务占用。",
                    stage="sqlite.connect",
                ) from None
            raise

    @contextlib.contextmanager
    def transaction(
        self, *, write: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """提供显式提交和回滚的独立事务连接。

        Args:
            write: 是否使用 BEGIN IMMEDIATE 获取写锁。

        Yields:
            当前事务连接。

        Returns:
            以上下文管理器形式提供事务连接。

        Raises:
            ProviderUnavailable: 有界等待后仍发生锁冲突。

        """
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as error:
            connection.rollback()
            if _is_lock_error(error):
                raise ProviderUnavailable(
                    "SQLite 暂时被其他写事务占用。",
                    stage="sqlite.transaction",
                ) from None
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def database_identity(self) -> str:
        """返回绑定真实数据库文件的稳定摘要输入。

        Args:
            无参数；读取数据库路径和文件身份。

        Returns:
            不暴露路径的设备和 inode 字符串。

        """
        stat = self.database_path.stat()
        return f"{stat.st_dev}:{stat.st_ino}"


def _is_lock_error(error: sqlite3.OperationalError) -> bool:
    message = str(error).casefold()
    return "locked" in message or "busy" in message


__all__ = ["SqliteConnectionFactory"]
