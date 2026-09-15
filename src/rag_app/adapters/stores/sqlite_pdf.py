"""规范化 PDF 解析缓存和持久 Job 页面进度。"""

from __future__ import annotations

from datetime import UTC, datetime

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.models import PdfPageProgress, PdfParseResult


class SqlitePdfParseCache:
    """只按完整五元解析身份复用 Paddle 结果。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        self._connections = connections

    def get(
        self,
        *,
        source_sha256: str,
        parser_mode: str,
        parser_model: str,
        parser_revision: str,
        parser_options_identity: str,
    ) -> PdfParseResult | None:
        """读取完全匹配且仍能通过模型验证的缓存。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT result_json FROM pdf_parse_cache "
                "WHERE source_sha256=? AND parser_mode=? AND parser_model=? "
                "AND parser_revision=? AND parser_options_identity=?",
                (
                    source_sha256,
                    parser_mode,
                    parser_model,
                    parser_revision,
                    parser_options_identity,
                ),
            ).fetchone()
        if row is None:
            return None
        return PdfParseResult.model_validate_json(str(row[0]))

    def put(self, result: PdfParseResult) -> None:
        """幂等保存已由 PdfParseResult 证明完整的页面集合。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO pdf_parse_cache("
                "source_sha256, parser_mode, parser_model, parser_revision, "
                "parser_options_identity, page_count, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT("
                "source_sha256, parser_mode, parser_model, parser_revision, "
                "parser_options_identity) DO NOTHING",
                (
                    result.source_sha256,
                    result.parser_mode.value,
                    result.parser_model,
                    result.parser_revision,
                    result.parser_options_identity,
                    result.page_count,
                    result.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )


class SqlitePdfProgressStore:
    """持久化一个入库 Job 的最新非敏感 PDF 页面进度。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        self._connections = connections

    def put_progress(self, job_id: str, progress: PdfPageProgress) -> None:
        """覆盖保存页面计数、失败页、模式和模型。"""
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO pdf_job_progress("
                "job_id, progress_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
                "progress_json=excluded.progress_json, "
                "updated_at=excluded.updated_at",
                (
                    job_id,
                    progress.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_progress(self, job_id: str) -> PdfPageProgress | None:
        """读取 Job 最新 PDF 页面进度。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT progress_json FROM pdf_job_progress WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return (
            None
            if row is None
            else PdfPageProgress.model_validate_json(str(row[0]))
        )


__all__ = ["SqlitePdfParseCache", "SqlitePdfProgressStore"]
