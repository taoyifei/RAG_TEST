"""湾事通文档相对路径的独立产品元数据存储。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from pydantic import Field

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.models.common import FrozenModel
from rag_app.core.models.management import Document
from rag_app.wanshitong.errors import AdminFacadeError


class WanshitongDocumentMetadata(FrozenModel):
    """不改变 Universal Document 身份的湾事通补充元数据。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    relative_path: str = Field(min_length=1, max_length=4096)
    department: str | None = Field(default=None, max_length=200)
    category_path: tuple[str, ...] = ()
    created_at: str
    updated_at: str


class WanshitongDocumentMetadataStore:
    """在同一 Universal SQLite 中读写湾事通产品元数据。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        self._connections = connections

    def get(self, document_id: str) -> WanshitongDocumentMetadata | None:
        """读取单个文档的产品元数据。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM wanshitong_document_metadata "
                "WHERE document_id=?",
                (document_id,),
            ).fetchone()
        return None if row is None else _decode(row)

    def list_for_scope(
        self, project_id: str, knowledge_base_id: str
    ) -> dict[str, WanshitongDocumentMetadata]:
        """按固定 Scope 返回以 Document ID 索引的元数据。"""
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM wanshitong_document_metadata "
                "WHERE project_id=? AND knowledge_base_id=?",
                (project_id, knowledge_base_id),
            ).fetchall()
        return {str(row["document_id"]): _decode(row) for row in rows}

    def bind(
        self,
        *,
        document: Document,
        relative_path: str,
        department: str | None = None,
        category_path: tuple[str, ...] = (),
    ) -> WanshitongDocumentMetadata:
        """幂等绑定相对路径，拒绝跨 Scope 或静默改写路径。"""
        now = datetime.now(UTC).isoformat()
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM wanshitong_document_metadata "
                "WHERE document_id=?",
                (document.document_id,),
            ).fetchone()
            if existing is not None:
                metadata = _decode(existing)
                if (
                    metadata.project_id != document.project_id
                    or metadata.knowledge_base_id
                    != document.knowledge_base_id
                    or metadata.relative_path != relative_path
                ):
                    raise AdminFacadeError(
                        "DOCUMENT_METADATA_CONFLICT",
                        "文档相对路径与既有上传记录不一致。",
                        status_code=409,
                        stage="wanshitong.document.metadata",
                    )
                return metadata
            connection.execute(
                "INSERT INTO wanshitong_document_metadata("
                "document_id, project_id, knowledge_base_id, relative_path, "
                "department, category_path_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document.document_id,
                    document.project_id,
                    document.knowledge_base_id,
                    relative_path,
                    department,
                    json.dumps(category_path, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        created = self.get(document.document_id)
        if created is None:
            raise RuntimeError("湾事通文档元数据写入后不可读取。")
        return created


def _decode(row: sqlite3.Row) -> WanshitongDocumentMetadata:
    """把 sqlite Row 解码成严格产品元数据。"""
    values = dict(row)
    try:
        category_path = tuple(json.loads(str(values["category_path_json"])))
    except (TypeError, ValueError):
        raise AdminFacadeError(
            "DOCUMENT_METADATA_INVALID",
            "文档分类元数据已损坏。",
            status_code=500,
            stage="wanshitong.document.metadata",
        ) from None
    return WanshitongDocumentMetadata(
        document_id=str(values["document_id"]),
        project_id=str(values["project_id"]),
        knowledge_base_id=str(values["knowledge_base_id"]),
        relative_path=str(values["relative_path"]),
        department=(
            None if values["department"] is None else str(values["department"])
        ),
        category_path=category_path,
        created_at=str(values["created_at"]),
        updated_at=str(values["updated_at"]),
    )


__all__ = [
    "WanshitongDocumentMetadata",
    "WanshitongDocumentMetadataStore",
]
