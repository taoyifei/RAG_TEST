"""湾事通文档分类元数据、路径解析与持久化。"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import unquote

from pydantic import Field, ValidationError

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.identifiers import canonical_json
from rag_app.core.models.common import (
    FrozenModel,
    JsonObject,
    freeze_json_object,
)
from rag_app.core.models.management import Document, Job
from rag_app.wanshitong.errors import AdminFacadeError

ALL_INTERNAL = "all_internal"
DOCUMENT_METADATA_REVISION = "wanshitong-document-metadata-v1"
DOCX_ONLY_MESSAGE = (
    "当前湾事通 Demo 仅开放 DOCX 文档。PDF、旧 DOC、Excel 和 ZIP "
    "将在后续版本接入。"
)
UNCATEGORIZED_DEPARTMENT = "未分类"

_MAX_RELATIVE_PATH_CHARS = 4096
_MAX_PATH_SEGMENT_CHARS = 255
_MAX_CATEGORY_DEPTH = 16
_MAX_TOPIC_KEYS = 32
_MAX_SECURITY_PATH_FORMS = 64
_MIN_DOUBLE_HYPHEN_PARTS = 2
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_SLUG_SEPARATORS = re.compile(r"[^0-9A-Za-z\u3400-\u9fff]+")


class WanshitongUploadMetadata(FrozenModel):
    """单个 DOCX 上传可由管理员显式覆盖的分类字段。"""

    source_relative_path: str = Field(min_length=1, max_length=8192)
    department_name: str | None = Field(default=None, max_length=200)
    category_path: tuple[str, ...] | None = Field(
        default=None, max_length=_MAX_CATEGORY_DEPTH
    )
    document_title: str | None = Field(default=None, max_length=512)
    topic_keys: tuple[str, ...] = Field(default=(), max_length=_MAX_TOPIC_KEYS)


class DocumentMetadataValues(FrozenModel):
    """不含数据库身份和时间戳的规范化文档元数据。"""

    department_key: str = Field(min_length=1, max_length=240)
    department_name: str = Field(min_length=1, max_length=200)
    category_path: tuple[str, ...] = Field(max_length=_MAX_CATEGORY_DEPTH)
    document_title: str = Field(min_length=1, max_length=512)
    source_relative_path: str = Field(
        min_length=1, max_length=_MAX_RELATIVE_PATH_CHARS
    )
    topic_keys: tuple[str, ...] = Field(max_length=_MAX_TOPIC_KEYS)
    visibility_scope: str = Field(
        default=ALL_INTERNAL, pattern=r"^all_internal$"
    )
    allowed_roles: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    metadata_revision: str = Field(
        default=DOCUMENT_METADATA_REVISION,
        pattern=r"^wanshitong-document-metadata-v1$",
    )

    def to_index_metadata(
        self, *, created_at: str, updated_at: str
    ) -> JsonObject:
        """返回进入 Universal 数据面的有限 JSON 元数据。"""
        return freeze_json_object(
            {
                **self.model_dump(mode="json"),
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )


class WanshitongDocumentMetadata(DocumentMetadataValues):
    """绑定逻辑文档身份且可持久化的湾事通元数据。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    created_at: str
    updated_at: str

    @property
    def relative_path(self) -> str:
        """返回 WB-05 管理 API 的兼容字段。"""
        return self.source_relative_path

    @property
    def department(self) -> str:
        """返回 WB-05 管理 API 的兼容字段。"""
        return self.department_name

    def index_metadata(self) -> JsonObject:
        """返回进入 Job、IR、Chunk 与索引的分类元数据。"""
        return _values(self).to_index_metadata(
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


def normalize_source_relative_path(value: str) -> tuple[str, str]:
    """规范化并验证浏览器目录上传的相对 DOCX 路径。

    Args:
        value: 浏览器提供的相对路径或单文件 basename。

    Returns:
        NFKC 规范化的相对路径与 basename。

    Raises:
        AdminFacadeError: 路径包含绝对位置、穿越或非法字符。

    """
    if not isinstance(value, str):
        raise _relative_path_error()
    original = value.strip()
    if not original or original.startswith(("\\\\", "//")):
        raise _relative_path_error()
    normalized = unicodedata.normalize("NFKC", original).replace("\\", "/")
    if not normalized or len(normalized) > _MAX_RELATIVE_PATH_CHARS:
        raise _relative_path_error()
    segments = tuple(segment.strip() for segment in normalized.split("/"))
    normalized = "/".join(segments)
    for candidate in _security_path_forms(normalized):
        _validate_path_form(candidate)
    if any(
        not segment
        or len(segment) > _MAX_PATH_SEGMENT_CHARS
        or any(_is_control(char) for char in segment)
        for segment in segments
    ):
        raise _relative_path_error()
    path = PurePosixPath(normalized)
    if path.is_absolute() or path.as_posix() != normalized:
        raise _relative_path_error()
    display_name = segments[-1]
    if not display_name.casefold().endswith(".docx"):
        raise AdminFacadeError(
            "DOCX_ONLY",
            DOCX_ONLY_MESSAGE,
            status_code=415,
            stage="wanshitong.document.type",
        )
    return normalized, display_name


def resolve_document_metadata(
    upload: WanshitongUploadMetadata,
) -> DocumentMetadataValues:
    """按显式值、目录、双横线和未分类的优先级解析元数据。"""
    source_relative_path, basename = normalize_source_relative_path(
        upload.source_relative_path
    )
    directories = source_relative_path.split("/")[:-1]
    stem = basename[:-5]
    inferred_department, inferred_categories, inferred_title = (
        _infer_path_metadata(directories, stem)
    )
    department_name = (
        _explicit_text(upload.department_name, "department_name")
        or inferred_department
    )
    category_path = (
        inferred_categories
        if upload.category_path is None
        else tuple(
            _required_segment(value, "category_path")
            for value in upload.category_path
        )
    )
    document_title = (
        _explicit_text(upload.document_title, "document_title")
        or inferred_title
    )
    topic_keys = tuple(
        dict.fromkeys(
            _required_segment(value, "topic_keys").casefold()
            for value in upload.topic_keys
        )
    )
    return DocumentMetadataValues(
        department_key=stable_department_key(department_name),
        department_name=department_name,
        category_path=category_path,
        document_title=html.unescape(document_title),
        source_relative_path=source_relative_path,
        topic_keys=topic_keys,
    )


def stable_department_key(department_name: str) -> str:
    """生成带可读前缀且跨进程稳定的部门 Key。"""
    normalized = _required_segment(department_name, "department_name")
    folded = normalized.casefold()
    readable = _SLUG_SEPARATORS.sub("-", folded).strip("-")
    if not readable:
        readable = "department"
    readable = readable[:200].rstrip("-") or "department"
    digest = hashlib.sha256(folded.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def build_document_metadata(  # noqa: PLR0913
    *,
    document_id: str,
    project_id: str,
    knowledge_base_id: str,
    values: DocumentMetadataValues,
    existing: WanshitongDocumentMetadata | None = None,
    now: str | None = None,
) -> WanshitongDocumentMetadata:
    """为规范化字段补齐逻辑身份与稳定时间戳。"""
    timestamp = now or datetime.now(UTC).isoformat()
    created_at = timestamp if existing is None else existing.created_at
    unchanged = existing is not None and _values(existing) == values
    return WanshitongDocumentMetadata(
        **values.model_dump(),
        document_id=document_id,
        project_id=project_id,
        knowledge_base_id=knowledge_base_id,
        created_at=created_at,
        updated_at=existing.updated_at if unchanged else timestamp,
    )


def parse_upload_metadata(
    *,
    relative_path: str | None,
    source_relative_path: str | None,
    metadata_json: str | None,
) -> WanshitongUploadMetadata:
    """解码兼容路径参数或完整的逐文件 JSON 上传合同。"""
    supplied_path = source_relative_path or relative_path
    if (
        source_relative_path is not None
        and relative_path is not None
        and normalize_source_relative_path(source_relative_path)[0]
        != normalize_source_relative_path(relative_path)[0]
    ):
        raise AdminFacadeError(
            "DOCUMENT_METADATA_CONFLICT",
            "上传请求包含不一致的 source_relative_path。",
            stage="wanshitong.document.metadata",
        )
    if metadata_json is None:
        if supplied_path is None:
            raise _relative_path_error()
        return WanshitongUploadMetadata(source_relative_path=supplied_path)
    try:
        parsed = WanshitongUploadMetadata.model_validate_json(metadata_json)
    except ValidationError as error:
        raise AdminFacadeError(
            "INVALID_DOCUMENT_METADATA",
            "文档分类元数据不符合上传合同。",
            stage="wanshitong.document.metadata",
        ) from error
    if supplied_path is not None and (
        normalize_source_relative_path(supplied_path)[0]
        != normalize_source_relative_path(parsed.source_relative_path)[0]
    ):
        raise AdminFacadeError(
            "DOCUMENT_METADATA_CONFLICT",
            "上传 JSON 与路径参数不一致。",
            stage="wanshitong.document.metadata",
        )
    return parsed


class WanshitongDocumentMetadataStore:
    """在 Universal SQLite 内保存逻辑文档和版本元数据快照。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        self._connections = connections

    def get(self, document_id: str) -> WanshitongDocumentMetadata | None:
        """读取单个逻辑文档的最新分类元数据。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM wanshitong_document_metadata "
                "WHERE document_id=?",
                (document_id,),
            ).fetchone()
        return None if row is None else _decode(row)

    def get_version_snapshot(
        self, document_version_id: str
    ) -> WanshitongDocumentMetadata | None:
        """读取不可变 DocumentVersion 的提交时分类快照。"""
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM "
                "wanshitong_document_version_metadata "
                "WHERE document_version_id=?",
                (document_version_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return WanshitongDocumentMetadata.model_validate_json(
                str(row["metadata_json"])
            )
        except ValidationError:
            raise _metadata_invalid() from None

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

    def synchronize_legacy_rows(self) -> int:
        """把 WB-05 登记幂等升级到通用文档 metadata 与版本快照。

        Returns:
            本次规范化并同步的湾事通逻辑文档数。

        """
        synchronized = 0
        with self._connections.transaction(write=True) as connection:
            rows = connection.execute(
                "SELECT metadata.*, documents.current_version_id, "
                "documents.metadata_json AS document_metadata_json, "
                "(SELECT jobs.job_id FROM ingestion_jobs jobs "
                "WHERE jobs.document_id=metadata.document_id AND "
                "jobs.document_version_id=documents.current_version_id "
                "ORDER BY jobs.updated_at DESC, jobs.job_id DESC LIMIT 1) "
                "AS current_job_id FROM wanshitong_document_metadata "
                "metadata JOIN documents ON "
                "documents.document_id=metadata.document_id "
                "ORDER BY metadata.document_id"
            ).fetchall()
            for row in rows:
                metadata = _decode(row)
                connection.execute(
                    "UPDATE wanshitong_document_metadata SET "
                    "relative_path=?, department=?, category_path_json=?, "
                    "department_key=?, department_name=?, document_title=?, "
                    "source_relative_path=?, topic_keys_json=?, "
                    "visibility_scope=?, allowed_roles_json=?, "
                    "allowed_groups_json=?, metadata_revision=? "
                    "WHERE document_id=?",
                    (
                        metadata.source_relative_path,
                        metadata.department_name,
                        json.dumps(metadata.category_path, ensure_ascii=False),
                        metadata.department_key,
                        metadata.department_name,
                        metadata.document_title,
                        metadata.source_relative_path,
                        json.dumps(metadata.topic_keys, ensure_ascii=False),
                        metadata.visibility_scope,
                        json.dumps(metadata.allowed_roles, ensure_ascii=False),
                        json.dumps(metadata.allowed_groups, ensure_ascii=False),
                        metadata.metadata_revision,
                        metadata.document_id,
                    ),
                )
                document_metadata = json.loads(
                    str(row["document_metadata_json"])
                )
                if not isinstance(document_metadata, dict):
                    raise _metadata_invalid()
                document_metadata.update(dict(metadata.index_metadata()))
                serialized = canonical_json(document_metadata)
                connection.execute(
                    "UPDATE documents SET metadata_json=? WHERE document_id=?",
                    (serialized, metadata.document_id),
                )
                version_id = row["current_version_id"]
                job_id = row["current_job_id"]
                if version_id is not None and job_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO "
                        "wanshitong_document_version_metadata("
                        "document_version_id, document_id, job_id, "
                        "metadata_json, metadata_revision, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            str(version_id),
                            metadata.document_id,
                            str(job_id),
                            canonical_json(metadata.model_dump(mode="json")),
                            metadata.metadata_revision,
                            metadata.updated_at,
                        ),
                    )
                synchronized += 1
        return synchronized

    def bind(
        self,
        *,
        document: Document,
        metadata: WanshitongDocumentMetadata,
        job: Job,
    ) -> WanshitongDocumentMetadata:
        """幂等更新逻辑登记，并冻结当前版本和 Job 的提交快照。"""
        expected_scope = (
            document.document_id,
            document.project_id,
            document.knowledge_base_id,
        )
        observed_scope = (
            metadata.document_id,
            metadata.project_id,
            metadata.knowledge_base_id,
        )
        if (
            expected_scope != observed_scope
            or job.document_id != document.document_id
        ):
            raise AdminFacadeError(
                "DOCUMENT_METADATA_CONFLICT",
                "分类元数据与文档 Scope 不一致。",
                status_code=409,
                stage="wanshitong.document.metadata",
            )
        if job.document_version_id is None:
            raise AdminFacadeError(
                "DOCUMENT_METADATA_CONFLICT",
                "文档 Job 缺少版本身份。",
                status_code=409,
                stage="wanshitong.document.metadata",
            )
        serialized = canonical_json(metadata.model_dump(mode="json"))
        with self._connections.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT project_id, knowledge_base_id FROM "
                "wanshitong_document_metadata WHERE document_id=?",
                (document.document_id,),
            ).fetchone()
            if existing is not None and (
                str(existing["project_id"]),
                str(existing["knowledge_base_id"]),
            ) != (document.project_id, document.knowledge_base_id):
                raise AdminFacadeError(
                    "DOCUMENT_METADATA_CONFLICT",
                    "文档分类元数据已绑定其他 Scope。",
                    status_code=409,
                    stage="wanshitong.document.metadata",
                )
            connection.execute(
                "INSERT INTO wanshitong_document_metadata("
                "document_id, project_id, knowledge_base_id, relative_path, "
                "department, category_path_json, department_key, "
                "department_name, document_title, source_relative_path, "
                "topic_keys_json, visibility_scope, allowed_roles_json, "
                "allowed_groups_json, metadata_revision, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?) ON CONFLICT(document_id) DO UPDATE SET "
                "relative_path=excluded.relative_path, "
                "department=excluded.department, "
                "category_path_json=excluded.category_path_json, "
                "department_key=excluded.department_key, "
                "department_name=excluded.department_name, "
                "document_title=excluded.document_title, "
                "source_relative_path=excluded.source_relative_path, "
                "topic_keys_json=excluded.topic_keys_json, "
                "visibility_scope=excluded.visibility_scope, "
                "allowed_roles_json=excluded.allowed_roles_json, "
                "allowed_groups_json=excluded.allowed_groups_json, "
                "metadata_revision=excluded.metadata_revision, "
                "updated_at=excluded.updated_at",
                _database_values(metadata),
            )
            snapshot = connection.execute(
                "SELECT metadata_json FROM "
                "wanshitong_document_version_metadata "
                "WHERE document_version_id=?",
                (job.document_version_id,),
            ).fetchone()
            if (
                snapshot is not None
                and str(snapshot["metadata_json"]) != serialized
            ):
                raise AdminFacadeError(
                    "DOCUMENT_METADATA_CONFLICT",
                    "同一文档版本已绑定不同分类快照。",
                    status_code=409,
                    stage="wanshitong.document.metadata",
                )
            connection.execute(
                "INSERT OR IGNORE INTO wanshitong_document_version_metadata("
                "document_version_id, document_id, job_id, metadata_json, "
                "metadata_revision, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job.document_version_id,
                    document.document_id,
                    job.job_id,
                    serialized,
                    metadata.metadata_revision,
                    metadata.updated_at,
                ),
            )
        created = self.get(document.document_id)
        if created is None:
            raise RuntimeError("湾事通文档元数据写入后不可读取。")
        return created


def _infer_path_metadata(
    directories: list[str], stem: str
) -> tuple[str, tuple[str, ...], str]:
    if directories:
        return directories[0], tuple(directories[1:]), html.unescape(stem)
    parts = tuple(part.strip() for part in stem.split("--"))
    if len(parts) >= _MIN_DOUBLE_HYPHEN_PARTS and all(parts):
        return parts[0], parts[1:-1], html.unescape(parts[-1])
    return UNCATEGORIZED_DEPARTMENT, (), html.unescape(stem)


def _explicit_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_segment(value, field_name)


def _required_segment(value: str, field_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    if (
        not normalized
        or len(normalized) > _MAX_PATH_SEGMENT_CHARS
        or "/" in normalized
        or "\\" in normalized
        or any(_is_control(char) for char in normalized)
    ):
        raise AdminFacadeError(
            "INVALID_DOCUMENT_METADATA",
            f"{field_name} 包含无效值。",
            stage="wanshitong.document.metadata",
        )
    return normalized


def _security_path_forms(value: str) -> tuple[str, ...]:
    forms: list[str] = []
    pending = [value]
    seen: set[str] = set()
    while pending:
        if len(seen) >= _MAX_SECURITY_PATH_FORMS:
            raise _relative_path_error()
        candidate = pending.pop()
        if candidate in seen:
            continue
        seen.add(candidate)
        forms.append(candidate)
        pending.extend(
            decoded
            for decoded in (unquote(candidate), html.unescape(candidate))
            if decoded not in seen
        )
    return tuple(forms)


def _validate_path_form(value: str) -> None:
    normalized = unicodedata.normalize("NFKC", value).replace("\\", "/")
    if (
        normalized.startswith(("/", "//"))
        or _WINDOWS_DRIVE.match(normalized)
        or any(segment in {"", ".", ".."} for segment in normalized.split("/"))
        or any(_is_control(char) for char in normalized)
    ):
        raise _relative_path_error()


def _is_control(char: str) -> bool:
    return char == "\x00" or unicodedata.category(char).startswith("C")


def _values(metadata: WanshitongDocumentMetadata) -> DocumentMetadataValues:
    return DocumentMetadataValues(
        **metadata.model_dump(
            include={
                "department_key",
                "department_name",
                "category_path",
                "document_title",
                "source_relative_path",
                "topic_keys",
                "visibility_scope",
                "allowed_roles",
                "allowed_groups",
                "metadata_revision",
            }
        )
    )


def _database_values(
    metadata: WanshitongDocumentMetadata,
) -> tuple[object, ...]:
    return (
        metadata.document_id,
        metadata.project_id,
        metadata.knowledge_base_id,
        metadata.source_relative_path,
        metadata.department_name,
        json.dumps(metadata.category_path, ensure_ascii=False),
        metadata.department_key,
        metadata.department_name,
        metadata.document_title,
        metadata.source_relative_path,
        json.dumps(metadata.topic_keys, ensure_ascii=False),
        metadata.visibility_scope,
        json.dumps(metadata.allowed_roles, ensure_ascii=False),
        json.dumps(metadata.allowed_groups, ensure_ascii=False),
        metadata.metadata_revision,
        metadata.created_at,
        metadata.updated_at,
    )


def _decode(row: sqlite3.Row) -> WanshitongDocumentMetadata:
    values = dict(row)
    source_relative_path = str(
        values.get("source_relative_path") or values["relative_path"]
    )
    try:
        category_path = tuple(
            json.loads(str(values.get("category_path_json") or "[]"))
        )
        topic_keys = tuple(
            json.loads(str(values.get("topic_keys_json") or "[]"))
        )
        allowed_roles = tuple(
            json.loads(str(values.get("allowed_roles_json") or "[]"))
        )
        allowed_groups = tuple(
            json.loads(str(values.get("allowed_groups_json") or "[]"))
        )
        fallback = resolve_document_metadata(
            WanshitongUploadMetadata(
                source_relative_path=source_relative_path,
                department_name=(
                    values.get("department_name") or values.get("department")
                ),
                category_path=category_path,
                document_title=values.get("document_title"),
                topic_keys=topic_keys,
            )
        )
        resolved = fallback.model_dump()
        resolved.update(
            department_key=str(
                values.get("department_key") or fallback.department_key
            ),
            visibility_scope=str(
                values.get("visibility_scope") or ALL_INTERNAL
            ),
            allowed_roles=allowed_roles,
            allowed_groups=allowed_groups,
            metadata_revision=str(
                values.get("metadata_revision") or DOCUMENT_METADATA_REVISION
            ),
        )
        return WanshitongDocumentMetadata(
            **resolved,
            document_id=str(values["document_id"]),
            project_id=str(values["project_id"]),
            knowledge_base_id=str(values["knowledge_base_id"]),
            created_at=str(values["created_at"]),
            updated_at=str(values["updated_at"]),
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        raise _metadata_invalid() from None


def _relative_path_error() -> AdminFacadeError:
    return AdminFacadeError(
        "INVALID_RELATIVE_PATH",
        "文档相对路径无效，仅允许安全的目录相对路径。",
        stage="wanshitong.document.path",
    )


def _metadata_invalid() -> AdminFacadeError:
    return AdminFacadeError(
        "DOCUMENT_METADATA_INVALID",
        "文档分类元数据已损坏。",
        status_code=500,
        stage="wanshitong.document.metadata",
    )


__all__ = [
    "ALL_INTERNAL",
    "DOCUMENT_METADATA_REVISION",
    "DOCX_ONLY_MESSAGE",
    "DocumentMetadataValues",
    "WanshitongDocumentMetadata",
    "WanshitongDocumentMetadataStore",
    "WanshitongUploadMetadata",
    "build_document_metadata",
    "normalize_source_relative_path",
    "parse_upload_metadata",
    "resolve_document_metadata",
    "stable_department_key",
]
