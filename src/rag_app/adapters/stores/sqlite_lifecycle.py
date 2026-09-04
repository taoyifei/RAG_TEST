"""SQLite P09 生命周期、幂等记录与公共作业视图。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.core.errors import (
    Conflict,
    NotFound,
    QueueLimitExceeded,
    RevisionStateError,
)
from rag_app.core.identifiers import canonical_json, document_version_id
from rag_app.core.models.management import (
    ArtifactDescriptor,
    Document,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    Job,
    JobStatus,
    KnowledgeBase,
    KnowledgeBaseStatus,
    Project,
    ProjectStatus,
    QueuedIngestion,
    SlotProgress,
)

_MAX_PAGE_SIZE = 200
_DEFAULT_MAX_PENDING_JOBS = 64
_MAX_JOB_ATTEMPTS = 3


class SqliteLifecycleStore:
    """以独立小接口补齐 P09 公共生命周期读写。"""

    def __init__(
        self,
        connections: SqliteConnectionFactory,
        *,
        max_pending_jobs: int = _DEFAULT_MAX_PENDING_JOBS,
    ) -> None:
        """保存已完成 P09 migration 的连接工厂。

        Args:
            connections: SQLite 连接工厂。
            max_pending_jobs: queued/running 请求总量上限。

        Returns:
            无返回值。

        """
        if max_pending_jobs <= 0:
            raise ValueError("持久作业队列上限必须为正数。")
        self._connections = connections
        self._max_pending_jobs = max_pending_jobs

    def create_project(self, project_id: str, name: str) -> Project:
        """幂等创建项目。

        Args:
            project_id: 服务端生成的项目 ID。
            name: 非空显示名。

        Returns:
            持久化项目视图。

        """
        now = _now()
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT name FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is not None and str(row["name"]) != name:
                raise Conflict(
                    "项目 ID 已绑定其他内容。", stage="project.create"
                )
            connection.execute(
                "INSERT OR IGNORE INTO projects("
                "project_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (project_id, name, now, now),
            )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> Project:
        """读取项目。

        Args:
            project_id: 目标项目 ID。

        Returns:
            项目公共视图。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT project_id, name, lifecycle_status, created_at, "
                "updated_at FROM projects WHERE project_id=? "
                "AND deleted_at IS NULL",
                (project_id,),
            ).fetchone()
        if row is None:
            raise NotFound("项目不存在。", stage="project.read")
        return _project(row)

    def list_projects(self, *, limit: int, offset: int) -> tuple[Project, ...]:
        """按 ID 稳定分页读取项目。

        Args:
            limit: 有界页大小。
            offset: 非负偏移。

        Returns:
            当前页项目。

        """
        _validate_page(limit, offset)
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT project_id, name, lifecycle_status, created_at, "
                "updated_at FROM projects WHERE deleted_at IS NULL "
                "ORDER BY project_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return tuple(_project(row) for row in rows)

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        status: ProjectStatus | None = None,
    ) -> Project:
        """更新项目显示名或状态。

        Args:
            project_id: 目标项目 ID。
            name: 可选新显示名。
            status: 可选新状态。

        Returns:
            更新后的项目。

        """
        current = self.get_project(project_id)
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE projects SET name=?, lifecycle_status=?, updated_at=? "
                "WHERE project_id=?",
                (
                    name or current.name,
                    (status or current.status).value,
                    _now(),
                    project_id,
                ),
            )
        return self.get_project(project_id)

    def create_knowledge_base(
        self,
        knowledge_base_id: str,
        project_id: str,
        name: str,
        *,
        profile_id: str,
        description: str,
    ) -> KnowledgeBase:
        """在项目 scope 内幂等创建知识库。

        Args:
            knowledge_base_id: 服务端生成的知识库 ID。
            project_id: 所属项目 ID。
            name: 显示名。
            profile_id: 冻结 Profile 身份。
            description: 可选说明。

        Returns:
            持久化知识库。

        """
        self.get_project(project_id)
        now = _now()
        with self._connections.transaction(write=True) as connection:
            try:
                connection.execute(
                    "INSERT INTO knowledge_bases("
                    "knowledge_base_id, project_id, name, normalized_name, "
                    "description, profile_id, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        knowledge_base_id,
                        project_id,
                        name,
                        name.strip().casefold(),
                        description,
                        profile_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                row = connection.execute(
                    "SELECT project_id, name, profile_id FROM knowledge_bases "
                    "WHERE knowledge_base_id=?",
                    (knowledge_base_id,),
                ).fetchone()
                if row is None or tuple(row) != (project_id, name, profile_id):
                    raise Conflict(
                        "知识库名称或 ID 已在 scope 内使用。",
                        stage="knowledge_base.create",
                    ) from error
        return self.get_knowledge_base(project_id, knowledge_base_id)

    def get_knowledge_base(
        self, project_id: str, knowledge_base_id: str
    ) -> KnowledgeBase:
        """按完整 scope 读取知识库。

        Args:
            project_id: 目标项目 ID。
            knowledge_base_id: 目标知识库 ID。

        Returns:
            知识库公共视图。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT project_id, knowledge_base_id, name, description, "
                "profile_id, lifecycle_status, active_revision_id, created_at, "
                "updated_at FROM knowledge_bases WHERE project_id=? AND "
                "knowledge_base_id=? AND deleted_at IS NULL",
                (project_id, knowledge_base_id),
            ).fetchone()
        if row is None:
            raise NotFound("知识库不存在。", stage="knowledge_base.read")
        return _knowledge_base(row)

    def list_knowledge_bases(
        self, project_id: str, *, limit: int, offset: int
    ) -> tuple[KnowledgeBase, ...]:
        """按 ID 稳定分页读取项目内知识库。

        Args:
            project_id: 所属项目 ID。
            limit: 有界页大小。
            offset: 非负偏移。

        Returns:
            当前页知识库。

        """
        self.get_project(project_id)
        _validate_page(limit, offset)
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT project_id, knowledge_base_id, name, description, "
                "profile_id, lifecycle_status, active_revision_id, created_at, "
                "updated_at FROM knowledge_bases WHERE project_id=? AND "
                "deleted_at IS NULL ORDER BY knowledge_base_id "
                "LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            ).fetchall()
        return tuple(_knowledge_base(row) for row in rows)

    def update_knowledge_base(
        self,
        project_id: str,
        knowledge_base_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: KnowledgeBaseStatus | None = None,
    ) -> KnowledgeBase:
        """更新知识库显示信息或状态。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。
            name: 可选新名称。
            description: 可选新说明。
            status: 可选新状态。

        Returns:
            更新后的知识库。

        """
        current = self.get_knowledge_base(project_id, knowledge_base_id)
        resolved_name = name or current.name
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE knowledge_bases SET name=?, normalized_name=?, "
                "description=?, lifecycle_status=?, updated_at=? WHERE "
                "project_id=? AND knowledge_base_id=?",
                (
                    resolved_name,
                    resolved_name.strip().casefold(),
                    current.description if description is None else description,
                    (status or current.status).value,
                    _now(),
                    project_id,
                    knowledge_base_id,
                ),
            )
        return self.get_knowledge_base(project_id, knowledge_base_id)

    def get_document(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> Document:
        """按完整 scope 读取逻辑文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 全局逻辑文档 ID。

        Returns:
            文档公共视图。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT d.project_id, d.knowledge_base_id, d.document_id, "
                "d.display_name, d.lifecycle_status, d.current_version_id, "
                "kb.active_revision_id, d.created_at, d.updated_at "
                "FROM documents d JOIN knowledge_bases kb "
                "ON kb.knowledge_base_id=d.knowledge_base_id "
                "WHERE d.project_id=? AND d.knowledge_base_id=? "
                "AND d.document_id=? "
                "AND d.deleted_at IS NULL",
                (project_id, knowledge_base_id, document_id),
            ).fetchone()
        if row is None:
            raise NotFound("文档不存在。", stage="document.read")
        return _document(row)

    def mark_knowledge_base_deleting(
        self, project_id: str, knowledge_base_id: str
    ) -> KnowledgeBase:
        """将知识库删除转换为持久生命周期操作。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 目标知识库 ID。

        Returns:
            deleting 状态知识库。

        """
        self.get_knowledge_base(project_id, knowledge_base_id)
        now = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE knowledge_bases SET lifecycle_status='deleting', "
                "updated_at=? WHERE project_id=? AND knowledge_base_id=?",
                (now, project_id, knowledge_base_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO lifecycle_operations("
                "operation_id, operation_type, project_id, knowledge_base_id, "
                "document_id, state, created_at, updated_at) "
                "VALUES (?, 'delete_knowledge_base', ?, ?, NULL, "
                "'planned', ?, ?)",
                (
                    f"delete_knowledge_base:{knowledge_base_id}",
                    project_id,
                    knowledge_base_id,
                    now,
                    now,
                ),
            )
            _cancel_scope_jobs(
                connection,
                knowledge_base_id=knowledge_base_id,
                document_id=None,
                now=now,
            )
        return self.get_knowledge_base(project_id, knowledge_base_id)

    def list_documents(
        self,
        project_id: str,
        knowledge_base_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[Document, ...]:
        """按 ID 稳定分页读取知识库文档。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            limit: 有界页大小。
            offset: 非负偏移。

        Returns:
            当前页文档。

        """
        self.get_knowledge_base(project_id, knowledge_base_id)
        _validate_page(limit, offset)
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT d.project_id, d.knowledge_base_id, d.document_id, "
                "d.display_name, d.lifecycle_status, d.current_version_id, "
                "kb.active_revision_id, d.created_at, d.updated_at "
                "FROM documents d JOIN knowledge_bases kb "
                "ON kb.knowledge_base_id=d.knowledge_base_id "
                "WHERE d.project_id=? AND d.knowledge_base_id=? AND "
                "d.deleted_at IS NULL ORDER BY d.document_id LIMIT ? OFFSET ?",
                (project_id, knowledge_base_id, limit, offset),
            ).fetchall()
        return tuple(_document(row) for row in rows)

    def rename_document(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        display_name: str,
    ) -> Document:
        """只修改显示名，不创建版本或 Revision。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。
            display_name: 新显示名。

        Returns:
            更新后的文档。

        """
        self.get_document(project_id, knowledge_base_id, document_id)
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE documents SET display_name=?, updated_at=? WHERE "
                "project_id=? AND knowledge_base_id=? AND document_id=?",
                (
                    display_name,
                    _now(),
                    project_id,
                    knowledge_base_id,
                    document_id,
                ),
            )
        return self.get_document(project_id, knowledge_base_id, document_id)

    def mark_document_deleting(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> Document:
        """将删除转换为受控生命周期操作。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。

        Returns:
            状态为 deleting 的文档。

        """
        self.get_document(project_id, knowledge_base_id, document_id)
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE documents SET lifecycle_status='deleting', "
                "updated_at=? "
                "WHERE project_id=? AND knowledge_base_id=? AND document_id=?",
                (_now(), project_id, knowledge_base_id, document_id),
            )
            now = _now()
            connection.execute(
                "INSERT OR IGNORE INTO lifecycle_operations("
                "operation_id, operation_type, project_id, knowledge_base_id, "
                "document_id, state, created_at, updated_at) "
                "VALUES (?, 'delete_document', ?, ?, ?, 'planned', ?, ?)",
                (
                    f"delete_document:{document_id}",
                    project_id,
                    knowledge_base_id,
                    document_id,
                    now,
                    now,
                ),
            )
            _cancel_scope_jobs(
                connection,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                now=now,
            )
        return self.get_document(project_id, knowledge_base_id, document_id)

    def list_document_versions(
        self, project_id: str, knowledge_base_id: str, document_id: str
    ) -> tuple[DocumentVersion, ...]:
        """读取文档的全部不可变版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标逻辑文档 ID。

        Returns:
            按创建时间和 ID 排序的版本。

        """
        self.get_document(project_id, knowledge_base_id, document_id)
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT document_id, document_version_id, content_sha256, "
                "source_artifact_id, size_bytes, media_type, lifecycle_status, "
                "created_at FROM document_versions WHERE document_id=? "
                "ORDER BY created_at, document_version_id",
                (document_id,),
            ).fetchall()
        return tuple(_document_version(row) for row in rows)

    def get_document_version(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
    ) -> DocumentVersion:
        """按完整 scope 读取单个版本。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 目标文档 ID。
            document_version_id: 目标版本 ID。

        Returns:
            不可变版本公共视图。

        """
        for version in self.list_document_versions(
            project_id, knowledge_base_id, document_id
        ):
            if version.document_version_id == document_version_id:
                return version
        raise NotFound("文档版本不存在。", stage="document_version.read")

    def mark_version_ready(
        self, document_id: str, document_version_id: str
    ) -> None:
        """激活后推进版本并淘汰旧 Ready 版本。

        Args:
            document_id: 目标文档 ID。
            document_version_id: 新 Ready 版本 ID。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE document_versions SET lifecycle_status='superseded' "
                "WHERE document_id=? AND document_version_id<>? AND "
                "lifecycle_status='ready'",
                (document_id, document_version_id),
            )
            connection.execute(
                "UPDATE document_versions SET lifecycle_status='ready' "
                "WHERE document_id=? AND document_version_id=?",
                (document_id, document_version_id),
            )
            connection.execute(
                "UPDATE documents SET current_version_id=?, updated_at=? "
                "WHERE document_id=?",
                (document_version_id, _now(), document_id),
            )

    def claim_idempotency(
        self,
        *,
        scope_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        result_id: str,
    ) -> str:
        """持久化幂等键并拒绝同键异请求。

        Args:
            scope_id: Project、KB 或 Document scope ID。
            operation: 规范化写操作名。
            idempotency_key: 调用方提供的非空键。
            request_hash: canonical request 指纹。
            result_id: 首次请求预分配的结果 ID。

        Returns:
            首次或既有结果 ID。

        """
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT request_hash, result_id FROM idempotency_records "
                "WHERE scope_id=? AND operation=? AND idempotency_key=?",
                (scope_id, operation, idempotency_key),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash:
                    raise Conflict(
                        "Idempotency-Key 已绑定不同请求。",
                        stage="idempotency.claim",
                        code="IDEMPOTENCY_CONFLICT",
                    )
                return str(row["result_id"])
            connection.execute(
                "INSERT INTO idempotency_records(scope_id, operation, "
                "idempotency_key, request_hash, result_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    scope_id,
                    operation,
                    idempotency_key,
                    request_hash,
                    result_id,
                    _now(),
                ),
            )
        return result_id

    def bind_job_document(
        self, job_id: str, document_id: str, document_version_id: str
    ) -> None:
        """把 Builder Job 绑定到公开文档与版本。

        Args:
            job_id: 已持久化 Job ID。
            document_id: 目标文档 ID。
            document_version_id: 目标版本 ID。

        Returns:
            无返回值。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE ingestion_jobs SET document_id=?, "
                "document_version_id=?, "
                "updated_at=? WHERE job_id=?",
                (document_id, document_version_id, _now(), job_id),
            )

    def enqueue_ingestion(
        self, request: QueuedIngestion, *, idempotency_key: str
    ) -> Job:
        """原子保存 Job、目标版本和持久无正文请求。

        Args:
            request: 完整 Revision 构建请求。
            idempotency_key: 调用方幂等键。

        Returns:
            queued 或既有 Job。

        """
        target = next(
            item
            for item in request.documents
            if item.document.document_id == request.target_document_id
        )
        if request.target_document_version_id != document_version_id(
            request.target_document_id, target.content_sha256
        ):
            raise ValueError("队列目标版本身份无效。")
        document = target.document
        serialized = canonical_json(request.model_dump(mode="json"))
        now = _now()
        with self._connections.transaction(write=True) as connection:
            existing_request = connection.execute(
                "SELECT job_id FROM ingestion_requests WHERE job_id=?",
                (request.job_id,),
            ).fetchone()
            if existing_request is None:
                pending = connection.execute(
                    "SELECT count(*) AS value FROM ingestion_requests "
                    "WHERE state IN ('queued', 'running')"
                ).fetchone()
                if int(pending["value"]) >= self._max_pending_jobs:
                    raise QueueLimitExceeded(
                        "本地作业队列已满，请稍后重试。",
                        stage="job.queue",
                    )
            existing_document = connection.execute(
                "SELECT project_id, knowledge_base_id FROM documents "
                "WHERE document_id=?",
                (document.document_id,),
            ).fetchone()
            if existing_document is None:
                connection.execute(
                    "INSERT INTO documents(document_id, project_id, "
                    "knowledge_base_id, display_name, status, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
                    (
                        document.document_id,
                        document.project_id,
                        document.knowledge_base_id,
                        document.display_name,
                        now,
                        now,
                    ),
                )
            elif (
                str(existing_document["project_id"]),
                str(existing_document["knowledge_base_id"]),
            ) != (document.project_id, document.knowledge_base_id):
                raise Conflict(
                    "文档 ID 已绑定其他 scope。", stage="document.queue"
                )
            connection.execute(
                "INSERT OR IGNORE INTO document_versions("
                "document_version_id, document_id, content_sha256, "
                "source_artifact_id, size_bytes, media_type, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request.target_document_version_id,
                    document.document_id,
                    target.content_sha256,
                    target.artifact_id,
                    target.size_bytes,
                    target.media_type,
                    now,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO ingestion_jobs("
                "job_id, project_id, knowledge_base_id, document_id, "
                "document_version_id, revision_id, idempotency_key, state, "
                "stage, attempt, heartbeat_at, retryable, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', "
                "'queued', 0, ?, 0, ?, ?)",
                (
                    request.job_id,
                    document.project_id,
                    document.knowledge_base_id,
                    document.document_id,
                    request.target_document_version_id,
                    request.revision_id,
                    idempotency_key,
                    now,
                    now,
                    now,
                ),
            )
            job = connection.execute(
                "SELECT project_id, knowledge_base_id, document_id, "
                "document_version_id, revision_id FROM ingestion_jobs "
                "WHERE job_id=?",
                (request.job_id,),
            ).fetchone()
            expected = (
                document.project_id,
                document.knowledge_base_id,
                document.document_id,
                request.target_document_version_id,
                request.revision_id,
            )
            if job is None or tuple(job) != expected:
                raise Conflict("Job ID 已绑定不同构建。", stage="job.queue")
            connection.execute(
                "INSERT OR IGNORE INTO ingestion_requests("
                "job_id, request_json, state, created_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, ?)",
                (request.job_id, serialized, now, now),
            )
        return self.get_job(request.job_id)

    def claim_ingestion(self, job_id: str) -> QueuedIngestion | None:
        """原子领取一个未取消的持久构建请求。

        Args:
            job_id: 目标 Job ID。

        Returns:
            成功领取的请求；不可领取时返回 None。

        """
        now = _now()
        with self._connections.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT r.request_json, r.state, j.cancel_requested "
                "FROM ingestion_requests r JOIN ingestion_jobs j "
                "ON j.job_id=r.job_id WHERE r.job_id=?",
                (job_id,),
            ).fetchone()
            if (
                row is None
                or str(row["state"]) != "queued"
                or bool(row["cancel_requested"])
            ):
                return None
            cursor = connection.execute(
                "UPDATE ingestion_requests SET state='running', updated_at=? "
                "WHERE job_id=? AND state='queued'",
                (now, job_id),
            )
            if cursor.rowcount != 1:
                return None
            connection.execute(
                "UPDATE ingestion_jobs SET state='running', stage='claimed', "
                "started_at=coalesce(started_at, ?), heartbeat_at=?, "
                "updated_at=? WHERE job_id=?",
                (now, now, now, job_id),
            )
        return QueuedIngestion.model_validate_json(str(row["request_json"]))

    def pending_ingestion_jobs(self) -> tuple[str, ...]:
        """恢复中断请求并返回全部 queued Job。

        Args:
            无参数；读取持久队列。

        Returns:
            稳定 Job ID 序列。

        """
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE ingestion_requests SET state='queued', updated_at=? "
                "WHERE state='running' AND job_id IN (SELECT job_id FROM "
                "ingestion_jobs WHERE state='interrupted')",
                (_now(),),
            )
            rows = connection.execute(
                "SELECT r.job_id FROM ingestion_requests r "
                "JOIN ingestion_jobs j ON j.job_id=r.job_id "
                "WHERE r.state='queued' AND j.cancel_requested=0 "
                "ORDER BY r.created_at, r.job_id"
            ).fetchall()
        return tuple(str(row["job_id"]) for row in rows)

    def finish_ingestion(
        self,
        job_id: str,
        *,
        succeeded: bool,
        error_code: str | None = None,
        safe_message: str | None = None,
        retryable: bool = False,
    ) -> None:
        """保存请求终态并补写 Builder 前失败的安全 Job 状态。

        Args:
            job_id: 目标 Job ID。
            succeeded: 是否成功。
            error_code: 可选稳定错误码。
            safe_message: 可选安全消息。
            retryable: 是否可安全重试。

        Returns:
            无返回值。

        """
        now = _now()
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE ingestion_requests SET state=?, updated_at=? "
                "WHERE job_id=?",
                ("succeeded" if succeeded else "failed", now, job_id),
            )
            if not succeeded:
                job = connection.execute(
                    "SELECT attempt FROM ingestion_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if job is None:
                    raise NotFound("作业不存在。", stage="job.finish")
                can_retry = (
                    retryable and int(job["attempt"]) < _MAX_JOB_ATTEMPTS
                )
                connection.execute(
                    "UPDATE ingestion_jobs SET state=?, stage='failed', "
                    "error_code=?, safe_message=?, retryable=?, updated_at=?, "
                    "finished_at=? WHERE job_id=? AND state IN ("
                    "'pending', 'running', 'interrupted', 'completed')",
                    (
                        "failed_retryable" if can_retry else "failed_terminal",
                        error_code or "INGESTION_FAILED",
                        safe_message or "文档构建失败。",
                        int(can_retry),
                        now,
                        now,
                        job_id,
                    ),
                )

    def get_job(self, job_id: str) -> Job:
        """读取不含 fencing token 的作业与实际 slot 进度。

        Args:
            job_id: 目标 Job ID。

        Returns:
            P09 稳定作业视图。

        """
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT job_id, project_id, knowledge_base_id, document_id, "
                "document_version_id, revision_id, state, stage, attempt, "
                "error_code, safe_message, retryable, cancel_requested, "
                "(SELECT state FROM ingestion_requests r WHERE "
                "r.job_id=ingestion_jobs.job_id) AS request_state FROM "
                "ingestion_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise NotFound("作业不存在。", stage="job.read")
            lease = connection.execute(
                "SELECT owner_job_id, state FROM revision_build_leases "
                "WHERE revision_id=?",
                (str(row["revision_id"]),),
            ).fetchone()
            slots = connection.execute(
                "SELECT slot_id, vector_written_count, failed_count, "
                "expected_chunk_count FROM revision_embedding_coverage "
                "WHERE revision_id=? ORDER BY slot_id",
                (str(row["revision_id"]),),
            ).fetchall()
        error_code = (
            None if row["error_code"] is None else str(row["error_code"])
        )
        return Job(
            job_id=str(row["job_id"]),
            project_id=str(row["project_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            document_id=_optional(row["document_id"]),
            document_version_id=_optional(row["document_version_id"]),
            revision_id=str(row["revision_id"]),
            state=_job_status(
                str(row["state"]),
                bool(row["cancel_requested"]),
                error_code,
                _optional(row["request_state"]),
            ),
            stage=(
                "finalizing"
                if row["request_state"] == "running"
                and row["state"] == "completed"
                else str(row["stage"])
            ),
            attempt=int(row["attempt"]),
            retryable=bool(row["retryable"]),
            safe_error=_optional(row["safe_message"]),
            lease_owner=(
                lease is not None and str(lease["owner_job_id"]) == job_id
            ),
            fencing_safe_status=(
                "not_acquired" if lease is None else str(lease["state"])
            ),
            slot_progress=tuple(
                SlotProgress(
                    slot_id=str(slot["slot_id"]),
                    completed=int(slot["vector_written_count"]),
                    failed=int(slot["failed_count"]),
                    total=int(slot["expected_chunk_count"]),
                )
                for slot in slots
            ),
        )

    def list_jobs(
        self,
        *,
        project_id: str | None,
        knowledge_base_id: str | None,
        states: tuple[str, ...],
        limit: int,
        offset: int,
    ) -> tuple[tuple[Job, ...], int]:
        """按 scope 和公开状态稳定分页读取 Job。

        Args:
            project_id: 可选项目过滤。
            knowledge_base_id: 可选知识库过滤。
            states: 可选 P09 公开状态值。
            limit: 单页上限。
            offset: 稳定偏移量。

        Returns:
            Job 分页项和过滤后的总数。

        """
        clauses: list[str] = []
        parameters: list[object] = []
        if project_id is not None:
            clauses.append("project_id=?")
            parameters.append(project_id)
        if knowledge_base_id is not None:
            clauses.append("knowledge_base_id=?")
            parameters.append(knowledge_base_id)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT job_id FROM ingestion_jobs"  # noqa: S608
                + where
                + " ORDER BY created_at DESC, job_id DESC",
                tuple(parameters),
            ).fetchall()
        jobs = tuple(self.get_job(str(row["job_id"])) for row in rows)
        if states:
            allowed = frozenset(states)
            jobs = tuple(job for job in jobs if job.state.value in allowed)
        return jobs[offset : offset + limit], len(jobs)

    def request_job_cancellation(self, job_id: str) -> Job:
        """为非终态 Job 保存可恢复取消请求。

        Args:
            job_id: 目标 Job ID。

        Returns:
            更新后的 Job。

        """
        current = self.get_job(job_id)
        if current.state not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            raise RevisionStateError("终态作业不能取消。", stage="job.cancel")
        with self._connections.transaction(write=True) as connection:
            now = _now()
            if current.state is JobStatus.QUEUED:
                connection.execute(
                    "UPDATE ingestion_jobs SET cancel_requested=1, "
                    "state='failed_terminal', error_code='JOB_CANCELLED', "
                    "safe_message='作业已取消。', retryable=0, updated_at=?, "
                    "finished_at=? WHERE job_id=?",
                    (now, now, job_id),
                )
                connection.execute(
                    "UPDATE ingestion_requests SET state='cancelled', "
                    "updated_at=? WHERE job_id=? AND state='queued'",
                    (now, job_id),
                )
            else:
                connection.execute(
                    "UPDATE ingestion_jobs SET cancel_requested=1, "
                    "updated_at=? WHERE job_id=?",
                    (now, job_id),
                )
        return self.get_job(job_id)

    def retry_job(self, job_id: str) -> Job:
        """将可重试失败恢复到持久化队列。

        Args:
            job_id: 目标 Job ID。

        Returns:
            回到 queued 的 Job。

        """
        current = self.get_job(job_id)
        if current.state is not JobStatus.FAILED_RETRYABLE:
            raise RevisionStateError("作业当前不可重试。", stage="job.retry")
        if current.attempt >= _MAX_JOB_ATTEMPTS:
            raise RevisionStateError(
                "作业已达到最大尝试次数。", stage="job.retry"
            )
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "UPDATE ingestion_jobs SET state='pending', "
                "stage='retry_queued', "
                "attempt=attempt+1, error_code=NULL, safe_message=NULL, "
                "retryable=0, cancel_requested=0, updated_at=?, "
                "finished_at=NULL "
                "WHERE job_id=?",
                (_now(), job_id),
            )
            connection.execute(
                "UPDATE ingestion_requests SET state='queued', updated_at=? "
                "WHERE job_id=? AND state='failed'",
                (_now(), job_id),
            )
        return self.get_job(job_id)

    def list_artifacts(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
    ) -> tuple[ArtifactDescriptor, ...]:
        """经完整逻辑引用链授权后列出 Artifact。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 所属文档 ID。
            document_version_id: 所属版本 ID。

        Returns:
            授权范围内的 Artifact 摘要。

        """
        self.get_document_version(
            project_id, knowledge_base_id, document_id, document_version_id
        )
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT bo.artifact_id, br.owner_id AS document_version_id, "
                "bo.media_type, bo.size_bytes, br.role FROM blob_references br "
                "JOIN blob_objects bo ON bo.artifact_id=br.artifact_id WHERE "
                "br.owner_type='document_version' AND br.owner_id=? "
                "ORDER BY bo.artifact_id",
                (document_version_id,),
            ).fetchall()
        return tuple(
            ArtifactDescriptor(
                artifact_id=str(row["artifact_id"]),
                document_version_id=str(row["document_version_id"]),
                media_type=str(row["media_type"]),
                size_bytes=int(row["size_bytes"]),
                role=str(row["role"]),
            )
            for row in rows
        )

    def authorize_artifact(
        self,
        project_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version_id: str,
        artifact_id: str,
    ) -> ArtifactDescriptor:
        """要求 Artifact 出现在指定版本的引用链中。

        Args:
            project_id: 所属项目 ID。
            knowledge_base_id: 所属知识库 ID。
            document_id: 所属文档 ID。
            document_version_id: 所属版本 ID。
            artifact_id: 待读取 Artifact ID。

        Returns:
            已授权的 Artifact 摘要。

        """
        for artifact in self.list_artifacts(
            project_id, knowledge_base_id, document_id, document_version_id
        ):
            if artifact.artifact_id == artifact_id:
                return artifact
        raise NotFound("Artifact 不存在。", stage="artifact.read")

    def system_integrity(self) -> tuple[str, int, dict[str, object]]:
        """读取 GC 与物理目录对账摘要，不执行删除。

        Args:
            无参数；读取当前数据库。

        Returns:
            完整性状态、待处理 GC 数和对账计数。

        """
        with self._connections.transaction() as connection:
            pending = connection.execute(
                "SELECT count(*) AS value FROM gc_plan_items "
                "WHERE state<>'completed'"
            ).fetchone()
            rows = connection.execute(
                "SELECT observed_state, count(*) AS value FROM "
                "blob_reconciliation GROUP BY observed_state"
            ).fetchall()
        reconciliation: dict[str, object] = {
            str(row["observed_state"]): int(row["value"]) for row in rows
        }
        status = "ok" if not reconciliation else "attention_required"
        return status, int(pending["value"]), reconciliation

    def lexical_status(self) -> tuple[str, str, bool]:
        """读取全部 Active Revision 的词法 schema 兼容状态。

        Args:
            无参数；读取 Active Revision 冻结合同。

        Returns:
            schema、analyzer ID 与是否必须重建索引。

        """
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT ir.lexical_schema_json FROM knowledge_bases kb "
                "JOIN index_revisions ir "
                "ON ir.index_revision_id=kb.active_revision_id "
                "WHERE kb.deleted_at IS NULL ORDER BY kb.knowledge_base_id"
            ).fetchall()
        if not rows:
            return "fts-v2", "deterministic-cjk-bigram-v2", False
        identities = tuple(
            _lexical_identity(row["lexical_schema_json"]) for row in rows
        )
        incompatible = any(version != "2" for version, _ in identities)
        versions = {version for version, _ in identities}
        analyzers = {analyzer for _, analyzer in identities}
        schema = (
            f"fts-v{next(iter(versions))}" if len(versions) == 1 else "mixed"
        )
        analyzer = next(iter(analyzers)) if len(analyzers) == 1 else "mixed"
        return schema, analyzer, incompatible


def _project(row: sqlite3.Row) -> Project:
    return Project(
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        status=ProjectStatus(str(row["lifecycle_status"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _knowledge_base(row: sqlite3.Row) -> KnowledgeBase:
    return KnowledgeBase(
        project_id=str(row["project_id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        profile_id=str(row["profile_id"]),
        status=KnowledgeBaseStatus(str(row["lifecycle_status"])),
        active_index_revision_id=_optional(row["active_revision_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _document(row: sqlite3.Row) -> Document:
    return Document(
        project_id=str(row["project_id"]),
        knowledge_base_id=str(row["knowledge_base_id"]),
        document_id=str(row["document_id"]),
        display_name=str(row["display_name"]),
        status=DocumentStatus(str(row["lifecycle_status"])),
        current_version_id=_optional(row["current_version_id"]),
        active_index_revision_id=_optional(row["active_revision_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _document_version(row: sqlite3.Row) -> DocumentVersion:
    return DocumentVersion(
        document_id=str(row["document_id"]),
        document_version_id=str(row["document_version_id"]),
        content_sha256=str(row["content_sha256"]),
        source_artifact_id=str(row["source_artifact_id"]),
        size_bytes=int(row["size_bytes"]),
        media_type=str(row["media_type"]),
        status=DocumentVersionStatus(str(row["lifecycle_status"])),
        created_at=str(row["created_at"]),
    )


def _job_status(
    state: str,
    cancelled: bool,
    error_code: str | None,
    request_state: str | None,
) -> JobStatus:
    if cancelled or error_code in {"CANCELLED", "JOB_CANCELLED"}:
        return JobStatus.CANCELLED
    if request_state in {"queued", "running"}:
        return (
            JobStatus.QUEUED if request_state == "queued" else JobStatus.RUNNING
        )
    return {
        "pending": JobStatus.QUEUED,
        "running": JobStatus.RUNNING,
        "completed": JobStatus.SUCCEEDED,
        "failed_retryable": JobStatus.FAILED_RETRYABLE,
        "failed_terminal": JobStatus.FAILED_TERMINAL,
        "interrupted": JobStatus.FAILED_RETRYABLE,
    }[state]


def _lexical_identity(value: object) -> tuple[str, str]:
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return "unknown", "unknown"
    if not isinstance(payload, dict):
        return "unknown", "unknown"
    version = str(payload.get("fts_schema_version", "unknown"))
    analyzer = str(payload.get("analyzer_id", "unknown"))
    analyzer_version = str(payload.get("analyzer_version", ""))
    if analyzer_version and not analyzer.endswith(f"-v{analyzer_version}"):
        analyzer = f"{analyzer}-v{analyzer_version}"
    return version, analyzer


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= _MAX_PAGE_SIZE or offset < 0:
        raise ValueError("分页参数超出允许范围。")


def _cancel_scope_jobs(
    connection: sqlite3.Connection,
    *,
    knowledge_base_id: str,
    document_id: str | None,
    now: str,
) -> None:
    """在生命周期删除事务内取消目标 scope 的未完成作业。"""
    connection.execute(
        "UPDATE ingestion_requests SET state='cancelled', updated_at=? "
        "WHERE state='queued' AND job_id IN (SELECT job_id FROM "
        "ingestion_jobs WHERE knowledge_base_id=? "
        "AND (? IS NULL OR document_id=?) AND state='pending')",
        (now, knowledge_base_id, document_id, document_id),
    )
    connection.execute(
        "UPDATE ingestion_jobs SET cancel_requested=1, "
        "state=CASE WHEN state='pending' THEN 'failed_terminal' "
        "ELSE state END, "
        "stage=CASE WHEN state='pending' THEN 'cancelled' ELSE stage END, "
        "error_code=CASE WHEN state='pending' THEN 'JOB_CANCELLED' "
        "ELSE error_code END, "
        "safe_message=CASE WHEN state='pending' THEN '作业已取消。' "
        "ELSE safe_message END, retryable=0, updated_at=?, "
        "finished_at=CASE WHEN state='pending' THEN ? ELSE finished_at END "
        "WHERE knowledge_base_id=? AND (? IS NULL OR document_id=?) "
        "AND state IN ('pending', 'running')",
        (now, now, knowledge_base_id, document_id, document_id),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["SqliteLifecycleStore"]
