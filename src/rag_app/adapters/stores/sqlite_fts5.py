"""SQLite FTS5、Exact Identifier 与受控查询 builder。"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Sequence
from functools import wraps
from typing import ParamSpec, TypeVar

from pydantic import ValidationError

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
    RequestedAnswerType,
    SearchHit,
    StructuralSearchRequest,
)
from rag_app.core.models.lexical import AnalyzedLexicalQuery
from rag_app.core.ports.lexical_analyzer import LexicalAnalyzerPort
from rag_app.core.query_text import (
    context_label_variants,
    normalize_document_label,
    normalize_identifier,
    normalize_section_heading_label,
    select_unique_label_owner,
)

_QUERY_TOKEN = re.compile(r"[\w.-]+", flags=re.UNICODE)
_PARAMETERS = ParamSpec("_PARAMETERS")
_RESULT = TypeVar("_RESULT")
_CJK_BIGRAM_LENGTH = 2
_FTS_SCHEMA_VERSION = 2
_STRUCTURAL_SCAN_MULTIPLIER = 4
_STRUCTURAL_SCAN_CAP = 200
_STRUCTURAL_TABLE_CLOSURE_CAP = 8
_FLOW_ARCHITECTURE_PATH_DEPTH = 2
_NUMBERED_STAGE_HEADING = re.compile(r"^\d+\.\d+(?:\.\d+)?\s+\S")
_FLOW_ARCHITECTURE_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*)?\s*流程架构$")
_STAGE_QUERY = re.compile(r"阶段|环节|全流程")
_STRUCTURAL_RELATION_ALIASES = {
    RequestedAnswerType.DEFINITION: re.compile(
        r"定义|释义|术语|说明|含义|是指|指的是"
    ),
    RequestedAnswerType.PURPOSE: re.compile(r"目的|目标|作用|用途|宗旨|旨在"),
    RequestedAnswerType.DUTIES: re.compile(
        r"职责|工作内容|岗位任务|负责事项|主要工作|任务说明"
    ),
    RequestedAnswerType.RESPONSIBLE_PARTY: re.compile(
        r"责任角色|责任人|负责人|主责|牵头|经办|承办|受理角色|受理人|"
        r"负责部门|责任部门"
    ),
    RequestedAnswerType.ENUMERATION: re.compile(
        r"阶段|组成|分类|类型|清单|目录|包括|包含"
    ),
    RequestedAnswerType.COUNT: re.compile(r"阶段|组成|分类|类型|清单|目录"),
    RequestedAnswerType.ORDINAL_ITEM: re.compile(r"阶段|步骤|流程|清单|目录"),
    RequestedAnswerType.PROCEDURE: re.compile(r"步骤|流程|顺序|程序|阶段"),
    RequestedAnswerType.SECTION_SUMMARY: re.compile(
        r"章节|管理要求|工作要求|规定|要求|说明"
    ),
}


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
        except ValidationError as error:
            raise IndexCorrupt(
                "SQLite FTS canonical Chunk 无法解析。", stage="fts.channel"
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
                "JOIN documents d ON d.document_id=c.document_id "
                f"WHERE {table} MATCH ? AND c.revision_id=? "
                "AND r.project_id=? AND r.knowledge_base_id=? "
                "AND d.deleted_at IS NULL AND d.lifecycle_status='active' "
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
                "JOIN documents d ON d.document_id=c.document_id "
                f"WHERE {table} MATCH ? AND c.revision_id=? "
                "AND r.project_id=? AND r.knowledge_base_id=? "
                "AND d.deleted_at IS NULL AND d.lifecycle_status='active' "
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
    def search_structural_candidates(  # noqa: PLR0912, PLR0915
        self, request: StructuralSearchRequest
    ) -> tuple[ChannelHit, ...]:
        """在现有 canonical 字段上执行独立、有界的结构重排。

        Args:
            request: 活动 revision 与共享 typed semantics。

        Returns:
            单一 structural family 中带原因码的 1-based rank 候选。

        """
        self._ensure_open()
        revision = request.revision
        scan_limit = min(
            _STRUCTURAL_SCAN_CAP,
            max(request.limit, request.limit * _STRUCTURAL_SCAN_MULTIPLIER),
        )
        context_variants = context_label_variants(
            request.context_qualifier or ""
        )
        search_text = " ".join(
            value
            for value in (
                request.target,
                *context_variants,
                request.relation,
                request.query,
            )
            if value
        )
        table_closure_rows: dict[str, frozenset[int]] = {}
        with self._connections.transaction() as connection:
            table = fts_table_for_revision(
                connection, revision.index_revision_id
            )
            rows: list[sqlite3.Row] = []
            expression = self._query_expression(search_text, table)
            if expression:
                rows.extend(
                    connection.execute(
                        "SELECT c.chunk_json, d.display_name, "  # noqa: S608
                        f"bm25({table}, 0.0, 0.0, 0.0, "
                        "4.0, 3.0, 6.0, 1.0) AS lexical_rank "
                        f"FROM {table} JOIN chunks c "
                        f"ON c.row_id={table}.rowid "
                        "JOIN index_revisions r "
                        "ON r.index_revision_id=c.revision_id "
                        "JOIN documents d ON d.document_id=c.document_id "
                        f"WHERE {table} MATCH ? AND c.revision_id=? "
                        "AND r.project_id=? AND r.knowledge_base_id=? "
                        "AND d.deleted_at IS NULL "
                        "AND d.lifecycle_status='active' "
                        "ORDER BY lexical_rank ASC, c.chunk_id ASC LIMIT ?",
                        (
                            expression,
                            revision.index_revision_id,
                            revision.project_id,
                            revision.knowledge_base_id,
                            scan_limit,
                        ),
                    ).fetchall()
                )
            document_term = (
                _source_lookup_term(request.source_qualifier)
                if request.source_qualifier
                else (
                    context_variants[1]
                    if len(context_variants) > 1
                    else request.context_qualifier or request.target
                )
            )
            if document_term:
                rows.extend(
                    connection.execute(
                        "SELECT c.chunk_json, d.display_name, "
                        "0.0 AS lexical_rank FROM chunks c "
                        "JOIN index_revisions r "
                        "ON r.index_revision_id=c.revision_id "
                        "JOIN documents d ON d.document_id=c.document_id "
                        "WHERE c.revision_id=? AND r.project_id=? "
                        "AND r.knowledge_base_id=? "
                        "AND d.deleted_at IS NULL "
                        "AND d.lifecycle_status='active' "
                        "AND d.display_name LIKE ? ESCAPE '\\' "
                        "ORDER BY c.row_id ASC LIMIT ?",
                        (
                            revision.index_revision_id,
                            revision.project_id,
                            revision.knowledge_base_id,
                            f"%{_like_literal(document_term)}%",
                            scan_limit,
                        ),
                    ).fetchall()
                )
            parsed = _parse_structural_rows(rows)
            anchor_ids = _structural_document_anchor_ids(parsed, request)
            if len(anchor_ids) == 1:
                # 显式来源已经唯一定位文档后，改用对象/关系在该文档内
                # 再检索一次。否则文件名位于每个 FTS 行的 title 字段，
                # 大文档前部大量短块会淹没后部的短标题（如“预期”）。
                document_id = next(iter(anchor_ids))
                scoped_text = " ".join(
                    value
                    for value in (
                        request.target,
                        *context_variants,
                        request.relation,
                    )
                    if value
                )
                scoped_expression = self._query_expression(scoped_text, table)
                if scoped_expression:
                    scoped_rows = connection.execute(
                        "SELECT c.chunk_json, d.display_name, "  # noqa: S608
                        f"bm25({table}, 0.0, 0.0, 0.0, "
                        "4.0, 3.0, 6.0, 1.0) AS lexical_rank "
                        f"FROM {table} JOIN chunks c "
                        f"ON c.row_id={table}.rowid "
                        "JOIN index_revisions r "
                        "ON r.index_revision_id=c.revision_id "
                        "JOIN documents d ON d.document_id=c.document_id "
                        f"WHERE {table} MATCH ? AND c.revision_id=? "
                        "AND c.document_id=? AND r.project_id=? "
                        "AND r.knowledge_base_id=? AND d.deleted_at IS NULL "
                        "AND d.lifecycle_status='active' "
                        "ORDER BY lexical_rank ASC, c.chunk_id ASC LIMIT ?",
                        (
                            scoped_expression,
                            revision.index_revision_id,
                            document_id,
                            revision.project_id,
                            revision.knowledge_base_id,
                            scan_limit,
                        ),
                    ).fetchall()
                    rows = [*scoped_rows, *rows]
                    parsed = _parse_structural_rows(rows)
                    anchor_ids = _structural_document_anchor_ids(
                        parsed, request
                    )
            remaining = _STRUCTURAL_SCAN_CAP - len(parsed)
            if len(anchor_ids) == 1 and remaining > 0:
                document_id = next(iter(anchor_ids))
                if _is_structural_stage_request(request):
                    rows.extend(
                        connection.execute(
                            "SELECT c.chunk_json, d.display_name, "
                            "0.0 AS lexical_rank FROM chunks c "
                            "JOIN index_revisions r "
                            "ON r.index_revision_id=c.revision_id "
                            "JOIN documents d ON d.document_id=c.document_id "
                            "WHERE c.row_id IN ("
                            "SELECT MIN(row_id) FROM chunks "
                            "WHERE revision_id=? AND document_id=? "
                            "AND heading_path_json <> '[]' "
                            "GROUP BY heading_path_json) "
                            "AND c.revision_id=? "
                            "AND r.project_id=? AND r.knowledge_base_id=? "
                            "AND d.deleted_at IS NULL "
                            "AND d.lifecycle_status='active' "
                            "ORDER BY c.row_id ASC LIMIT ?",
                            (
                                revision.index_revision_id,
                                document_id,
                                revision.index_revision_id,
                                revision.project_id,
                                revision.knowledge_base_id,
                                remaining,
                            ),
                        ).fetchall()
                    )
                else:
                    rows.extend(
                        connection.execute(
                            "SELECT c.chunk_json, d.display_name, "
                            "0.0 AS lexical_rank FROM chunks c "
                            "JOIN index_revisions r "
                            "ON r.index_revision_id=c.revision_id "
                            "JOIN documents d ON d.document_id=c.document_id "
                            "WHERE c.revision_id=? AND c.document_id=? "
                            "AND r.project_id=? AND r.knowledge_base_id=? "
                            "AND d.deleted_at IS NULL "
                            "AND d.lifecycle_status='active' "
                            "ORDER BY c.row_id ASC LIMIT ?",
                            (
                                revision.index_revision_id,
                                document_id,
                                revision.project_id,
                                revision.knowledge_base_id,
                                remaining,
                            ),
                        ).fetchall()
                    )
            expanded = _parse_structural_rows(rows)
            table_closure_rows = _structural_table_closure_rows(
                expanded,
                request,
                unique_anchor=(
                    next(iter(anchor_ids)) if len(anchor_ids) == 1 else None
                ),
            )
            closure_rows: list[sqlite3.Row] = []
            for parent_node_id in table_closure_rows:
                closure_rows.extend(
                    connection.execute(
                        "SELECT c.chunk_json, d.display_name, "
                        "0.0 AS lexical_rank FROM chunks c "
                        "JOIN index_revisions r "
                        "ON r.index_revision_id=c.revision_id "
                        "JOIN documents d ON d.document_id=c.document_id "
                        "WHERE c.revision_id=? AND c.parent_node_id=? "
                        "AND r.project_id=? AND r.knowledge_base_id=? "
                        "AND d.deleted_at IS NULL "
                        "AND d.lifecycle_status='active' "
                        "ORDER BY c.row_id ASC LIMIT ?",
                        (
                            revision.index_revision_id,
                            parent_node_id,
                            revision.project_id,
                            revision.knowledge_base_id,
                            _STRUCTURAL_SCAN_CAP,
                        ),
                    ).fetchall()
                )
            # 目标表的表头与目标行优先占用同一个有界扫描窗口。否则大文档
            # 中先出现的普通 chunk 可能把表头裁掉，Evidence 无法验证列语义。
            rows = [*closure_rows, *rows]
        parsed = _parse_structural_rows(rows)
        anchor_ids = _structural_document_anchor_ids(parsed, request)
        unique_anchor = next(iter(anchor_ids)) if len(anchor_ids) == 1 else None
        scored: list[
            tuple[
                float,
                float,
                int,
                Chunk,
                str,
                str,
                tuple[str, ...] | None,
            ]
        ] = []
        for chunk, display_name, lexical_rank in parsed:
            if (
                request.context_qualifier
                and unique_anchor is not None
                and chunk.version.document_id != unique_anchor
            ):
                continue
            structural = _structural_score(
                chunk,
                display_name,
                request,
                document_target_match=(
                    unique_anchor is not None
                    and request.source_qualifier is None
                    and chunk.version.document_id == unique_anchor
                ),
                table_closure=_chunk_in_table_closure(
                    chunk, table_closure_rows
                ),
            )
            if structural is None:
                continue
            score, reason = structural
            stage_group = _structural_stage_group(chunk, request)
            if reason != "STRUCTURAL_STAGE_HEADING":
                stage_group = None
            scored.append(
                (
                    score,
                    lexical_rank,
                    _chunk_source_ordinal(chunk),
                    chunk,
                    reason,
                    display_name,
                    stage_group,
                )
            )
        scored.sort(
            key=lambda item: (-item[0], item[1], item[2], item[3].chunk_id)
        )
        selected: list[tuple[float, Chunk, str]] = []
        seen_stage_groups: set[tuple[str, ...]] = set()
        for score, _, _, chunk, reason, _, stage_group in scored:
            if stage_group is not None:
                group_key = (chunk.version.document_version_id, *stage_group)
                if group_key in seen_stage_groups:
                    continue
                seen_stage_groups.add(group_key)
            selected.append((score, chunk, reason))
            if len(selected) >= request.limit:
                break
        return tuple(
            ChannelHit(
                revision_id=revision.index_revision_id,
                chunk_id=chunk.chunk_id,
                document_id=chunk.version.document_id,
                document_version_id=chunk.version.document_version_id,
                role=chunk.role.value,
                section_id=chunk.section_id,
                content_sha256=chunk.content_sha256,
                channel="structural:canonical-v1",
                rank=rank,
                raw_score=score,
                match_type=reason,
            )
            for rank, (score, chunk, reason) in enumerate(selected, 1)
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
                    "JOIN documents d ON d.document_id=c.document_id "
                    "WHERE e.revision_id=? AND e.normalized_identifier=? "
                    "AND r.project_id=? AND r.knowledge_base_id=? "
                    "AND d.deleted_at IS NULL AND d.lifecycle_status='active' "
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
                    "JOIN documents d ON d.document_id=c.document_id "
                    f"WHERE {table} MATCH ? AND c.revision_id=? "
                    "AND r.project_id=? AND r.knowledge_base_id=? "
                    "AND d.deleted_at IS NULL AND d.lifecycle_status='active' "
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
                "SELECT e.chunk_id FROM exact_identifiers e "
                "JOIN chunks c ON c.revision_id=e.revision_id "
                "AND c.chunk_id=e.chunk_id "
                "JOIN documents d ON d.document_id=c.document_id "
                "WHERE e.revision_id=? AND e.normalized_identifier=? "
                "AND d.deleted_at IS NULL AND d.lifecycle_status='active' "
                "ORDER BY e.chunk_id LIMIT ?",
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
        normalized_identifiers: set[str] = set()
        for identifier in chunk.identifiers:
            normalized = normalize_identifier(identifier)
            # Exact 主键采用规范形式；原文变体仍完整保留在 Chunk 中。
            if normalized in normalized_identifiers:
                continue
            normalized_identifiers.add(normalized)
            connection.execute(
                "INSERT INTO exact_identifiers("
                "revision_id, chunk_id, identifier, normalized_identifier) "
                "VALUES (?, ?, ?, ?)",
                (
                    chunk.index_revision_id,
                    chunk.chunk_id,
                    identifier,
                    normalized,
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
            groups.append(f"({full_phrase} OR ({' AND '.join(bigrams)}))")
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
        raise IndexCorrupt("Lexical schema 必须为对象。", stage="fts.schema")
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


def _like_literal(value: str) -> str:
    """转义参数化 LIKE 中具有模式含义的字符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _structural_normalize(value: str) -> str:
    """为结构标签匹配执行 NFKC、大小写和分隔符统一。"""
    return normalize_document_label(value)


def _structural_contains(needle: str | None, value: str) -> bool:
    """只用请求原词判断结构标签包含关系，不维护实体别名。"""
    if not needle:
        return False
    normalized_needle = _structural_normalize(needle)
    normalized_value = _structural_normalize(value)
    return bool(normalized_needle) and normalized_needle in normalized_value


def _source_lookup_term(value: str) -> str:
    """移除问句附加的通用载体词，保留动态文档身份核心。"""
    normalized = unicodedata.normalize("NFKC", value).strip()
    return re.sub(r"(?:规范|文档|制度|手册)$", "", normalized).strip()


def _structural_source_contains(needle: str | None, value: str) -> bool:
    """允许来源限定比真实文件名多一个通用文档载体词。"""
    if not needle:
        return False
    return _structural_contains(_source_lookup_term(needle), value)


def _parse_structural_rows(
    rows: Sequence[sqlite3.Row],
) -> tuple[tuple[Chunk, str, float], ...]:
    """解析并按 chunk 身份去重一次结构通道扫描结果。"""
    parsed: list[tuple[Chunk, str, float]] = []
    seen: set[str] = set()
    for row in rows:
        chunk = Chunk.model_validate_json(str(row["chunk_json"]))
        if chunk.chunk_id in seen or len(parsed) >= _STRUCTURAL_SCAN_CAP:
            continue
        seen.add(chunk.chunk_id)
        parsed.append(
            (chunk, str(row["display_name"]), float(row["lexical_rank"]))
        )
    return tuple(parsed)


def _structural_document_anchor_ids(
    rows: tuple[tuple[Chunk, str, float], ...],
    request: StructuralSearchRequest,
) -> set[str]:
    """从文件名、显式来源或根标题确定唯一目标文档。"""
    source_matches = {
        chunk.version.document_id
        for chunk, display_name, _ in rows
        if request.source_qualifier
        and _structural_source_contains(request.source_qualifier, display_name)
    }
    if source_matches:
        return source_matches
    if request.context_qualifier:
        context_labels = tuple(
            (chunk.version.document_id, display_name)
            for chunk, display_name, _ in rows
        )
        context_owners = {
            owner
            for variant in context_label_variants(request.context_qualifier)
            if (owner := select_unique_label_owner(variant, context_labels))
            is not None
        }
        if len(context_owners) == 1:
            return context_owners
    if not request.target:
        return set()
    labels = [
        (chunk.version.document_id, display_name)
        for chunk, display_name, _ in rows
    ]
    labels.extend(
        (chunk.version.document_id, chunk.citation_text)
        for chunk, _, _ in rows
        if not chunk.heading_path
    )
    owner = select_unique_label_owner(request.target, labels)
    return {owner} if owner is not None else set()


def _chunk_table_row_indices(chunk: Chunk) -> frozenset[int]:
    """读取 canonical table atom 的真实行号。"""
    atoms = dict(chunk.metadata).get("atoms", [])
    rows: set[int] = set()
    if not isinstance(atoms, list):
        return frozenset()
    for atom in atoms:
        if not isinstance(atom, dict):
            continue
        metadata = atom.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        row = metadata.get("row_index")
        if isinstance(row, int) and row >= 0:
            rows.add(row)
    return frozenset(rows)


def _structural_table_closure_rows(
    rows: tuple[tuple[Chunk, str, float], ...],
    request: StructuralSearchRequest,
    *,
    unique_anchor: str | None,
) -> dict[str, frozenset[int]]:
    """定位含目标的少量 table 与真实目标行，供表头闭合。"""
    if (
        request.answer_type
        not in {
            RequestedAnswerType.DEFINITION,
            RequestedAnswerType.DUTIES,
            RequestedAnswerType.RESPONSIBLE_PARTY,
            RequestedAnswerType.SECTION_SUMMARY,
        }
        or (
            request.answer_type is RequestedAnswerType.SECTION_SUMMARY
            and request.relation != "对应内容"
        )
        or not request.target
    ):
        return {}
    relation_pattern = _STRUCTURAL_RELATION_ALIASES.get(request.answer_type)
    context_variants = context_label_variants(request.context_qualifier or "")
    candidates: dict[str, set[int]] = {}
    context_matches: set[str] = set()
    relation_matches: set[str] = set()
    for chunk, display_name, _ in rows:
        parent_node_id = chunk.parent_node_id
        if (
            chunk.role.value != "table"
            or parent_node_id is None
            or (unique_anchor and chunk.version.document_id != unique_anchor)
            or (
                request.source_qualifier
                and not _structural_source_contains(
                    request.source_qualifier,
                    " ".join((display_name, *chunk.heading_path)),
                )
            )
        ):
            continue
        row_indices = {
            row for row in _chunk_table_row_indices(chunk) if row > 0
        }
        searchable = " ".join(
            (
                " / ".join(chunk.heading_path),
                chunk.lexical_text,
                chunk.citation_text,
            )
        )
        if not row_indices or not _structural_contains(
            request.target, searchable
        ):
            continue
        heading_label = " ".join((display_name, *chunk.heading_path))
        candidates.setdefault(parent_node_id, set()).update(row_indices)
        if any(
            _structural_contains(variant, heading_label)
            for variant in context_variants
        ):
            context_matches.add(parent_node_id)
        if relation_pattern and relation_pattern.search(heading_label):
            relation_matches.add(parent_node_id)
    selected_ids = list(candidates)
    if request.context_qualifier and context_matches:
        selected_ids = [
            parent_node_id
            for parent_node_id in selected_ids
            if parent_node_id in context_matches
        ]
    matched_relations = relation_matches.intersection(selected_ids)
    if matched_relations:
        selected_ids = [
            parent_node_id
            for parent_node_id in selected_ids
            if parent_node_id in matched_relations
        ]
    # 截断多个同名表会把歧义伪装成唯一命中；超出小规模闭合上限时
    # 直接不做闭合，由 Evidence 保持拒答。
    if len(selected_ids) > _STRUCTURAL_TABLE_CLOSURE_CAP:
        return {}
    return {
        parent_node_id: frozenset(candidates[parent_node_id])
        for parent_node_id in selected_ids
    }


def _chunk_in_table_closure(
    chunk: Chunk, closure_rows: dict[str, frozenset[int]]
) -> bool:
    """判断 chunk 是否属于目标 table 的表头或目标数据行。"""
    if chunk.parent_node_id not in closure_rows:
        return False
    rows = _chunk_table_row_indices(chunk)
    target_rows = closure_rows[chunk.parent_node_id]
    preceding_rows = {max(0, row - 1) for row in target_rows}
    return bool(rows & ({0, *preceding_rows} | set(target_rows)))


def _structural_stage_group(
    chunk: Chunk, request: StructuralSearchRequest
) -> tuple[str, ...] | None:
    """返回阶段标题的结构组，不依赖业务阶段名称。"""
    if request.answer_type not in {
        RequestedAnswerType.ENUMERATION,
        RequestedAnswerType.COUNT,
        RequestedAnswerType.ORDINAL_ITEM,
        RequestedAnswerType.PROCEDURE,
    } or not _STAGE_QUERY.search(
        " ".join((request.relation or "", request.query))
    ):
        return None
    if len(
        chunk.heading_path
    ) >= _FLOW_ARCHITECTURE_PATH_DEPTH and _FLOW_ARCHITECTURE_HEADING.match(
        chunk.heading_path[1]
    ):
        return chunk.heading_path[:1]
    for index, heading in enumerate(chunk.heading_path):
        if _NUMBERED_STAGE_HEADING.match(heading):
            return chunk.heading_path[: index + 1]
    return None


def _is_structural_stage_request(request: StructuralSearchRequest) -> bool:
    """判断是否需要按唯一文档的标题集合执行有界结构闭合。"""
    return request.answer_type in {
        RequestedAnswerType.ENUMERATION,
        RequestedAnswerType.COUNT,
        RequestedAnswerType.ORDINAL_ITEM,
        RequestedAnswerType.PROCEDURE,
    } and bool(
        _STAGE_QUERY.search(" ".join((request.relation or "", request.query)))
    )


def _chunk_source_ordinal(chunk: Chunk) -> int:
    """返回用于同一标题内选择首个正文的稳定来源顺序。"""
    return min(
        (
            span.source_anchor.ordinal
            for span in chunk.source_spans
            if span.is_citable and span.source_anchor is not None
        ),
        default=2**31 - 1,
    )


def _structural_score(
    chunk: Chunk,
    display_name: str,
    request: StructuralSearchRequest,
    *,
    document_target_match: bool = False,
    table_closure: bool = False,
) -> tuple[float, str] | None:
    """对 canonical 结构字段计分，并返回唯一原因码。"""
    heading = " / ".join(chunk.heading_path)
    body = chunk.citation_text
    searchable = " ".join((heading, chunk.lexical_text, body))
    source_label = " ".join((display_name, heading))
    source_match = _structural_source_contains(
        request.source_qualifier, source_label
    )
    if request.source_qualifier and not source_match:
        return None
    target_display = _structural_contains(request.target, display_name)
    target_document = target_display or document_target_match
    target_heading = _structural_contains(request.target, heading)
    normalized_target = normalize_section_heading_label(request.target or "")
    target_heading_exact = bool(normalized_target) and any(
        normalize_section_heading_label(item) == normalized_target
        for item in chunk.heading_path
    )
    target_body = _structural_contains(request.target, searchable)
    context_variants = context_label_variants(request.context_qualifier or "")
    context_heading = any(
        _structural_contains(variant, heading) for variant in context_variants
    )
    context_body = any(
        _structural_contains(variant, searchable)
        for variant in context_variants
    )
    relation_pattern = _STRUCTURAL_RELATION_ALIASES.get(request.answer_type)
    relation_heading = bool(
        relation_pattern and relation_pattern.search(heading)
    )
    relation_body = bool(
        relation_pattern and relation_pattern.search(searchable)
    )
    if request.relation:
        relation_heading = relation_heading or _structural_contains(
            request.relation, heading
        )
        relation_body = relation_body or _structural_contains(
            request.relation, searchable
        )
    if not any(
        (
            source_match,
            target_document,
            target_heading,
            target_body,
            context_heading,
            context_body,
            relation_heading,
            relation_body,
        )
    ):
        return None
    score = (
        8.0 * float(source_match)
        + 8.0 * float(target_document)
        + 7.0 * float(target_heading)
        + 12.0 * float(target_heading_exact)
        + 4.0 * float(target_body)
        + 10.0 * float(context_heading)
        + 7.0 * float(context_body)
        + 6.0 * float(relation_heading)
        + 3.0 * float(relation_body)
    )
    reason = "STRUCTURAL_FIELD_MATCH"
    stage_group = (
        _structural_stage_group(chunk, request)
        if document_target_match
        else None
    )
    if stage_group is not None:
        score += 18.0
        reason = "STRUCTURAL_STAGE_HEADING"
    elif (
        request.answer_type is RequestedAnswerType.DEFINITION
        and chunk.role.value == "table"
        and (target_body or table_closure)
    ):
        score += 10.0
        reason = "STRUCTURAL_GLOSSARY_ROW"
    elif (
        request.answer_type
        in {
            RequestedAnswerType.DUTIES,
            RequestedAnswerType.RESPONSIBLE_PARTY,
        }
        and chunk.role.value == "table"
        and (target_body or table_closure)
    ):
        score += 10.0
        reason = "STRUCTURAL_TABLE_ROW"
    elif (
        request.answer_type
        in {
            RequestedAnswerType.PURPOSE,
            RequestedAnswerType.SECTION_SUMMARY,
        }
        and relation_heading
        and (source_match or target_document or target_heading or target_body)
    ):
        score += 9.0
        reason = "STRUCTURAL_SECTION_HEADING_BODY"
    elif (
        request.answer_type
        in {
            RequestedAnswerType.ENUMERATION,
            RequestedAnswerType.COUNT,
            RequestedAnswerType.ORDINAL_ITEM,
            RequestedAnswerType.PROCEDURE,
        }
        and chunk.role.value == "list"
        and (source_match or target_document or target_heading or target_body)
    ):
        score += 9.0
        reason = "STRUCTURAL_CONTIGUOUS_LIST"
    elif target_heading:
        reason = "STRUCTURAL_HEADING_PATH"
    elif target_document or source_match:
        reason = "STRUCTURAL_DOCUMENT_NAME"
    elif chunk.parent_node_id or chunk.neighbor_group_id != "root":
        reason = "STRUCTURAL_PARENT_NEIGHBOR"
    return score, reason


__all__ = [
    "SqliteFtsStore",
    "build_fts_query",
    "build_fts_v2_query",
    "fts_table_for_revision",
    "normalize_identifier",
    "write_chunks_transaction",
]
