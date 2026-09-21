"""构建并持久化与活动索引版本绑定的部门 Profile。"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, ValidationError, model_validator

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.identifiers import canonical_json, canonical_sha256
from rag_app.core.models.common import FrozenModel

DEPARTMENT_PROFILE_SCHEMA_REVISION = "wanshitong-department-profile-v1"
UNKNOWN_DEPARTMENT_KEY = "UNKNOWN"
UNKNOWN_DEPARTMENT_NAME = "UNKNOWN"

_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SHA256_IDENTITY_LENGTH = 71
_MAX_PROFILE_DOCUMENTS = 10_000


class DepartmentEmbeddingIdentity(FrozenModel):
    """可与查询根向量逐字段比对的向量空间身份。"""

    slot_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    provider_id: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=500)
    vector_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    dimension: int = Field(gt=0)
    normalization: str = Field(min_length=1, max_length=100)
    adapter_revision: str = Field(min_length=1, max_length=80)

    @property
    def vector_space_identity(self) -> str:
        """返回与 Core slot 相同形状的向量空间身份。"""
        return ":".join(
            (
                self.slot_id,
                self.provider_id,
                self.model,
                str(self.dimension),
                self.normalization,
                self.adapter_revision,
            )
        )


class DepartmentProfileSourceDocument(FrozenModel):
    """活动 Index Revision 中构建部门 Profile 所需的最小元数据。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    document_version_id: str = Field(pattern=r"^dver_[0-9a-f]{32}$")
    department_key: str | None = Field(default=None, max_length=240)
    department_name: str | None = Field(default=None, max_length=200)
    category_path: tuple[str, ...] = Field(default=(), max_length=16)
    document_title: str = Field(min_length=1, max_length=512)
    representative_headings: tuple[str, ...] = Field(default=(), max_length=64)
    topic_keys: tuple[str, ...] = Field(default=(), max_length=32)
    aliases: tuple[str, ...] = Field(default=(), max_length=32)
    metadata_revision: str = Field(min_length=1, max_length=100)


class DepartmentProfileSourceSnapshot(FrozenModel):
    """一次只读事务冻结的活动索引与部门元数据。"""

    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    scope_id: str = Field(pattern=_SHA256_PATTERN)
    index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    metadata_revision: str = Field(pattern=_SHA256_PATTERN)
    documents: tuple[DepartmentProfileSourceDocument, ...] = Field(
        max_length=_MAX_PROFILE_DOCUMENTS
    )


class DepartmentProfile(FrozenModel):
    """一个部门在确定 Scope 与 Index Revision 下的派生缓存。"""

    scope_id: str = Field(pattern=_SHA256_PATTERN)
    index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    metadata_revision: str = Field(pattern=_SHA256_PATTERN)
    profile_revision: str = Field(pattern=_SHA256_PATTERN)
    embedding_identity: DepartmentEmbeddingIdentity | None = None
    department_key: str = Field(min_length=1, max_length=240)
    department_name: str = Field(min_length=1, max_length=200)
    aliases: tuple[str, ...] = Field(default=(), max_length=32)
    category_paths: tuple[tuple[str, ...], ...] = ()
    document_titles: tuple[str, ...] = Field(max_length=_MAX_PROFILE_DOCUMENTS)
    representative_headings: tuple[str, ...] = Field(default=(), max_length=512)
    topic_keys: tuple[str, ...] = Field(default=(), max_length=512)
    document_ids: tuple[str, ...] = Field(max_length=_MAX_PROFILE_DOCUMENTS)
    document_count: int = Field(ge=1, le=_MAX_PROFILE_DOCUMENTS)
    profile_text_digest: str = Field(pattern=_SHA256_PATTERN)
    vector: tuple[float, ...] | None = Field(default=None, repr=False)
    built_at: datetime

    @model_validator(mode="after")
    def _validate_vector(self) -> DepartmentProfile:
        if self.document_count != len(self.document_ids):
            raise ValueError("部门 Profile 文档计数与身份数量不一致。")
        if self.vector is None:
            if self.embedding_identity is not None:
                raise ValueError("无向量的部门 Profile 不得声明向量身份。")
            return self
        identity = self.embedding_identity
        if identity is None or len(self.vector) != identity.dimension:
            raise ValueError("部门 Profile 向量与向量空间身份不一致。")
        if any(not math.isfinite(value) for value in self.vector):
            raise ValueError("部门 Profile 向量必须全部为有限数值。")
        return self


class DepartmentProfileSet(FrozenModel):
    """一次原子发布的完整部门 Profile 集合。"""

    schema_revision: str = Field(
        default=DEPARTMENT_PROFILE_SCHEMA_REVISION,
        pattern=r"^wanshitong-department-profile-v1$",
    )
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    scope_id: str = Field(pattern=_SHA256_PATTERN)
    index_revision_id: str = Field(pattern=r"^irev_[0-9a-f]{32}$")
    metadata_revision: str = Field(pattern=_SHA256_PATTERN)
    profile_revision: str = Field(pattern=_SHA256_PATTERN)
    embedding_identity: DepartmentEmbeddingIdentity | None = None
    profiles: tuple[DepartmentProfile, ...]
    built_at: datetime

    @model_validator(mode="after")
    def _validate_profiles(self) -> DepartmentProfileSet:
        keys = tuple(profile.department_key for profile in self.profiles)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("部门 Profile 必须按唯一 department_key 排序。")
        for profile in self.profiles:
            if (
                profile.scope_id != self.scope_id
                or profile.index_revision_id != self.index_revision_id
                or profile.metadata_revision != self.metadata_revision
                or profile.embedding_identity != self.embedding_identity
            ):
                raise ValueError("部门 Profile 集合存在跨版本或跨 Scope 数据。")
        return self


class DepartmentProfileLoadError(ValueError):
    """Profile 文件存在但内容损坏或身份不一致。"""


class DepartmentProfileSourceReader:
    """从 Universal SQLite 只读冻结活动 Revision 的元数据快照。"""

    def __init__(self, connections: SqliteConnectionFactory) -> None:
        self._connections = connections

    def snapshot(
        self, project_id: str, knowledge_base_id: str
    ) -> DepartmentProfileSourceSnapshot:
        """读取活动 Index Revision 中全部可用文档元数据。

        Args:
            project_id: 固定项目身份。
            knowledge_base_id: 固定知识库身份。

        Returns:
            与活动索引精确绑定的不可变 Profile 来源快照。

        Raises:
            ValueError: Scope、活动 Revision 或元数据数量无效。

        """
        with self._connections.transaction() as connection:
            knowledge_base = connection.execute(
                "SELECT active_revision_id FROM knowledge_bases "
                "WHERE project_id=? AND knowledge_base_id=? "
                "AND deleted_at IS NULL",
                (project_id, knowledge_base_id),
            ).fetchone()
            if knowledge_base is None:
                raise ValueError("部门 Profile 的固定 Scope 不存在。")
            revision_id = knowledge_base["active_revision_id"]
            if revision_id is None:
                raise ValueError("部门 Profile 缺少活动 Index Revision。")
            revision = connection.execute(
                "SELECT state FROM index_revisions "
                "WHERE index_revision_id=? AND project_id=? "
                "AND knowledge_base_id=?",
                (revision_id, project_id, knowledge_base_id),
            ).fetchone()
            if revision is None or revision["state"] != "active":
                raise ValueError("部门 Profile 的活动 Index Revision 无效。")
            rows = connection.execute(
                "SELECT rd.document_id,rd.document_version_id,"
                "COALESCE(vm.metadata_json,d.metadata_json) AS metadata_json "
                "FROM revision_documents rd JOIN documents d "
                "ON d.document_id=rd.document_id "
                "LEFT JOIN wanshitong_document_version_metadata vm "
                "ON vm.document_version_id=rd.document_version_id "
                "WHERE rd.revision_id=? AND d.project_id=? "
                "AND d.knowledge_base_id=? AND d.status='active' "
                "AND d.deleted_at IS NULL ORDER BY rd.document_id",
                (revision_id, project_id, knowledge_base_id),
            ).fetchall()
        if len(rows) > _MAX_PROFILE_DOCUMENTS:
            raise ValueError("部门 Profile 活动文档数量超过固定上限。")
        documents = tuple(_source_document(row) for row in rows)
        scope_id = canonical_sha256(
            {"project_id": project_id, "knowledge_base_id": knowledge_base_id}
        )
        metadata_revision = canonical_sha256(
            [document.model_dump(mode="json") for document in documents]
        )
        return DepartmentProfileSourceSnapshot(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            scope_id=scope_id,
            index_revision_id=str(revision_id),
            metadata_revision=metadata_revision,
            documents=documents,
        )


class DepartmentProfileStore:
    """按 Scope、Index Revision 与 metadata revision 精确读写 JSON。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path_for(
        self,
        scope_id: str,
        index_revision_id: str,
        metadata_revision: str,
    ) -> Path:
        """返回不依赖“最新文件”指针的版本化路径。"""
        for name, value in (
            ("scope_id", scope_id),
            ("metadata_revision", metadata_revision),
        ):
            if (
                not value.startswith("sha256:")
                or len(value) != _SHA256_IDENTITY_LENGTH
            ):
                raise ValueError(f"{name} 必须是 SHA-256 身份。")
        if not index_revision_id.startswith("irev_"):
            raise ValueError("index_revision_id 格式无效。")
        return (
            self._root
            / scope_id.removeprefix("sha256:")
            / index_revision_id
            / f"{metadata_revision.removeprefix('sha256:')}.json"
        )

    def save(self, profiles: DepartmentProfileSet) -> Path:
        """校验后以临时文件和原子替换发布 Profile 集合。

        Args:
            profiles: 已完成版本和向量校验的 Profile 集合。

        Returns:
            最终版本化 JSON 路径。

        Raises:
            OSError: 文件系统无法安全写入或替换。
            ValidationError: 序列化后的文件无法按原合同读回。

        """
        target = self.path_for(
            profiles.scope_id,
            profiles.index_revision_id,
            profiles.metadata_revision,
        )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        serialized = canonical_json(profiles.model_dump(mode="json"))
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=".department-profile-", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            DepartmentProfileSet.model_validate_json(
                temporary.read_text(encoding="utf-8")
            )
            temporary.replace(target)
            target.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def load(
        self,
        scope_id: str,
        index_revision_id: str,
        metadata_revision: str,
    ) -> DepartmentProfileSet | None:
        """只读取请求精确版本，缺失时不退回旧 Profile。"""
        target = self.path_for(scope_id, index_revision_id, metadata_revision)
        if not target.is_file() or target.is_symlink():
            return None
        try:
            profiles = DepartmentProfileSet.model_validate_json(
                target.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            raise DepartmentProfileLoadError(
                "部门 Profile 文件损坏或无法读取。"
            ) from error
        if (
            profiles.scope_id != scope_id
            or profiles.index_revision_id != index_revision_id
            or profiles.metadata_revision != metadata_revision
        ):
            raise DepartmentProfileLoadError("部门 Profile 文件身份不匹配。")
        return profiles


def build_department_profiles(
    snapshot: DepartmentProfileSourceSnapshot,
    *,
    embedding_identity: DepartmentEmbeddingIdentity | None = None,
    vectors: Mapping[str, Sequence[float]] | None = None,
    built_at: datetime | None = None,
) -> DepartmentProfileSet:
    """按部门 Key 聚合活动文档，保持同名不同 Key 独立。

    Args:
        snapshot: 活动索引与 metadata revision 的只读快照。
        embedding_identity: 可选的统一部门向量空间身份。
        vectors: 以 department_key 索引的离线构建向量。
        built_at: 测试可注入的构建时间。

    Returns:
        可原子发布的完整 Profile 集合。

    Raises:
        ValueError: 向量集合不完整、维度不符或包含未知部门。

    """
    vector_map = {} if vectors is None else dict(vectors)
    if (embedding_identity is None) != (not vector_map):
        raise ValueError("部门向量和 embedding_identity 必须同时提供。")
    grouped: defaultdict[str, list[DepartmentProfileSourceDocument]] = (
        defaultdict(list)
    )
    for document in snapshot.documents:
        grouped[document.department_key or UNKNOWN_DEPARTMENT_KEY].append(
            document
        )
    if embedding_identity is not None and set(vector_map) != set(grouped):
        raise ValueError("部门向量必须与当前 Profile 部门集合逐项一致。")
    timestamp = built_at or datetime.now(UTC)
    profiles: list[DepartmentProfile] = []
    for department_key in sorted(grouped):
        documents = tuple(grouped[department_key])
        names = tuple(
            dict.fromkeys(
                document.department_name
                for document in documents
                if document.department_name
            )
        )
        department_name = names[0] if names else UNKNOWN_DEPARTMENT_NAME
        aliases = _ordered_unique(
            (
                *names[1:],
                *(
                    alias
                    for document in documents
                    for alias in document.aliases
                ),
            )
        )
        category_paths = tuple(
            dict.fromkeys(
                document.category_path
                for document in documents
                if document.category_path
            )
        )
        document_titles = _ordered_unique(
            tuple(document.document_title for document in documents)
        )
        representative_headings = _ordered_unique(
            tuple(
                heading
                for document in documents
                for heading in document.representative_headings
            )
        )
        topic_keys = _ordered_unique(
            tuple(
                topic for document in documents for topic in document.topic_keys
            )
        )
        document_ids = tuple(document.document_id for document in documents)
        profile_identity_body: dict[str, object] = {
            "scope_id": snapshot.scope_id,
            "index_revision_id": snapshot.index_revision_id,
            "metadata_revision": snapshot.metadata_revision,
            "embedding_identity": (
                None
                if embedding_identity is None
                else embedding_identity.model_dump(mode="json")
            ),
            "department_key": department_key,
            "department_name": department_name,
            "aliases": aliases,
            "category_paths": category_paths,
            "document_titles": document_titles,
            "representative_headings": representative_headings,
            "topic_keys": topic_keys,
            "document_ids": document_ids,
            "document_count": len(document_ids),
            "profile_text_digest": canonical_sha256(
                {
                    "department_name": department_name,
                    "aliases": aliases,
                    "category_paths": category_paths,
                    "document_titles": document_titles,
                    "representative_headings": representative_headings,
                    "topic_keys": topic_keys,
                }
            ),
            "vector": (
                None
                if embedding_identity is None
                else tuple(float(value) for value in vector_map[department_key])
            ),
        }
        profiles.append(
            DepartmentProfile(
                scope_id=snapshot.scope_id,
                index_revision_id=snapshot.index_revision_id,
                metadata_revision=snapshot.metadata_revision,
                profile_revision=canonical_sha256(profile_identity_body),
                embedding_identity=embedding_identity,
                department_key=department_key,
                department_name=department_name,
                aliases=aliases,
                category_paths=category_paths,
                document_titles=document_titles,
                representative_headings=representative_headings,
                topic_keys=topic_keys,
                document_ids=document_ids,
                document_count=len(document_ids),
                profile_text_digest=str(
                    profile_identity_body["profile_text_digest"]
                ),
                vector=(
                    None
                    if embedding_identity is None
                    else tuple(
                        float(value) for value in vector_map[department_key]
                    )
                ),
                built_at=timestamp,
            )
        )
    set_identity_body = {
        "schema_revision": DEPARTMENT_PROFILE_SCHEMA_REVISION,
        "project_id": snapshot.project_id,
        "knowledge_base_id": snapshot.knowledge_base_id,
        "scope_id": snapshot.scope_id,
        "index_revision_id": snapshot.index_revision_id,
        "metadata_revision": snapshot.metadata_revision,
        "embedding_identity": (
            None
            if embedding_identity is None
            else embedding_identity.model_dump(mode="json")
        ),
        "profile_revisions": tuple(
            profile.profile_revision for profile in profiles
        ),
    }
    return DepartmentProfileSet(
        schema_revision=DEPARTMENT_PROFILE_SCHEMA_REVISION,
        project_id=snapshot.project_id,
        knowledge_base_id=snapshot.knowledge_base_id,
        scope_id=snapshot.scope_id,
        index_revision_id=snapshot.index_revision_id,
        metadata_revision=snapshot.metadata_revision,
        profile_revision=canonical_sha256(set_identity_body),
        embedding_identity=embedding_identity,
        profiles=tuple(profiles),
        built_at=timestamp,
    )


def _source_document(
    row: Mapping[str, object],
) -> DepartmentProfileSourceDocument:
    try:
        raw_metadata = json.loads(str(row["metadata_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("活动文档的部门元数据不是有效 JSON。") from error
    if not isinstance(raw_metadata, dict):
        raise ValueError("活动文档的部门元数据必须是对象。")
    document_title = raw_metadata.get("document_title")
    if not isinstance(document_title, str) or not document_title.strip():
        document_title = str(row["document_id"])
    return DepartmentProfileSourceDocument(
        document_id=str(row["document_id"]),
        document_version_id=str(row["document_version_id"]),
        department_key=_optional_text(raw_metadata.get("department_key")),
        department_name=_optional_text(
            raw_metadata.get("department_name")
            or raw_metadata.get("department")
        ),
        category_path=_text_tuple(raw_metadata.get("category_path")),
        document_title=document_title.strip(),
        representative_headings=_text_tuple(
            raw_metadata.get("representative_headings")
        ),
        topic_keys=_text_tuple(raw_metadata.get("topic_keys")),
        aliases=_text_tuple(raw_metadata.get("department_aliases")),
        metadata_revision=(
            _optional_text(raw_metadata.get("metadata_revision")) or "UNKNOWN"
        ),
    )


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return _ordered_unique(
        tuple(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    )


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "DEPARTMENT_PROFILE_SCHEMA_REVISION",
    "UNKNOWN_DEPARTMENT_KEY",
    "UNKNOWN_DEPARTMENT_NAME",
    "DepartmentEmbeddingIdentity",
    "DepartmentProfile",
    "DepartmentProfileLoadError",
    "DepartmentProfileSet",
    "DepartmentProfileSourceDocument",
    "DepartmentProfileSourceReader",
    "DepartmentProfileSourceSnapshot",
    "DepartmentProfileStore",
    "build_department_profiles",
]
