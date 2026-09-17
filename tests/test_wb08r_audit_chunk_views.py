"""Chunk V3 三视图审计脚本的脱敏与只读回归测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rag_app.core.models import Chunk, ChunkRole
from rag_app.core.models.document import DocumentVersionRef
from scripts.wb08r_audit_chunk_views import (
    ChunkView,
    audit_chunks,
    audit_sqlite,
    audit_view,
)


def test_audit_chunks_reports_context_without_leaking_text() -> None:
    chunk = Chunk.model_construct(
        chunk_id="chunk_" + "a" * 32,
        version=DocumentVersionRef.model_construct(
            document_id="doc_" + "b" * 32
        ),
        citation_text="敏感正文",
        embedding_text="文档：甲制度\n位置：一级 > 二级\n\n敏感正文",
        lexical_text="文档:甲制度 位置:一级 > 二级 敏感正文",
        heading_path=("一级", "二级"),
        role=ChunkRole.LIST,
        section_id="section-one",
    )

    rows = list(
        audit_chunks(
            (chunk,),
            {chunk.version.document_id: "甲制度"},
            {
                chunk.version.document_id: {
                    "department_name": "内部部门",
                    "category_path": ["内部分类"],
                }
            },
        )
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["embedding_has_document_title"] is True
    assert row["embedding_has_heading_path"] is True
    assert row["lexical_has_document_title"] is True
    assert row["lexical_has_heading_path"] is True
    assert row["department_present"] is True
    assert row["category_present"] is True
    assert row["duplicate_prefix_detected"] is False
    assert row["role"] == "list"
    assert row["section_id"] == "section-one"
    serialized = json.dumps(row, ensure_ascii=False)
    for sensitive in ("敏感正文", "甲制度", "内部部门", "内部分类"):
        assert sensitive not in serialized


def test_duplicate_prefix_detected_only_before_citation() -> None:
    single = audit_view(
        ChunkView(
            chunk_id="chunk_" + "a" * 32,
            citation_text="标题词在正文中再次出现",
            embedding_text="文档：标题词\n\n标题词在正文中再次出现",
            lexical_text="标题词在正文中再次出现",
            heading_path=(),
            role="text",
            section_id="root",
        ),
        "标题词",
        {},
    )
    repeated = audit_view(
        ChunkView(
            chunk_id="chunk_" + "a" * 32,
            citation_text="标题词在正文中再次出现",
            embedding_text="文档：标题词\n标题：标题词\n\n标题词在正文中再次出现",
            lexical_text="标题词在正文中再次出现",
            heading_path=(),
            role="text",
            section_id="root",
        ),
        "标题词",
        {},
    )

    assert single["duplicate_prefix_detected"] is False
    assert repeated["duplicate_prefix_detected"] is True
    assert single["embedding_citation_ratio"] > 1.0


def _create_fixture_database(path: Path) -> tuple[str, str]:
    active_revision = "irev_" + "a" * 32
    old_revision = "irev_" + "b" * 32
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE knowledge_bases("
            "knowledge_base_id TEXT, active_revision_id TEXT, "
            "deleted_at TEXT);"
            "CREATE TABLE index_revisions("
            "index_revision_id TEXT, knowledge_base_id TEXT);"
            "CREATE TABLE documents("
            "document_id TEXT, knowledge_base_id TEXT, display_name TEXT, "
            "metadata_json TEXT);"
            "CREATE TABLE chunks("
            "row_id INTEGER PRIMARY KEY, revision_id TEXT, chunk_id TEXT, "
            "document_id TEXT, citation_text TEXT, embedding_text TEXT, "
            "lexical_text TEXT, heading_path_json TEXT, role TEXT, "
            "section_id TEXT, metadata_json TEXT);"
        )
        connection.execute(
            "INSERT INTO knowledge_bases VALUES (?, ?, NULL)",
            ("kb_test", active_revision),
        )
        connection.executemany(
            "INSERT INTO index_revisions VALUES (?, ?)",
            ((active_revision, "kb_test"), (old_revision, "kb_test")),
        )
        connection.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?)",
            (
                "doc_test",
                "kb_test",
                "保密文档名",
                json.dumps({"department_name": "保密部门"}),
            ),
        )
        connection.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    1,
                    active_revision,
                    "chunk_active",
                    "doc_test",
                    "敏感内容",
                    "文档：保密文档名\n位置：章节\n\n敏感内容",
                    "文档:保密文档名 位置:章节 敏感内容",
                    '["章节"]',
                    "text",
                    "section-one",
                    "{}",
                ),
                (
                    2,
                    old_revision,
                    "chunk_old",
                    "doc_test",
                    "旧敏感内容",
                    "文档：保密文档名\n\n旧敏感内容",
                    "文档:保密文档名 旧敏感内容",
                    "[]",
                    "table",
                    "section-old",
                    "{}",
                ),
            ),
        )
    return active_revision, old_revision


def test_audit_sqlite_selects_active_or_specified_revision_read_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "views.sqlite3"
    active_revision, old_revision = _create_fixture_database(database)
    before = database.read_bytes()

    active = list(audit_sqlite(database))
    old = list(audit_sqlite(database, revision_id=old_revision))

    assert database.read_bytes() == before
    assert [row["chunk_id"] for row in active] == ["chunk_active"]
    assert [row["chunk_id"] for row in old] == ["chunk_old"]
    assert active[0]["embedding_has_heading_path"] is True
    assert old[0]["embedding_has_heading_path"] is False
    assert active[0]["department_present"] is True
    assert active[0]["category_present"] is False
    assert "敏感内容" not in json.dumps(active, ensure_ascii=False)
    with pytest.raises(ValueError, match="不属于指定知识库"):
        list(
            audit_sqlite(
                database,
                revision_id=active_revision,
                knowledge_base_id="kb_other",
            )
        )


def test_audit_sqlite_uses_chunk_metadata_for_older_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "old-views.sqlite3"
    _create_fixture_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE chunks SET metadata_json=?",
            (json.dumps({"category_path": ["内部分类"]}),),
        )
        connection.execute("ALTER TABLE documents DROP COLUMN metadata_json")

    rows = list(audit_sqlite(database))

    assert len(rows) == 1
    assert rows[0]["category_present"] is True
    assert rows[0]["department_present"] is False
