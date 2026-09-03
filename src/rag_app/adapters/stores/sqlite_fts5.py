"""SQLite FTS5、Exact Identifier 与受控查询 builder。"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Sequence

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import Conflict
from rag_app.core.identifiers import canonical_json
from rag_app.core.models import (
    ChannelHit,
    Chunk,
    ExactSearchRequest,
    LexicalSearchRequest,
    SearchHit,
)
from rag_app.core.query_text import normalize_identifier

_QUERY_TOKEN = re.compile(r"[\w.-]+", flags=re.UNICODE)


class SqliteFtsStore:
    """按 revision/scope 硬过滤并返回 1-based rank 的 FTS5 Store。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.LEXICAL_STORE,
        name="sqlite-fts5",
        version="unicode61-ngram-v1",
        mode=ProviderMode.LOCAL,
    )

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        """保存已迁移数据库连接工厂。

        Args:
            connections: P06 SQLite 连接工厂。

        Returns:
            无返回值。

        """
        self._connections = connections
        self._closed = False

    def write(self, chunks: tuple[Chunk, ...]) -> None:
        """同一事务写 authoritative Chunk、Exact 与 FTS 行。

        Args:
            chunks: 同一已创建 revision 的 canonical chunks。

        Returns:
            无返回值。

        """
        self._ensure_open()
        with self._connections.transaction(write=True) as connection:
            write_chunks_transaction(connection, chunks)

    def search(self, request: LexicalSearchRequest) -> tuple[SearchHit, ...]:
        """使用参数化 MATCH 和硬 scope 过滤查询。

        Args:
            request: revision、用户输入和最大命中数。

        Returns:
            按 bm25 顺序映射为 1-based rank 的命中。

        """
        self._ensure_open()
        expression = build_fts_query(request.query)
        if not expression:
            return ()
        revision = request.revision
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT c.chunk_json, bm25(chunks_fts, 0.0, 0.0, 0.0, "
                "4.0, 3.0, 6.0, 1.0) AS raw_score "
                "FROM chunks_fts JOIN chunks c ON c.row_id=chunks_fts.rowid "
                "JOIN index_revisions r ON r.index_revision_id=c.revision_id "
                "WHERE chunks_fts MATCH ? AND c.revision_id=? "
                "AND r.project_id=? AND r.knowledge_base_id=? "
                "ORDER BY raw_score ASC, c.chunk_id ASC LIMIT ?",
                (
                    expression,
                    revision.index_revision_id,
                    revision.project_id,
                    revision.knowledge_base_id,
                    request.limit,
                ),
            ).fetchall()
        return tuple(
            SearchHit(
                chunk=Chunk.model_validate_json(str(row["chunk_json"])),
                score=1.0 / rank,
                rank=rank,
                channels=("lexical:fts5",),
            )
            for rank, row in enumerate(rows, start=1)
        )

    def search_candidates(
        self, request: LexicalSearchRequest
    ) -> tuple[ChannelHit, ...]:
        """返回不携带正文且受 revision/scope 硬过滤的候选。

        Args:
            request: revision、用户输入和最大命中数。

        Returns:
            仅含稳定 ID、诊断分数和 1-based rank 的结果。

        """
        self._ensure_open()
        expression = build_fts_query(request.query)
        if not expression:
            return ()
        revision = request.revision
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT c.chunk_id, c.document_id, c.document_version_id, "
                "c.role, c.section_id, c.content_sha256, "
                "bm25(chunks_fts, 0.0, 0.0, 0.0, "
                "4.0, 3.0, 6.0, 1.0) AS raw_score "
                "FROM chunks_fts JOIN chunks c ON c.row_id=chunks_fts.rowid "
                "JOIN index_revisions r ON r.index_revision_id=c.revision_id "
                "WHERE chunks_fts MATCH ? AND c.revision_id=? "
                "AND r.project_id=? AND r.knowledge_base_id=? "
                "ORDER BY raw_score ASC, c.chunk_id ASC LIMIT ?",
                (
                    expression,
                    revision.index_revision_id,
                    revision.project_id,
                    revision.knowledge_base_id,
                    request.limit,
                ),
            ).fetchall()
        return tuple(
            ChannelHit(
                revision_id=revision.index_revision_id,
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                document_version_id=str(row["document_version_id"]),
                role=str(row["role"]),
                section_id=str(row["section_id"]),
                content_sha256=str(row["content_sha256"]),
                channel="lexical:fts5",
                rank=rank,
                raw_score=float(row["raw_score"]),
            )
            for rank, row in enumerate(rows, start=1)
        )

    def search_exact_candidates(
        self, request: ExactSearchRequest
    ) -> tuple[ChannelHit, ...]:
        """查询正规 identifier 表和受控 quoted phrase。

        Args:
            request: revision、已分析 identifier/phrase 和最大数量。

        Returns:
            identifier 优先、稳定去重且不携带正文的候选。

        """
        self._ensure_open()
        revision = request.revision
        found: list[tuple[sqlite3.Row, str, bool]] = []
        seen: set[str] = set()
        with self._connections.transaction() as connection:
            for identifier in request.identifiers:
                normalized = normalize_identifier(identifier)
                if not normalized:
                    continue
                rows = connection.execute(
                    "SELECT e.chunk_id, c.document_id, "
                    "c.document_version_id, c.role, c.section_id, "
                    "c.content_sha256 FROM exact_identifiers e "
                    "JOIN chunks c ON c.revision_id=e.revision_id "
                    "AND c.chunk_id=e.chunk_id "
                    "JOIN index_revisions r "
                    "ON r.index_revision_id=e.revision_id "
                    "WHERE e.revision_id=? AND e.normalized_identifier=? "
                    "AND r.project_id=? AND r.knowledge_base_id=? "
                    "ORDER BY e.chunk_id LIMIT ?",
                    (
                        revision.index_revision_id,
                        normalized,
                        revision.project_id,
                        revision.knowledge_base_id,
                        request.limit,
                    ),
                ).fetchall()
                for row in rows:
                    chunk_id = str(row["chunk_id"])
                    if chunk_id not in seen:
                        seen.add(chunk_id)
                        found.append((row, "identifier", True))
            for phrase in request.quoted_phrases:
                expression = _exact_phrase_query(phrase)
                if not expression or len(found) >= request.limit:
                    continue
                rows = connection.execute(
                    "SELECT c.chunk_id, c.document_id, "
                    "c.document_version_id, c.role, c.section_id, "
                    "c.content_sha256 FROM chunks_fts "
                    "JOIN chunks c ON c.row_id=chunks_fts.rowid "
                    "JOIN index_revisions r "
                    "ON r.index_revision_id=c.revision_id "
                    "WHERE chunks_fts MATCH ? AND c.revision_id=? "
                    "AND r.project_id=? AND r.knowledge_base_id=? "
                    "ORDER BY c.chunk_id LIMIT ?",
                    (
                        expression,
                        revision.index_revision_id,
                        revision.project_id,
                        revision.knowledge_base_id,
                        request.limit,
                    ),
                ).fetchall()
                for row in rows:
                    chunk_id = str(row["chunk_id"])
                    if chunk_id not in seen:
                        seen.add(chunk_id)
                        found.append((row, "quoted_phrase", False))
        return tuple(
            ChannelHit(
                revision_id=revision.index_revision_id,
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                document_version_id=str(row["document_version_id"]),
                role=str(row["role"]),
                section_id=str(row["section_id"]),
                content_sha256=str(row["content_sha256"]),
                channel="exact",
                rank=rank,
                raw_score=1.0,
                match_type=match_type,
                must_keep=must_keep,
            )
            for rank, (row, match_type, must_keep) in enumerate(
                found[: request.limit], start=1
            )
        )

    def search_exact(
        self,
        revision_id: str,
        identifier: str,
        *,
        limit: int = 20,
    ) -> tuple[str, ...]:
        """从正规 Exact 表查询 identifier。

        Args:
            revision_id: 目标不可变 revision。
            identifier: 用户输入的完整 identifier。
            limit: 最大 chunk ID 数。

        Returns:
            稳定排序的 chunk IDs。

        """
        normalized = normalize_identifier(identifier)
        if not normalized:
            return ()
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT chunk_id FROM exact_identifiers "
                "WHERE revision_id=? AND normalized_identifier=? "
                "ORDER BY chunk_id LIMIT ?",
                (revision_id, normalized, limit),
            ).fetchall()
        return tuple(str(row["chunk_id"]) for row in rows)

    def count_revision(self, revision_id: str) -> int:
        """统计实际 FTS staging 行。

        Args:
            revision_id: 目标 revision。

        Returns:
            FTS 行数。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT count(*) AS value FROM chunks_fts WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
        return int(row["value"])

    def close(self) -> None:
        """幂等关闭 Store。

        Args:
            无参数；不拥有共享连接工厂。

        Returns:
            无返回值。

        """
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SqliteFtsStore 已关闭。")


def write_chunks_transaction(
    connection: sqlite3.Connection, chunks: Sequence[Chunk]
) -> None:
    """在调用方现有事务中原子写 Chunk/Exact/FTS。

    Args:
        connection: sqlite3 Connection，保持基础设施类型在 adapter 内。
        chunks: canonical Chunk 序列。

    Returns:
        无返回值。

    """
    for chunk in chunks:
        chunk_json = chunk.model_dump_json()
        existing = connection.execute(
            "SELECT row_id, chunk_json FROM chunks "
            "WHERE revision_id=? AND chunk_id=?",
            (chunk.index_revision_id, chunk.chunk_id),
        ).fetchone()
        if existing is not None:
            if str(existing["chunk_json"]) != chunk_json:
                raise Conflict(
                    "同一 revision/chunk 已存在不同内容。", stage="fts.write"
                )
            continue
        cursor = connection.execute(
            "INSERT INTO chunks("
            "revision_id, chunk_id, document_id, document_version_id, role, "
            "parent_node_id, section_id, neighbor_group_id, previous_chunk_id, "
            "next_chunk_id, citation_text, embedding_text, lexical_text, "
            "heading_path_json, source_spans_json, identifiers_json, "
            "token_count, token_count_is_estimate, tokenizer_id, "
            "content_sha256, metadata_json, chunk_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            (
                chunk.index_revision_id,
                chunk.chunk_id,
                chunk.version.document_id,
                chunk.version.document_version_id,
                chunk.role.value,
                chunk.parent_node_id,
                chunk.section_id,
                chunk.neighbor_group_id,
                chunk.previous_chunk_id,
                chunk.next_chunk_id,
                chunk.citation_text,
                chunk.embedding_text,
                chunk.lexical_text,
                canonical_json(chunk.heading_path),
                canonical_json(
                    tuple(
                        span.model_dump(mode="json")
                        for span in chunk.source_spans
                    )
                ),
                canonical_json(chunk.identifiers),
                chunk.token_count,
                int(chunk.token_count_is_estimate),
                chunk.tokenizer_id,
                chunk.content_sha256,
                canonical_json(chunk.metadata),
                chunk_json,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite 未返回 chunk rowid。")
        row_id = cursor.lastrowid
        title = chunk.heading_path[0] if chunk.heading_path else ""
        heading = " / ".join(chunk.heading_path)
        identifiers = " ".join(_identifier_forms(chunk.identifiers))
        connection.execute(
            "INSERT INTO chunks_fts(rowid, chunk_id, revision_id, "
            "knowledge_base_id, document_id, title, heading, identifiers, "
            "lexical_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                chunk.chunk_id,
                chunk.index_revision_id,
                chunk.knowledge_base_id,
                chunk.version.document_id,
                title,
                heading,
                identifiers,
                chunk.lexical_text,
            ),
        )
        for identifier in chunk.identifiers:
            connection.execute(
                "INSERT INTO exact_identifiers("
                "revision_id, chunk_id, identifier, normalized_identifier) "
                "VALUES (?, ?, ?, ?)",
                (
                    chunk.index_revision_id,
                    chunk.chunk_id,
                    identifier,
                    normalize_identifier(identifier),
                ),
            )


def build_fts_query(query: str) -> str:
    """把普通用户输入转换为仅含受控 phrase 的 FTS 表达式。

    Args:
        query: 不可信用户输入。

    Returns:
        参数化 MATCH 使用的安全表达式；空输入返回空字符串。

    """
    normalized = unicodedata.normalize("NFKC", query).casefold()
    tokens = _QUERY_TOKEN.findall(normalized)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if _contains_cjk(token):
            expanded.extend(
                token[index : index + 2] for index in range(len(token) - 1)
            )
    unique = tuple(dict.fromkeys(item for item in expanded if item))
    return " OR ".join(
        f'"{item.replace(chr(34), chr(34) * 2)}"' for item in unique
    )


def _exact_phrase_query(phrase: str) -> str:
    normalized = unicodedata.normalize("NFKC", phrase).casefold().strip()
    if not normalized:
        return ""
    return f'"{normalized.replace(chr(34), chr(34) * 2)}"'


def _identifier_forms(identifiers: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    for identifier in identifiers:
        values.extend(
            (
                identifier,
                unicodedata.normalize("NFKC", identifier).casefold(),
                normalize_identifier(identifier),
            )
        )
    return tuple(dict.fromkeys(value for value in values if value))


def _contains_cjk(value: str) -> bool:
    return any("\u3400" <= character <= "\u9fff" for character in value)


__all__ = [
    "SqliteFtsStore",
    "build_fts_query",
    "normalize_identifier",
    "write_chunks_transaction",
]
