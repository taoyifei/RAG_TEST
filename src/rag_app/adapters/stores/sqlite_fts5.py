"""SQLite FTS5、Exact Identifier 与受控查询 builder。"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Sequence
from functools import wraps
from typing import ParamSpec, TypeVar

from rag_app.adapters.lexical import DeterministicCjkBigramAnalyzer
from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.capabilities import (
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    ChannelUnavailable,
    Conflict,
    IndexCorrupt,
    IndexNotReady,
    ProviderUnavailable,
)
from rag_app.core.identifiers import canonical_json
from rag_app.core.models import (
    ChannelHit,
    Chunk,
    ExactSearchRequest,
    LexicalSearchRequest,
    SearchHit,
)
from rag_app.core.models.lexical import AnalyzedLexicalQuery
from rag_app.core.ports.lexical_analyzer import LexicalAnalyzerPort
from rag_app.core.query_text import normalize_identifier

_QUERY_TOKEN = re.compile(r"[\w.-]+", flags=re.UNICODE)
_PARAMETERS = ParamSpec("_PARAMETERS")
_RESULT = TypeVar("_RESULT")
_CJK_BIGRAM_LENGTH = 2
_FTS_SCHEMA_VERSION = 2


def _channel_boundary(
    function: Callable[_PARAMETERS, _RESULT],
) -> Callable[_PARAMETERS, _RESULT]:
    """把 adapter 故障分类为可降级可用性或索引腐败。"""

    @wraps(function)
    def wrapped(
        *args: _PARAMETERS.args,
        **kwargs: _PARAMETERS.kwargs,
    ) -> _RESULT:
        """执行被包装的通道操作并分类基础设施异常。

        Args:
            *args: 原函数的位置参数。
            **kwargs: 原函数的关键字参数。

        Returns:
            原函数返回值。

        """
        try:
            return function(*args, **kwargs)
        except ProviderUnavailable as error:
            raise ChannelUnavailable(
                "SQLite FTS 通道暂时不可用。", stage="fts.channel"
            ) from error
        except sqlite3.DatabaseError as error:
            raise IndexCorrupt(
                "SQLite FTS 索引读取失败。", stage="fts.channel"
            ) from error

    return wrapped


class SqliteFtsStore:
    """按 revision/scope 硬过滤并返回 1-based rank 的 FTS5 Store。"""

    descriptor = ComponentDescriptor(
        kind=ComponentKind.LEXICAL_STORE,
        name="sqlite-fts5",
        version="deterministic-cjk-bigram-v2",
        mode=ProviderMode.LOCAL,
    )

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        analyzer: LexicalAnalyzerPort | None = None,
    ) -> None:
        """保存已迁移数据库连接工厂。

        Args:
            connections: P06 SQLite 连接工厂。
            analyzer: 文档与 Query 共用的确定性分析器。

        Returns:
            无返回值。

        """
        self._connections = connections
        self._analyzer = analyzer or DeterministicCjkBigramAnalyzer()
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
            write_chunks_transaction(connection, chunks, self._analyzer)

    @_channel_boundary
    def search(self, request: LexicalSearchRequest) -> tuple[SearchHit, ...]:
        """使用参数化 MATCH 和硬 scope 过滤查询。

        Args:
            request: revision、用户输入和最大命中数。

        Returns:
            按 bm25 顺序映射为 1-based rank 的命中。

        """
        self._ensure_open()
        revision = request.revision
        with self._connections.transaction() as connection:
            table = fts_table_for_revision(
                connection, revision.index_revision_id
            )
            expression = self._query_expression(request.query, table)
            if not expression:
                return ()
            rows = connection.execute(
                f"SELECT c.chunk_json, bm25({table}, 0.0, 0.0, 0.0, "  # noqa: S608
                "4.0, 3.0, 6.0, 1.0) AS raw_score "
                f"FROM {table} JOIN chunks c ON c.row_id={table}.rowid "
                "JOIN index_revisions r ON r.index_revision_id=c.revision_id "
                f"WHERE {table} MATCH ? AND c.revision_id=? "
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

    @_channel_boundary
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
        revision = request.revision
        with self._connections.transaction() as connection:
            table = fts_table_for_revision(
                connection, revision.index_revision_id
            )
            expression = self._query_expression(request.query, table)
            if not expression:
                return ()
            rows = connection.execute(
                "SELECT c.chunk_id, c.document_id, c.document_version_id, "  # noqa: S608
                "c.role, c.section_id, c.content_sha256, "
                f"bm25({table}, 0.0, 0.0, 0.0, "
                "4.0, 3.0, 6.0, 1.0) AS raw_score "
                f"FROM {table} JOIN chunks c ON c.row_id={table}.rowid "
                "JOIN index_revisions r ON r.index_revision_id=c.revision_id "
                f"WHERE {table} MATCH ? AND c.revision_id=? "
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
                channel=(
                    "lexical:fts5-v2"
                    if table == "chunks_fts_v2"
                    else "lexical:fts5-v1"
                ),
                rank=rank,
                raw_score=float(row["raw_score"]),
            )
            for rank, row in enumerate(rows, start=1)
        )

    @_channel_boundary
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
            table = fts_table_for_revision(
                connection, revision.index_revision_id
            )
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
                expression = (
                    build_fts_v2_query(self._analyzer.analyze_query(phrase))
                    if table == "chunks_fts_v2"
                    else _exact_phrase_query(phrase)
                )
                if not expression or len(found) >= request.limit:
                    continue
                rows = connection.execute(
                    "SELECT c.chunk_id, c.document_id, "  # noqa: S608
                    "c.document_version_id, c.role, c.section_id, "
                    f"c.content_sha256 FROM {table} "
                    f"JOIN chunks c ON c.row_id={table}.rowid "
                    "JOIN index_revisions r "
                    "ON r.index_revision_id=c.revision_id "
                    f"WHERE {table} MATCH ? AND c.revision_id=? "
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

    @_channel_boundary
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
            table = fts_table_for_revision(connection, revision_id)
            row = connection.execute(
                f"SELECT count(*) AS value FROM {table} WHERE revision_id=?",  # noqa: S608
                (revision_id,),
            ).fetchone()
        return int(row["value"])

    def _query_expression(self, query: str, table: str) -> str:
        if table == "chunks_fts_v2":
            return build_fts_v2_query(self._analyzer.analyze_query(query))
        return build_fts_query(query)

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
    connection: sqlite3.Connection,
    chunks: Sequence[Chunk],
    analyzer: LexicalAnalyzerPort | None = None,
) -> None:
    """在调用方现有事务中原子写 Chunk/Exact/FTS。

    Args:
        connection: sqlite3 Connection，保持基础设施类型在 adapter 内。
        chunks: canonical Chunk 序列。
        analyzer: 文档与 Query 共用的 v2 分析器。

    Returns:
        无返回值。

    """
    resolved_analyzer = analyzer or DeterministicCjkBigramAnalyzer()
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
        table = fts_table_for_revision(connection, chunk.index_revision_id)
        if table == "chunks_fts_v2":
            analyzed_title = resolved_analyzer.analyze_document(title)
            analyzed_heading = resolved_analyzer.analyze_document(heading)
            analyzed_identifiers = resolved_analyzer.analyze_document(
                identifiers
            )
            analyzed_text = resolved_analyzer.analyze_document(
                chunk.lexical_text
            )
            connection.execute(
                "INSERT INTO chunks_fts_v2(rowid, chunk_id, revision_id, "
                "knowledge_base_id, document_id, analyzer_id, "
                "analyzed_title, analyzed_heading, analyzed_identifiers, "
                "analyzed_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    chunk.chunk_id,
                    chunk.index_revision_id,
                    chunk.knowledge_base_id,
                    chunk.version.document_id,
                    analyzed_text.analyzer_id,
                    analyzed_title.fts_index_text,
                    analyzed_heading.fts_index_text,
                    analyzed_identifiers.fts_index_text,
                    analyzed_text.fts_index_text,
                ),
            )
        else:
            connection.execute(
                "INSERT INTO chunks_fts(rowid, chunk_id, revision_id, "
                "knowledge_base_id, document_id, title, heading, "
                "identifiers, lexical_text) "
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


def build_fts_v2_query(analysis: AnalyzedLexicalQuery) -> str:
    """把受控分析结果组合成有界 FTS 表达式。

    Args:
        analysis: `AnalyzedLexicalQuery`，保持延迟导入面最小。

    Returns:
        CJK group 使用 AND、各语义组使用 OR 的安全表达式。

    """
    groups: list[str] = []
    for tokens in analysis.cjk_groups:
        if not tokens:
            continue
        full_phrase = _fts_quote(tokens[0])
        bigrams = tuple(
            _fts_quote(token)
            for token in tokens[1:]
            if len(token) == _CJK_BIGRAM_LENGTH
        )
        if bigrams and bigrams != (full_phrase,):
            groups.append(
                f"({full_phrase} OR ({' AND '.join(bigrams)}))"
            )
        else:
            groups.append(full_phrase)
    groups.extend(_fts_quote(token) for token in analysis.identifier_tokens)
    return " OR ".join(groups)


def fts_table_for_revision(
    connection: sqlite3.Connection,
    revision_id: str,
) -> str:
    """从不可变 revision 合同选择显式 v1/v2 reader。

    Args:
        connection: 当前 SQLite 事务。
        revision_id: 目标不可变 Revision ID。

    Returns:
        受控 FTS 表名。

    Raises:
        IndexCorrupt: lexical schema JSON 已损坏。
        IndexNotReady: schema 版本未知，需要重建索引。

    """
    row = connection.execute(
        "SELECT lexical_schema_json FROM index_revisions "
        "WHERE index_revision_id=?",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise IndexNotReady("REINDEX_REQUIRED", stage="fts.schema")
    try:
        schema = json.loads(str(row["lexical_schema_json"]))
    except (TypeError, ValueError):
        raise IndexCorrupt(
            "Lexical schema JSON 已损坏。", stage="fts.schema"
        ) from None
    if not isinstance(schema, dict):
        raise IndexCorrupt(
            "Lexical schema 必须为对象。", stage="fts.schema"
        )
    version = schema.get("fts_schema_version")
    if version in {"2", _FTS_SCHEMA_VERSION}:
        return "chunks_fts_v2"
    component_version = schema.get("version")
    if version in {None, "1", 1} and component_version != (
        "deterministic-cjk-bigram-v2"
    ):
        return "chunks_fts"
    raise IndexNotReady("REINDEX_REQUIRED", stage="fts.schema")


def _fts_quote(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


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
    "build_fts_v2_query",
    "fts_table_for_revision",
    "normalize_identifier",
    "write_chunks_transaction",
]
