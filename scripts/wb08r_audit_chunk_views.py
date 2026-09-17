"""只读审计 Chunk V3 三种文本视图，不输出文档正文。"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag_app.core.models import Chunk

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ChunkView:
    """审计所需的最小 Chunk 文本与结构字段。"""

    chunk_id: str
    citation_text: str
    embedding_text: str
    lexical_text: str
    heading_path: Sequence[str]
    role: str
    section_id: str


def _normalized(text: str) -> str:
    """按检索视图的大小写和空白规则比较上下文标签。"""
    return _WHITESPACE.sub(
        " ", unicodedata.normalize("NFKC", text).casefold()
    ).strip()


def _contains(text: str, label: str) -> bool:
    """检查非空标签是否出现在文本中。"""
    normalized_label = _normalized(label)
    return bool(normalized_label) and normalized_label in _normalized(text)


def _has_heading_path(text: str, heading_path: Sequence[str]) -> bool:
    """要求每级非空标题均存在于目标视图。"""
    headings = tuple(heading for heading in heading_path if heading.strip())
    return bool(headings) and all(
        _contains(text, heading) for heading in headings
    )


def _has_value(metadata: Mapping[str, Any], *keys: str) -> bool:
    """只判断元数据字段是否非空，不返回元数据内容。"""
    return any(bool(metadata.get(key)) for key in keys)


def _embedding_prefix(embedding_text: str, citation_text: str) -> str:
    """排除正文，只检测生成的 embedding 上下文前缀。"""
    if embedding_text.endswith(citation_text):
        return embedding_text[: -len(citation_text)]
    return embedding_text.split("\n\n", maxsplit=1)[0]


def _duplicate_prefix(
    embedding_text: str,
    citation_text: str,
    title: str,
    heading_path: Sequence[str],
) -> bool:
    """识别前缀中重复的文档或章节标签。"""
    prefix = _normalized(_embedding_prefix(embedding_text, citation_text))
    labels = {label for label in (title, *heading_path) if label.strip()}
    return any(prefix.count(_normalized(label)) > 1 for label in labels)


def audit_view(
    view: ChunkView,
    document_title: str,
    document_metadata: Mapping[str, Any],
) -> dict[str, str | bool | int | float]:
    """生成一条无正文、无标题及元数据值的审计记录。

    Args:
        view: 当前 Chunk 的最小文本与结构字段。
        document_title: 已存储的文档展示标题。
        document_metadata: 已存储的文档元数据。

    Returns:
        仅包含 ID、布尔值、长度、比例及结构坐标的记录。

    Raises:
        ValueError: citation 文本为空，无法计算长度比例。

    """
    citation_chars = len(view.citation_text)
    if citation_chars == 0:
        raise ValueError("citation_text 为空，无法计算长度比例。")
    return {
        "chunk_id": view.chunk_id,
        "embedding_has_document_title": _contains(
            view.embedding_text, document_title
        ),
        "embedding_has_heading_path": _has_heading_path(
            view.embedding_text, view.heading_path
        ),
        "lexical_has_document_title": _contains(
            view.lexical_text, document_title
        ),
        "lexical_has_heading_path": _has_heading_path(
            view.lexical_text, view.heading_path
        ),
        "department_present": _has_value(
            document_metadata, "department_name", "department_key"
        ),
        "category_present": _has_value(document_metadata, "category_path"),
        "citation_chars": citation_chars,
        "embedding_chars": len(view.embedding_text),
        "lexical_chars": len(view.lexical_text),
        "embedding_citation_ratio": round(
            len(view.embedding_text) / citation_chars, 3
        ),
        "lexical_citation_ratio": round(
            len(view.lexical_text) / citation_chars, 3
        ),
        "duplicate_prefix_detected": _duplicate_prefix(
            view.embedding_text,
            view.citation_text,
            document_title,
            view.heading_path,
        ),
        "role": view.role,
        "section_id": view.section_id,
    }


def audit_chunks(
    chunks: Sequence[Chunk],
    document_titles: Mapping[str, str],
    document_metadata: Mapping[str, Mapping[str, Any]],
) -> Iterator[dict[str, str | bool | int | float]]:
    """审计内存中的测试 Chunk 集合。

    Args:
        chunks: 待审计的 Chunk V3 集合。
        document_titles: document_id 到展示标题的映射。
        document_metadata: document_id 到元数据的映射。

    Returns:
        迭代产生与 SQLite 审计相同的脱敏记录。

    """
    for chunk in chunks:
        document_id = chunk.version.document_id
        yield audit_view(
            ChunkView(
                chunk_id=chunk.chunk_id,
                citation_text=chunk.citation_text,
                embedding_text=chunk.embedding_text,
                lexical_text=chunk.lexical_text,
                heading_path=chunk.heading_path,
                role=chunk.role.value,
                section_id=chunk.section_id,
            ),
            document_titles.get(document_id, ""),
            document_metadata.get(document_id, {}),
        )


def _revision_id(
    connection: sqlite3.Connection,
    *,
    revision_id: str | None,
    knowledge_base_id: str | None,
) -> str:
    """解析唯一活动 Revision，或验证显式指定的 Revision。"""
    if revision_id is not None:
        row = connection.execute(
            "SELECT knowledge_base_id FROM index_revisions "
            "WHERE index_revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ValueError("指定的 Index Revision 不存在。")
        if (
            knowledge_base_id is not None
            and row["knowledge_base_id"] != knowledge_base_id
        ):
            raise ValueError("Index Revision 不属于指定知识库。")
        return revision_id

    query = (
        "SELECT active_revision_id FROM knowledge_bases "
        "WHERE active_revision_id IS NOT NULL AND deleted_at IS NULL"
    )
    parameters: tuple[str, ...] = ()
    if knowledge_base_id is not None:
        query += " AND knowledge_base_id=?"
        parameters = (knowledge_base_id,)
    rows = connection.execute(
        f"{query} ORDER BY knowledge_base_id", parameters
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            "活动 Index Revision 不唯一；请指定知识库或 Revision。"
        )
    return str(rows[0]["active_revision_id"])


def audit_sqlite(
    database_path: Path,
    *,
    revision_id: str | None = None,
    knowledge_base_id: str | None = None,
) -> Iterator[dict[str, str | bool | int | float]]:
    """以 SQLite 只读连接流式审计一个 Index Revision。

    Args:
        database_path: 已存在的 Universal SQLite 数据库路径。
        revision_id: 指定 Revision；省略时使用活动 Revision。
        knowledge_base_id: 多知识库时限定活动 Revision 的知识库。

    Returns:
        迭代产生仅含安全审计字段的 NDJSON 兼容记录。

    """
    database_uri = database_path.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        selected_revision = _revision_id(
            connection,
            revision_id=revision_id,
            knowledge_base_id=knowledge_base_id,
        )
        rows = connection.execute(
            "SELECT c.chunk_id, c.citation_text, c.embedding_text, "
            "c.lexical_text, c.heading_path_json, c.role, c.section_id, "
            "c.metadata_json AS chunk_metadata_json, d.* "
            "FROM chunks c JOIN documents d ON d.document_id=c.document_id "
            "JOIN index_revisions r ON r.index_revision_id=c.revision_id "
            "AND r.knowledge_base_id=d.knowledge_base_id "
            "WHERE c.revision_id=? ORDER BY c.row_id",
            (selected_revision,),
        )
        for row in rows:
            try:
                metadata_json = row["metadata_json"]
            except IndexError:
                metadata_json = row["chunk_metadata_json"]
            yield audit_view(
                ChunkView(
                    chunk_id=str(row["chunk_id"]),
                    citation_text=str(row["citation_text"]),
                    embedding_text=str(row["embedding_text"]),
                    lexical_text=str(row["lexical_text"]),
                    heading_path=json.loads(str(row["heading_path_json"])),
                    role=str(row["role"]),
                    section_id=str(row["section_id"]),
                ),
                str(row["display_name"]),
                json.loads(str(metadata_json)),
            )


def main() -> int:
    """读取指定数据库并将脱敏审计记录输出为 NDJSON。

    Args:
        无参数；命令行选项从当前进程读取。

    Returns:
        成功输出返回零；审计失败返回一。

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--revision-id")
    parser.add_argument("--knowledge-base-id")
    arguments = parser.parse_args()
    try:
        for row in audit_sqlite(
            arguments.database,
            revision_id=arguments.revision_id,
            knowledge_base_id=arguments.knowledge_base_id,
        ):
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    except (OSError, sqlite3.Error, ValueError) as error:
        print(f"审计失败：{type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
