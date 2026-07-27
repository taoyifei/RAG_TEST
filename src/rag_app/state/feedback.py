"""只保存回答追踪标识与有用性信号的 SQLite 存储。"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["FeedbackStore"]

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_feedback (
    trace_id TEXT PRIMARY KEY,
    useful INTEGER NOT NULL CHECK (useful IN (0, 1)),
    updated_at TEXT NOT NULL
);
"""


class FeedbackStore:
    """幂等保存不含问题、答案和原文的用户反馈。"""

    def __init__(self, database_path: Path) -> None:
        """保存 SQLite 文件位置。

        Args:
            database_path: 与应用状态共用的 SQLite WAL 数据库。

        """
        self._database_path = database_path

    def initialize(self) -> None:
        """初始化反馈表与 WAL 模式。"""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    def record(
        self,
        trace_id: str,
        *,
        useful: bool,
        now: datetime,
    ) -> None:
        """按 trace ID 幂等写入或更新反馈。

        Args:
            trace_id: 最终回答的 32 位十六进制追踪标识。
            useful: 用户选择的有用或没用信号。
            now: 带时区的记录时间。

        Raises:
            ValueError: trace ID 或时间无效。

        """
        if _TRACE_ID_PATTERN.fullmatch(trace_id) is None:
            raise ValueError("trace_id 格式无效。")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now 必须包含时区。")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO answer_feedback (trace_id, useful, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    useful = excluded.useful,
                    updated_at = excluded.updated_at
                """,
                (
                    trace_id,
                    int(useful),
                    now.astimezone(UTC).isoformat(),
                ),
            )

    def counts(self) -> dict[str, int]:
        """返回有用、没用和去重后的反馈总数。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(useful), 0),
                    COALESCE(SUM(1 - useful), 0),
                    COUNT(*)
                FROM answer_feedback
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("SQLite 未返回反馈统计。")
        return {
            "useful": int(row[0]),
            "not_useful": int(row[1]),
            "total": int(row[2]),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10.0)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection
