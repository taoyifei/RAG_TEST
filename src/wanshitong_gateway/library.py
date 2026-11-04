"""以独立知识库隔离公开、人力与个人资料，复用原生解析和检索。"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)

from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.compat.adapters.stores.sqlite_connection import (
    SqliteConnectionFactory,
)
from wanshitong_gateway.compat.core.errors import ProviderUnavailable
from wanshitong_gateway.document_catalogue import (
    DocumentCatalogue,
    guide_group,
    infer_kind,
)
from wanshitong_gateway.library_quota import (
    ACTIVE_STATES,
    MAX_FILE_BYTES,
    SUBMISSION_LEASE_SECONDS,
    PersonalJob,
    PersonalQuotaStore,
    native_pending,
    native_revision,
)
from wanshitong_gateway.models import SEARCH_SCOPE_ORDER, SearchScope
from wanshitong_gateway.roles import UserProfile, can_access_hr_internal
from wanshitong_gateway.weknora.admin import (
    NativeAdminClient,
    NativeAdminResponse,
)
from wanshitong_gateway.weknora.client import NativeHttpError

LibraryScope = Literal["personal", "public", "hr_public", "hr_internal"]
_MAX_FILE_BYTES = MAX_FILE_BYTES
_PAGE_SIZE = 100
_DELETE_TIMEOUT_SECONDS = 30.0
_DELETE_POLL_SECONDS = 0.25
_EXTENSIONS = frozenset({".docx", ".pdf", ".xlsx", ".pptx", ".txt", ".md"})
_TEMPLATE_FIELDS = (
    "chunking_config",
    "embedding_model_id",
    "summary_model_id",
    "vlm_config",
    "question_generation_config",
    "image_processing_config",
    "storage_provider_config",
    "storage_backend_id",
    "vector_store_id",
    "indexing_strategy",
)
_SCOPE_NAMES = {
    "public": "湾小度公共资料",
    "hr_public": "湾小度公开人力制度",
    "hr_internal": "湾小度人力内部资料",
    "personal": "个人资料",
}
_LOGGER = logging.getLogger(__name__)
_PERSONAL_POLL_SECONDS = 2.0
_RETRY_CONFIRMATION_PENDING = "重试已提交，正在等待解析状态更新；原件仍可下载。"


def _pdf_process_config() -> dict[str, Any]:
    """文字版 PDF 采用内置解析器，扫描页的文字识别仍需配置 OCR。"""
    return {
        "parser_engine_rules": [{"file_types": ["pdf"], "engine": "builtin"}]
    }


def _library_chunking_config(config: dict[str, Any]) -> dict[str, Any]:
    """新资料库保留分块及其他格式规则，单独采用内置 PDF 默认规则。"""
    chunking = copy.deepcopy(config)
    other_rules = []
    for rule in chunking.get("parser_engine_rules") or []:
        file_types = [
            file_type
            for file_type in rule.get("file_types", [])
            if file_type.strip().lower().lstrip(".") != "pdf"
        ]
        if file_types:
            other_rules.append({**rule, "file_types": file_types})
    chunking["parser_engine_rules"] = [
        *_pdf_process_config()["parser_engine_rules"],
        *other_rules,
    ]
    return chunking


class LibraryService:
    """保存知识库归属及共享关系，不自行解析文件或建立索引。"""

    def __init__(  # noqa: PLR0913 - 服务依赖与独立能力开关保持显式注入。
        self,
        database_path: Path,
        native: NativeAdminClient,
        public_kb_ids: tuple[str, ...],
        key_fingerprint: bytes,
        *,
        public_scope: Callable[[], tuple[str, ...]] | None = None,
        personal_sharing_enabled: bool = False,
    ) -> None:
        self._connections = SqliteConnectionFactory(database_path)
        self._native = native
        self._initial_public_kb_ids = public_kb_ids
        self._public_scope = public_scope
        self._personal_sharing_enabled = personal_sharing_enabled
        self._key_fingerprint = key_fingerprint
        self._creation_lock = asyncio.Lock()
        self._document_locks: dict[str, asyncio.Lock] = {}
        self._personal_locks: dict[str, asyncio.Lock] = {}
        self._personal_transfers = asyncio.Semaphore(2)
        self._quota = PersonalQuotaStore(database_path)
        self.catalogue = DocumentCatalogue(database_path)
        self._workflow_lock = asyncio.Lock()
        self._personal_worker: asyncio.Task[None] | None = None
        with self._connections.transaction(write=True) as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS library_kbs ("
                "kb_id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
                "owner_id TEXT NOT NULL DEFAULT '', "
                "UNIQUE(scope,owner_id));"
                "CREATE TABLE IF NOT EXISTS library_documents ("
                "native_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
                "owner_name TEXT NOT NULL, source_id TEXT UNIQUE, "
                "created_at TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS library_personal_kbs ("
                "kb_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL);"
                "INSERT OR IGNORE INTO library_personal_kbs(kb_id,owner_id) "
                "SELECT kb_id,owner_id FROM library_kbs WHERE scope='personal';"
                "CREATE TABLE IF NOT EXISTS library_hr_internal_kbs ("
                "kb_id TEXT PRIMARY KEY);"
                "INSERT OR IGNORE INTO library_hr_internal_kbs(kb_id) "
                "SELECT kb_id FROM library_kbs WHERE scope='hr_internal';"
            )

    async def start(self) -> None:
        """启动个人解析队列，原生全局解析并发保持原样。"""
        if self._personal_worker is None or self._personal_worker.done():
            self._personal_worker = asyncio.create_task(self._personal_loop())

    async def close(self) -> None:
        """停止本进程轮询，原件与投递状态保留供下次启动恢复。"""
        task = self._personal_worker
        self._personal_worker = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def personal_limits(self, user_id: str) -> dict[str, int]:
        """额度配置不包含个人资料名称、用量或访问能力。"""
        return self._quota.limits(user_id)

    def set_personal_limits(
        self, user_id: str, max_bytes: int, max_files: int
    ) -> dict[str, int]:
        """调用方核对超管身份后只修改额度，不读取或移除个人原件。"""
        return self._quota.set_limits(user_id, max_bytes, max_files)

    def _personal_lock(self, owner_id: str) -> asyncio.Lock:
        return self._personal_locks.setdefault(owner_id, asyncio.Lock())

    async def _personal_loop(self) -> None:
        while True:
            try:
                owners = self._quota.owner_jobs()
            except ProviderUnavailable:
                _LOGGER.warning("个人资料队列数据库暂忙，稍后继续轮询。")
                await asyncio.sleep(_PERSONAL_POLL_SECONDS)
                continue
            for owner_id in owners:
                try:
                    await self._advance_personal(owner_id)
                except asyncio.CancelledError:
                    raise
                except (
                    HTTPException,
                    OSError,
                    ValueError,
                    ProviderUnavailable,
                ):
                    # 服务暂不可用时不释放未确认的活动槽，也不重复上传。
                    _LOGGER.warning(
                        "个人资料队列轮询暂未完成，状态及原件已保留。"
                    )
            await asyncio.sleep(_PERSONAL_POLL_SECONDS)

    async def _personal_native_items(
        self, owner_id: str
    ) -> list[dict[str, Any]]:
        kb_ids = [
            row["kb_id"]
            for row in self.registered_kbs()
            if row["scope"] == "personal" and row["owner_id"] == owner_id
        ]
        items: list[dict[str, Any]] = []
        for kb_id in kb_ids:
            page = 1
            while True:
                try:
                    payload = await self._request(
                        "GET",
                        f"knowledge-bases/{kb_id}/knowledge",
                        query=f"page_size={_PAGE_SIZE}&page={page}",
                    )
                except HTTPException as error:
                    if error.status_code == HTTPStatus.NOT_FOUND:
                        break
                    raise
                batch = payload.get("data")
                if not isinstance(batch, list) or any(
                    not isinstance(item, dict) or not item.get("id")
                    for item in batch
                ):
                    raise HTTPException(502, "个人资料列表格式无效。")
                items.extend(batch)
                if len(batch) < _PAGE_SIZE or page * _PAGE_SIZE >= payload.get(
                    "total", page * _PAGE_SIZE + 1
                ):
                    break
                page += 1
        return items

    @staticmethod
    def _upload_marker(item: dict[str, Any]) -> str | None:
        metadata = item.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                return None
        if not isinstance(metadata, dict):
            return None
        marker = metadata.get("wxd_upload_id")
        return marker if isinstance(marker, str) else None

    def _spool_file(self, job_id: str) -> Path:
        return self._quota.spool_path / job_id

    def _remove_spool(self, job_id: str) -> None:
        self._spool_file(job_id).unlink(missing_ok=True)
        self._spool_file(job_id).with_suffix(".part").unlink(missing_ok=True)

    async def _reconcile_personal(
        self, owner_id: str, native_items: list[dict[str, Any]]
    ) -> None:
        by_id = {str(item["id"]): item for item in native_items}
        markers = {self._upload_marker(item): item for item in native_items}
        for job in self._quota.jobs(owner_id):
            if job.state == "staging":
                self._recover_staging(job)
                continue
            item = by_id.get(job.native_id or "") or markers.get(job.job_id)
            if item is not None:
                self._reconcile_personal_item(job, item)
                continue
            if job.native_id is not None:
                try:
                    item = await self._knowledge(job.native_id)
                except HTTPException as error:
                    if error.status_code != HTTPStatus.NOT_FOUND:
                        raise
                    for identifier in self._quota.mark_deleted(
                        job.native_id, owner_id
                    ):
                        self._remove_spool(identifier)
                    continue
                if job.state in ACTIVE_STATES:
                    self._reconcile_personal_item(job, item)
            elif job.state == "submitting" and time.time() >= job.lease_until:
                self._quota.set_state(
                    job.job_id,
                    "uncertain",
                    error="原件投递结果待确认，已暂停本人的后续解析；原件已保留，请稍后重试。",
                )

    def _reconcile_personal_item(
        self, job: PersonalJob, item: dict[str, Any]
    ) -> None:
        if self._unconfirmed_retry(job, item):
            return
        if job.native_id is None or job.state in ACTIVE_STATES:
            self._quota.accept_native(job.job_id, item)
            self.catalogue.adopt(job.job_id, str(item["id"]))
            if job.kind == "upload" and job.native_id is None:
                self._remember(str(item["id"]), self._job_profile(job))
            self._remove_spool(job.job_id)

    def _unconfirmed_retry(
        self, job: PersonalJob, item: dict[str, Any]
    ) -> bool:
        if job.kind != "retry" or job.state not in {"submitting", "uncertain"}:
            return False
        if native_pending(item) or (
            job.native_revision and native_revision(item) != job.native_revision
        ):
            return False
        if time.time() >= job.lease_until and (
            job.state == "submitting"
            or job.error == _RETRY_CONFIRMATION_PENDING
        ):
            self._quota.set_state(
                job.job_id,
                "uncertain",
                error="重试结果待确认，原件仍可下载；已暂停后续解析，请稍后重试。",
            )
        return True

    def _recover_staging(self, job: PersonalJob) -> None:
        if time.time() - job.updated_at < SUBMISSION_LEASE_SECONDS:
            return
        path = self._spool_file(job.job_id)
        if path.is_file() and path.stat().st_size == job.size_bytes:
            self._quota.set_state(job.job_id, "queued")
        else:
            self._quota.forget_staging(job.job_id)
            self.catalogue.forget(job.job_id)
            self._remove_spool(job.job_id)

    async def _advance_personal(
        self, owner_id: str, *, wait_for_transfer: bool = True
    ) -> None:
        async with self._personal_lock(owner_id):
            items = await self._personal_native_items(owner_id)
            await self._reconcile_personal(owner_id, items)
            if not wait_for_transfer and self._personal_transfers.locked():
                return
            async with self._personal_transfers:
                job = self._quota.claim_next(owner_id, items)
                if job is not None:
                    await self._submit_personal(job)

    @staticmethod
    def _job_profile(job: PersonalJob) -> UserProfile:
        # 个人资料权限只依赖稳定的本人 ID，排队期间切换角色不扩大范围。
        return UserProfile(
            job.owner_id, job.owner_id, job.owner_name, None, "user"
        )

    async def _submit_personal(self, job: PersonalJob) -> None:
        sent = False
        try:
            if job.kind == "upload":
                kb_id = await self._scope_kb("personal", self._job_profile(job))
                content = await asyncio.to_thread(
                    self._spool_file(job.job_id).read_bytes
                )
                multipart = self._multipart(
                    job.filename,
                    content,
                    self._job_profile(job),
                    upload_id=job.job_id,
                    replaces_id=self.catalogue.entry(job.job_id).get(
                        "replaces_id"
                    )
                    or None,
                )
                method_path = f"knowledge-bases/{kb_id}/knowledge/file"
                request_content = multipart.read()
                content_type = multipart.headers["content-type"]
            else:
                item, scope = await self.document(
                    str(job.native_id), self._job_profile(job), write=True
                )
                if scope["scope"] != "personal":
                    raise HTTPException(403, "只能处理本人的个人资料。")
                kb_id = str(item["knowledge_base_id"])
                self._quota.set_retry_revision(job.job_id, item)
                method_path = (
                    f"knowledge/{quote(str(job.native_id), safe='')}/reparse"
                )
                request_content, content_type = b"{}", "application/json"
            sent = True
            response = await self._native.proxy(
                method="POST",
                path="api/v1/" + method_path,
                query="",
                content=request_content,
                browser_headers={"Content-Type": content_type},
            )
            accepted_item = await self._personal_receipt(job, response, kb_id)
            if accepted_item is None:
                return
            current = self._quota.job(job.job_id)
            if current is not None and self._unconfirmed_retry(
                current, accepted_item
            ):
                self._quota.set_state(
                    job.job_id, "uncertain", error=_RETRY_CONFIRMATION_PENDING
                )
                return
            self._quota.accept_native(job.job_id, accepted_item)
            self.catalogue.adopt(job.job_id, str(accepted_item["id"]))
            if job.kind == "upload":
                self._remember(str(accepted_item["id"]), self._job_profile(job))
                self._remove_spool(job.job_id)
        except asyncio.CancelledError:
            if sent:
                self._quota.set_state(
                    job.job_id,
                    "uncertain",
                    error="投递中断，原件已保留，正在核对原生处理结果。",
                )
            else:
                self._quota.set_state(job.job_id, "queued")
            raise
        except (HTTPException, NativeHttpError, OSError, ValueError):
            self._quota.set_state(
                job.job_id,
                "uncertain" if sent else "failed",
                error="原件投递结果待确认，已暂停本人的后续解析；原件已保留，请稍后重试。"
                if sent
                else "原件暂未提交解析，请稍后重试；原件已保留。",
            )

    async def _personal_receipt(
        self, job: PersonalJob, response: NativeAdminResponse, kb_id: str
    ) -> dict[str, Any] | None:
        """只有明确的原生接收回执才释放本地原件；不猜测错误响应。"""
        if response.stream is not None:
            await response.stream.aclose()
            raise ValueError("原生上传响应不应为流。")
        try:
            payload = json.loads(response.content)
        except ValueError:
            payload = None
        item = payload.get("data") if isinstance(payload, dict) else None
        if response.status_code < HTTPStatus.BAD_REQUEST and (
            not isinstance(payload, dict) or payload.get("success") is False
        ):
            raise ValueError("原生投递结果未确认。")
        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise ValueError("原生投递结果未确认。")
        if response.status_code >= HTTPStatus.BAD_REQUEST and not (
            response.status_code == HTTPStatus.CONFLICT
            and isinstance(item, dict)
            and (
                self._upload_marker(item) == job.job_id
                or (
                    payload.get("code") == "duplicate_file"
                    and str(item.get("knowledge_base_id")) == kb_id
                    and self.catalogue.entry(job.job_id).get("state")
                    != "staging"
                )
            )
        ):
            self._quota.set_state(
                job.job_id,
                "failed",
                error="原生资料服务未接受原件，请检查文件后重试；原件已保留。",
            )
            return None
        if (
            not isinstance(item, dict)
            or not item.get("id")
            or str(item.get("knowledge_base_id")) != kb_id
        ):
            raise ValueError("原生原件归属未确认。")
        return item

    @staticmethod
    def _job_status(job: PersonalJob) -> str:
        if job.state == "uncertain":
            return (
                "pending"
                if job.error == _RETRY_CONFIRMATION_PENDING
                else "failed"
            )
        return (
            "pending" if job.state in {"staging", "submitting"} else job.state
        )

    @staticmethod
    def _job_response(job: PersonalJob) -> dict[str, Any]:
        return {
            "id": job.native_id or job.job_id,
            "native_id": job.native_id,
            "status": LibraryService._job_status(job),
        }

    def _job_item(self, job: PersonalJob) -> dict[str, Any]:
        return {
            "id": job.native_id or job.job_id,
            "native_id": job.native_id,
            "title": job.filename,
            "status": self._job_status(job),
            "created_at": datetime.fromtimestamp(
                job.created_at, UTC
            ).isoformat(),
            "size": job.size_bytes,
            "shared": False,
            "owner_id": job.owner_id,
            "owner_name": job.owner_name,
            "error": job.error,
            "can_manage": True,
        }

    @property
    def public_kb_ids(self) -> tuple[str, ...]:
        """读取有效公共绑定，个人及内部库即使误绑定也不能公开。"""
        candidates = (
            self._public_scope()
            if self._public_scope is not None
            else self._initial_public_kb_ids
        )
        private = self.hr_internal_kbs() | self.personal_kbs().keys()
        return tuple(kb_id for kb_id in candidates if kb_id not in private)

    def personal_kbs(self) -> dict[str, str]:
        """保留个人库的历史归属，重建后旧记录也不能被视为公开资料。"""
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT kb_id,owner_id FROM library_personal_kbs UNION ALL "
                "SELECT kb_id,owner_id FROM library_kbs WHERE scope='personal' "
                "AND kb_id NOT IN (SELECT kb_id FROM library_personal_kbs)"
            ).fetchall()
        return {row["kb_id"]: row["owner_id"] for row in rows}

    def hr_internal_kbs(self) -> frozenset[str]:
        """内部区即使误选进公共绑定，也不能按公共资料对外提供。"""
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT kb_id FROM library_hr_internal_kbs UNION "
                "SELECT kb_id FROM library_kbs WHERE scope='hr_internal'"
            ).fetchall()
        return frozenset(row["kb_id"] for row in rows)

    def restricted_kbs(self, profile: UserProfile) -> tuple[str, ...]:
        """后台、记录与导出同样排除他人私库及当前无权访问的内部库。"""
        candidates = (
            self.hr_internal_kbs()
            | self.personal_kbs().keys()
            | {row["kb_id"] for row in self.registered_kbs()}
        )
        return tuple(
            sorted(
                kb_id
                for kb_id in candidates
                if not self.can_read_kb(kb_id, profile)
            )
        )

    @staticmethod
    def can_manage_scope(scope: LibraryScope, profile: UserProfile) -> bool:
        """资料区的写权限不由上传人或原生管理员身份推导。"""
        if scope == "personal":
            return True
        if scope == "hr_internal":
            return can_access_hr_internal(profile)
        if scope == "hr_public":
            return profile.role in {"admin", "hr"} or profile.is_super_admin
        return profile.role == "admin" or profile.is_super_admin

    @staticmethod
    def _can_read_scope(scope: dict[str, Any], profile: UserProfile) -> bool:
        if scope["scope"] == "personal":
            return bool(scope["owner_id"] == profile.user_id)
        if scope["scope"] == "hr_internal":
            return can_access_hr_internal(profile)
        return scope["scope"] in {"public", "hr_public"}

    def can_read_kb(self, kb_id: str, profile: UserProfile) -> bool:
        """库归属优先于公共绑定，超级管理员也没有他人私库读取特权。"""
        scope = self._scope_for_kb(kb_id)
        return scope is not None and self._can_read_scope(scope, profile)

    def can_manage_kb(self, kb_id: str, profile: UserProfile) -> bool:
        """读写使用同一归属，只有本人可以维护个人库。"""
        scope = self._scope_for_kb(kb_id)
        return (
            scope is not None
            and self._can_read_scope(scope, profile)
            and self.can_manage_scope(scope["scope"], profile)
        )

    @staticmethod
    def available_search_scopes(
        profile: UserProfile,
    ) -> list[dict[str, str]]:
        """只公布当前账号可以选择的资料区，不接受浏览器传入库ID。"""
        scopes = [
            {"id": "public", "label": "公共知识库"},
            {"id": "hr_public", "label": "公开人力制度"},
            {"id": "personal", "label": "我的资料"},
        ]
        if can_access_hr_internal(profile):
            scopes.append({"id": "hr_internal", "label": "人力内部资料"})
        return scopes

    def search_kbs(
        self, profile: UserProfile, scopes: tuple[str, ...]
    ) -> tuple[str, ...]:
        """把已授权的逻辑范围转换成实际知识库，只缩小检索集合。"""
        if not scopes or not set(scopes).issubset(SEARCH_SCOPE_ORDER):
            raise HTTPException(422, "请选择有效的资料范围。")
        if "hr_internal" in scopes and not can_access_hr_internal(profile):
            raise HTTPException(403, "人力内部资料仅限人力和超级管理员访问。")
        values = list(self.public_kb_ids) if "public" in scopes else []
        for row in self.registered_kbs():
            kind = row["scope"]
            if self._can_read_scope(row, profile) and (
                (kind in {"public", "hr_public"} and "public" in scopes)
                or (kind == "hr_public" and "hr_public" in scopes)
                or (
                    kind == "personal"
                    and "personal" in scopes
                    and row["owner_id"] == profile.user_id
                )
                or (kind == "hr_internal" and "hr_internal" in scopes)
            ):
                values.append(row["kb_id"])
        return tuple(dict.fromkeys(values))

    def historical_search_scopes(
        self, turns: list[dict[str, Any]]
    ) -> tuple[SearchScope, ...]:
        """旧会话按记录中的实际库恢复范围，避免默默缩小后复用旧上下文。"""
        selected: set[str] = set()
        for turn in turns:
            for kb_id in json.loads(turn.get("kb_scope_json") or "[]"):
                record = self._scope_for_kb(kb_id)
                if record is not None:
                    kind = record["scope"]
                    selected.add("public" if kind == "hr_public" else kind)
        return tuple(scope for scope in SEARCH_SCOPE_ORDER if scope in selected)

    async def require_searchable(
        self, kb_ids: tuple[str, ...], scopes: tuple[str, ...]
    ) -> None:
        """独立私库或人力区尚无已解析资料时提示，不扩大到公共区。"""
        if not kb_ids:
            raise HTTPException(
                409, "所选资料范围暂无资料，请先上传或选择其他范围。"
            )
        if "public" in scopes:
            return
        for kb_id in kb_ids:
            page = 1
            while True:
                result = await self._request(
                    "GET",
                    f"knowledge-bases/{kb_id}/knowledge",
                    query=f"page_size={_PAGE_SIZE}&page={page}",
                )
                items = result.get("data", [])
                if any(
                    item.get("parse_status") == "completed" for item in items
                ):
                    return
                if len(items) < _PAGE_SIZE:
                    break
                page += 1
        raise HTTPException(
            409, "所选资料尚无解析完成的文档，请完成上传解析后再提问。"
        )

    def registered_kbs(self) -> list[dict[str, Any]]:
        """读取网关创建的知识库归属，不从浏览器接收任意知识库 ID。"""
        with self._connections.transaction() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT kb_id,scope,owner_id FROM library_kbs"
                )
            ]

    def readable_kbs(self, profile: UserProfile) -> tuple[str, ...]:
        """每次问答重新核对资料归属，所有角色都只能检索本人私库。"""
        values = list(self.public_kb_ids)
        values.extend(
            row["kb_id"]
            for row in self.registered_kbs()
            if self._can_read_scope(row, profile)
        )
        return tuple(dict.fromkeys(values))

    def check_turn(self, profile: UserProfile, turn: dict[str, Any]) -> None:
        """角色降级后不能通过旧会话、引用或图片重新访问人力内部资料。"""
        scope = json.loads(turn.get("kb_scope_json") or "[]")
        if not set(scope).issubset(self.readable_kbs(profile)):
            raise HTTPException(403, "该会话的资料范围已变化，请新建问题。")

    def check_history(
        self,
        profile: UserProfile,
        turns: list[dict[str, Any]],
    ) -> None:
        """原生续问会读取旧上下文，因此逐轮核对当前权限。"""
        for turn in turns:
            self.check_turn(profile, turn)

    def _scope_for_kb(self, kb_id: str) -> dict[str, Any] | None:
        owner_id = self.personal_kbs().get(kb_id)
        if owner_id is not None:
            return {"kb_id": kb_id, "scope": "personal", "owner_id": owner_id}
        registered = next(
            (row for row in self.registered_kbs() if row["kb_id"] == kb_id),
            None,
        )
        if registered is not None:
            return registered
        if kb_id in self.hr_internal_kbs():
            return {"kb_id": kb_id, "scope": "hr_internal", "owner_id": ""}
        if kb_id in self.public_kb_ids:
            return {"kb_id": kb_id, "scope": "public", "owner_id": ""}
        return None

    def _record(self, native_id: str) -> dict[str, Any] | None:
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM library_documents WHERE native_id=?",
                (native_id,),
            ).fetchone()
        return dict(row) if row else None

    def _shared(self, source_id: str) -> dict[str, Any] | None:
        with self._connections.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM library_documents WHERE source_id=?",
                (source_id,),
            ).fetchone()
        return dict(row) if row else None

    def _remember(
        self,
        native_id: str,
        profile: UserProfile,
        source_id: str | None = None,
    ) -> None:
        previous = self._record(native_id)
        if previous and previous["owner_id"] != profile.user_id:
            raise HTTPException(409, "公共区已存在同一文件，请先使用现有资料。")
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO library_documents "
                "(native_id,owner_id,owner_name,source_id,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    native_id,
                    profile.user_id,
                    profile.display_name,
                    source_id,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def _forget(self, native_id: str) -> None:
        with self._connections.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM library_documents WHERE native_id=?",
                (native_id,),
            )

    async def _native_request(
        self,
        method: str,
        path: str,
        *,
        content: bytes = b"",
        content_type: str = "application/json",
        query: str = "",
    ) -> NativeAdminResponse:
        try:
            response = await self._native.proxy(
                method=method,
                path="api/v1/" + path,
                query=query,
                content=content,
                browser_headers={"Content-Type": content_type},
            )
        except NativeHttpError as error:
            raise _native_error(error) from error
        if response.status_code == HTTPStatus.NOT_FOUND:
            raise HTTPException(404, "资料不存在或已删除。")
        if response.status_code == HTTPStatus.CONFLICT:
            raise HTTPException(409, "目标资料区已有相同文件，请使用现有资料。")
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            raise HTTPException(502, "原生资料服务暂时不可用，请稍后重试。")
        if response.stream is not None:
            await response.stream.aclose()
            raise HTTPException(502, "原生资料响应格式无效。")
        return response

    async def _request(  # noqa: PLR0913 - 原生 JSON 与文件上传复用传输。
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        content: bytes = b"",
        content_type: str = "application/json",
        query: str = "",
    ) -> dict[str, Any]:
        response = await self._native_request(
            method,
            path,
            content=json.dumps(body).encode() if body is not None else content,
            content_type=content_type,
            query=query,
        )
        if not response.content:
            return {}
        try:
            payload = json.loads(response.content)
        except ValueError as error:
            raise HTTPException(502, "原生资料响应格式无效。") from error
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise HTTPException(502, "原生资料操作未完成。")
        return payload

    async def _knowledge(self, native_id: str) -> dict[str, Any]:
        result = await self._request(
            "GET", "knowledge/" + quote(native_id, safe="")
        )
        item = result.get("data")
        if not isinstance(item, dict):
            raise HTTPException(502, "原生资料详情格式无效。")
        return item

    async def document(
        self,
        native_id: str,
        profile: UserProfile,
        *,
        write: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """文件、原件、引用及修改均按资料区和本人归属核对，不绕过超管。"""
        job = self._quota.job(native_id)
        if job is not None:
            if job.owner_id != profile.user_id:
                raise HTTPException(403, "没有访问这份资料的权限。")
            if job.state == "deleted":
                raise HTTPException(404, "资料不存在或已删除。")
            if job.native_id is None:
                return {
                    "id": job.job_id,
                    "file_name": job.filename,
                    "file_size": job.size_bytes,
                    "knowledge_base_id": job.kb_id or "",
                    "parse_status": job.state,
                }, {
                    "scope": "personal",
                    "owner_id": job.owner_id,
                    "kb_id": job.kb_id or "",
                }
            native_id = job.native_id
        item = await self._knowledge(native_id)
        kb_id = str(item.get("knowledge_base_id", ""))
        scope = self._scope_for_kb(kb_id)
        if scope is None:
            raise HTTPException(404, "资料不存在。")
        if not self._can_read_scope(scope, profile):
            raise HTTPException(403, "没有访问这份资料的权限。")
        if write and not self.can_manage_kb(kb_id, profile):
            raise HTTPException(403, "没有维护这份资料的权限。")
        self._restore_version_record(item)
        return item, scope

    def _restore_version_record(self, item: dict[str, Any]) -> None:
        """从原生持久化标记恢复回执中断的新版本，未发布时保持隔离。"""
        metadata = item.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                return
        if not isinstance(metadata, dict) or not metadata.get(
            "wxd_replaces_id"
        ):
            return
        native_id = str(item["id"])
        marker = self._upload_marker(item)
        if marker and self.catalogue.entry(marker):
            self.catalogue.adopt(marker, native_id)
        if not self.catalogue.entry(native_id):
            self.catalogue.stage(
                native_id,
                str(item["knowledge_base_id"]),
                str(metadata["wxd_replaces_id"]),
            )

    async def _scope_kb(
        self,
        scope: LibraryScope,
        profile: UserProfile,
    ) -> str:
        if not self.public_kb_ids:
            raise HTTPException(409, "尚未发布公共资料区，请联系管理员。")
        if scope == "public":
            return self.public_kb_ids[0]
        owner = profile.user_id if scope == "personal" else ""
        async with self._creation_lock:
            existing = next(
                (
                    row
                    for row in self.registered_kbs()
                    if row["scope"] == scope and row["owner_id"] == owner
                ),
                None,
            )
            removed_kb_id = None
            kb_id = str(existing["kb_id"]) if existing else None
            if kb_id is not None:
                try:
                    await self._request("GET", "knowledge-bases/" + kb_id)
                except HTTPException as error:
                    if error.status_code != HTTPStatus.NOT_FOUND:
                        raise
                    removed_kb_id = kb_id
                    kb_id = None
            if kb_id is None:
                kb_id = await self._create_scope_kb(scope, profile, owner)
            # 先完成 Key 迁移，再更新归属；失败保留旧登记供下一次恢复重试。
            try:
                await self._native.allow_key_knowledge_base(
                    self._key_fingerprint, kb_id, removed_kb_id=removed_kb_id
                )
            except NativeHttpError as error:
                raise _native_error(error) from error
            if existing is None or removed_kb_id is not None:
                with self._connections.transaction(write=True) as connection:
                    connection.execute(
                        "INSERT INTO library_kbs(kb_id,scope,owner_id) "
                        "VALUES (?,?,?) ON CONFLICT(scope,owner_id) "
                        "DO UPDATE SET kb_id=excluded.kb_id",
                        (kb_id, scope, owner),
                    )
            return kb_id

    async def _create_scope_kb(
        self,
        scope: LibraryScope,
        profile: UserProfile,
        owner: str,
    ) -> str:
        template = (
            await self._request(
                "GET", "knowledge-bases/" + self.public_kb_ids[0]
            )
        )["data"]
        config = {
            key: template[key]
            for key in _TEMPLATE_FIELDS
            if template.get(key) is not None
        }
        config["chunking_config"] = _library_chunking_config(
            config.get("chunking_config") or {}
        )
        name = _SCOPE_NAMES[scope]
        if owner:
            name = f"{profile.display_name}的个人资料（{owner}）"
        config.update({"name": name, "description": name, "type": "document"})
        created = await self._request("POST", "knowledge-bases", body=config)
        kb_id = created["data"].get("id")
        if not isinstance(kb_id, str) or not kb_id:
            raise HTTPException(502, "原生资料库创建未返回有效 ID。")
        if scope in {"hr_internal", "personal"}:
            with self._connections.transaction(write=True) as connection:
                if scope == "personal":
                    connection.execute(
                        "INSERT OR IGNORE INTO library_personal_kbs"
                        "(kb_id,owner_id) VALUES (?,?)",
                        (kb_id, owner),
                    )
                else:
                    connection.execute(
                        "INSERT OR IGNORE INTO library_hr_internal_kbs(kb_id) "
                        "VALUES (?)",
                        (kb_id,),
                    )
        return kb_id

    @staticmethod
    def _check_scope(scope: LibraryScope, profile: UserProfile) -> None:
        if scope == "hr_internal" and not can_access_hr_internal(profile):
            raise HTTPException(403, "人力内部资料仅限人力和超级管理员访问。")

    async def list_documents(
        self,
        scope: LibraryScope,
        profile: UserProfile,
    ) -> dict[str, Any]:
        """列出当前可见资料及逐文件操作能力，个人入口始终只展示本人资料。"""
        self._check_scope(scope, profile)
        kb_ids = list(self.public_kb_ids) if scope == "public" else []
        for row in self.registered_kbs():
            if (
                row["scope"] == scope
                and (scope != "personal" or row["owner_id"] == profile.user_id)
            ) or (scope == "public" and row["scope"] == "hr_public"):
                kb_ids.append(row["kb_id"])
        items = []
        personal_native: list[dict[str, Any]] = []
        for kb_id in dict.fromkeys(kb_ids):
            page = 1
            while True:
                result = await self._request(
                    "GET",
                    f"knowledge-bases/{kb_id}/knowledge",
                    query=f"page_size={_PAGE_SIZE}&page={page}",
                )
                batch = result.get("data", [])
                if scope == "personal":
                    personal_native.extend(batch)
                for item in batch:
                    self._restore_version_record(item)
                    record = self._record(item["id"])
                    kb_scope = self._scope_for_kb(kb_id)
                    items.append(
                        {
                            "id": item["id"],
                            "title": item.get("file_name")
                            or item.get("title")
                            or "未命名资料",
                            "status": item.get("parse_status", "pending"),
                            "created_at": item.get("created_at", ""),
                            "size": item.get("file_size") or 0,
                            "shared": bool(self._shared(item["id"])),
                            "owner_id": record["owner_id"] if record else None,
                            "owner_name": record["owner_name"]
                            if record
                            else None,
                            "error": "解析失败，请重试。"
                            if item.get("parse_status") == "failed"
                            else None,
                            "can_manage": self.can_manage_kb(kb_id, profile)
                            if kb_scope
                            else False,
                        }
                    )
                if len(batch) < _PAGE_SIZE or page * _PAGE_SIZE >= result.get(
                    "total", page * _PAGE_SIZE + 1
                ):
                    break
                page += 1
        result = {
            "scope": scope,
            "can_manage": self.can_manage_scope(scope, profile),
            "can_share": self._personal_sharing_enabled
            and scope == "personal"
            and self.can_manage_scope("public", profile),
            "items": items,
        }
        if scope == "personal":
            result["quota"] = await self._personal_listing(
                profile.user_id, items, personal_native
            )
        for item in items:
            entry = self.catalogue.entry(item["id"])
            item.update(
                {
                    "version_state": entry.get("state", "current"),
                    "replaces_id": entry.get("replaces_id") or None,
                    "kind": entry.get("kind") or infer_kind(item["title"]),
                    "group_name": entry.get("group_name")
                    or guide_group(item["title"]),
                    "health": json.loads(entry["health_json"])
                    if entry.get("health_json")
                    else None,
                    "assets": self.catalogue.assets(item["id"]),
                }
            )
        return result

    async def searchable_document_ids(
        self, kb_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        """原生知识 ID 过滤快照排除待发布及旧版，不改变检索排序。"""
        async with self._workflow_lock:
            items = await self._items_in_kbs(kb_ids)
            return tuple(
                str(item["id"])
                for item in items
                if (
                    item.get("parse_status") == "completed"
                    and item.get("enable_status", "enabled") == "enabled"
                    and self.catalogue.searchable(str(item["id"]))
                )
            )

    async def _items_in_kbs(
        self, kb_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for kb_id in kb_ids:
            page = 1
            while True:
                payload = await self._request(
                    "GET",
                    f"knowledge-bases/{kb_id}/knowledge",
                    query=f"page_size={_PAGE_SIZE}&page={page}",
                )
                batch = payload.get("data", [])
                for item in batch:
                    self._restore_version_record(item)
                items.extend(batch)
                if len(batch) < _PAGE_SIZE or page * _PAGE_SIZE >= payload.get(
                    "total", page * _PAGE_SIZE + 1
                ):
                    break
                page += 1
        return items

    @staticmethod
    def _same_content(item: dict[str, Any], content: bytes) -> bool:
        expected = str(item.get("file_hash") or "").lower().lstrip("0")
        if not expected:
            return False
        return expected in {
            hashlib.sha256(content).hexdigest().lstrip("0"),
            hashlib.md5(content, usedforsecurity=False).hexdigest().lstrip("0"),
        }

    async def _duplicate_upload(
        self,
        scope: LibraryScope,
        profile: UserProfile,
        content: bytes,
        existing: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        hashes = {
            hashlib.sha256(content).hexdigest().lstrip("0"),
            hashlib.md5(content, usedforsecurity=False).hexdigest().lstrip("0"),
        }
        duplicate = next(
            (
                item
                for item in existing
                if str(item.get("file_hash") or "").lower().lstrip("0")
                in hashes
            ),
            None,
        )
        if duplicate:
            version_state = self.catalogue.entry(str(duplicate["id"])).get(
                "state", "current"
            )
            return {
                "id": duplicate["id"],
                "status": duplicate.get("parse_status", "pending"),
                "duplicate": True,
                "version_state": version_state,
                "message": "这份资料已经存在，已保留现有文件，"
                "没有重复解析或占用额度。"
                if version_state == "current"
                else "相同内容已在已归档旧版中，请切换版本范围查看。"
                if version_state == "archived"
                else "相同内容是待发布的新版本，请等待解析完成后发布。",
            }
        if scope == "personal":
            for job in self._quota.jobs(profile.user_id):
                if (
                    job.kind != "upload"
                    or job.native_id is not None
                    or job.state == "deleted"
                ):
                    continue
                path = self._spool_file(job.job_id)
                if (
                    path.is_file()
                    and await asyncio.to_thread(path.read_bytes) == content
                ):
                    return {
                        **self._job_response(job),
                        "duplicate": True,
                        "message": "相同资料已经在准备中，无需重复上传。",
                    }
        return None

    async def receive_upload(  # noqa: PLR0913 - 区分资料区、认证主体、原件及可选用途/版本。
        self,
        scope: LibraryScope,
        profile: UserProfile,
        filename: str,
        content: bytes,
        *,
        replaces_id: str | None = None,
        kind: str = "",
    ) -> dict[str, Any]:
        """先识别本资料区重复文件；新版本解析合格后才允许显式发布。"""
        self._check_scope(scope, profile)
        if kind and kind not in {"policy", "illustration", "template"}:
            raise HTTPException(422, "资料用途无效。")
        if not self.can_manage_scope(scope, profile):
            raise HTTPException(403, "没有向该资料区新增资料的权限。")
        async with self._workflow_lock:
            if replaces_id:
                previous, zone = await self.document(
                    replaces_id, profile, write=True
                )
                replaces_id = str(previous["id"])
                if zone["scope"] != scope or not self.catalogue.searchable(
                    replaces_id
                ):
                    raise HTTPException(
                        409, "请对同一资料区的当前版本执行更新。"
                    )
                if self.catalogue.pending(replaces_id):
                    raise HTTPException(
                        409, "已有待发布版本，请先处理或删除它。"
                    )
                kb_id = str(previous["knowledge_base_id"])
            else:
                kb_id = await self._scope_kb(scope, profile)
            existing = await self._items_in_kbs((kb_id,))
            duplicate = await self._duplicate_upload(
                scope, profile, content, existing
            )
            if duplicate:
                if replaces_id:
                    raise HTTPException(
                        409, "文件内容与已有资料完全相同，无需更新版本。"
                    )
                return duplicate
            clean_name = filename.replace("\\", "/").rsplit("/", 1)[-1]
            if not replaces_id and any(
                (item.get("file_name") or item.get("title")) == clean_name
                for item in existing
            ):
                raise HTTPException(
                    409,
                    "已有同名资料。内容有更新时，请点击该资料的“更新版本”，"
                    "避免新旧内容混用。",
                )
            result = await self.upload(
                scope, profile, filename, content, replaces_id=replaces_id
            )
            if replaces_id:
                result["version_state"] = "staging"
                result["message"] = (
                    "新版本已接收，原版本继续使用。解析完成并检查通过后，点击“发布此版本”。"
                )
            else:
                result["message"] = (
                    "原件已接收。正在准备解析，状态变为“可问答”后可直接问这份资料。"
                )
            if kind:
                self.catalogue.annotate(str(result["id"]), kb_id, kind=kind)
            return result

    async def check_document_health(
        self, native_id: str, profile: UserProfile
    ) -> dict[str, Any]:
        """验证原件确实能下载且大小一致；解析完成不等于已核验图片效果。"""
        item, zone = await self.document(native_id, profile)
        health: dict[str, Any] = {
            "checked_at": datetime.now(UTC).isoformat(),
            "original_available": False,
            "parse_status": item.get("parse_status"),
            "image_check": "not_checked",
        }
        try:
            raw = await self.download(native_id, profile)
            expected_size = int(item.get("file_size") or 0)
            size_ok = not expected_size or len(raw.content) == expected_size
            hash_ok = not item.get("file_hash") or self._same_content(
                item, raw.content
            )
            health.update(
                {
                    "original_available": True,
                    "size_ok": size_ok,
                    "hash_ok": hash_ok,
                    "bytes": len(raw.content),
                }
            )
        except HTTPException as error:
            if error.status_code not in {
                HTTPStatus.NOT_FOUND,
                HTTPStatus.BAD_GATEWAY,
                HTTPStatus.CONFLICT,
            }:
                raise
            if error.status_code != HTTPStatus.NOT_FOUND:
                health["original_available"] = None
                health["check_error"] = (
                    "原件下载暂时失败，尚未确认是否可用，请稍后重试。"
                )
        health["ready"] = bool(
            health["original_available"]
            and health.get("size_ok")
            and health.get("hash_ok")
            and item.get("parse_status") == "completed"
        )
        self.catalogue.set_health(str(item["id"]), str(zone["kb_id"]), health)
        return health

    async def publish_version(
        self, native_id: str, profile: UserProfile
    ) -> dict[str, Any]:
        """解析及原件检查通过才切换版本，失败时旧版本不受影响。"""
        async with self._workflow_lock:
            item, zone = await self.document(native_id, profile, write=True)
            native_id = str(item["id"])
            entry = self.catalogue.entry(native_id)
            previous_id = str(entry.get("replaces_id") or "")
            if entry.get("state") != "staging" or not previous_id:
                raise HTTPException(409, "这不是待发布的新版本。")
            previous, previous_zone = await self.document(
                previous_id, profile, write=True
            )
            if (
                str(previous["knowledge_base_id"])
                != str(item["knowledge_base_id"])
                or previous_zone != zone
            ):
                raise HTTPException(403, "版本所属资料区不一致，不能发布。")
            health = await self.check_document_health(native_id, profile)
            if not health["ready"]:
                raise HTTPException(
                    409,
                    "新版本尚未解析完成或原件检查未通过，旧版本仍继续使用。",
                )
            try:
                self.catalogue.publish(
                    native_id, str(zone["kb_id"]), previous_id
                )
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            return {
                "id": native_id,
                "version_state": "current",
                "previous_id": previous_id,
            }

    async def add_document_asset(
        self,
        native_id: str,
        profile: UserProfile,
        filename: str,
        content: bytes,
        relation: str,
    ) -> dict[str, Any]:
        """只有该共享资料区的维护者可附真实原始模板，个人附件不绕过额度。"""
        item, zone = await self.document(native_id, profile, write=True)
        if zone["scope"] == "personal":
            raise HTTPException(
                422,
                "个人资料请直接上传文件，附件关联仅用于公共和人力资料的模板及指引。",
            )
        if relation not in {"template", "source_workbook", "full_guide"}:
            raise HTTPException(422, "附件用途无效。")
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if (
            Path(filename).suffix.lower()
            not in {".xlsx", ".xls", ".docx", ".doc", ".pdf"}
            or not content
            or len(content) > MAX_FILE_BYTES
        ):
            raise HTTPException(
                422, "请提供不超过50MB的原始Word、Excel或PDF文件。"
            )
        digest = hashlib.sha256(content).hexdigest()
        existing = next(
            (
                asset
                for asset in self.catalogue.assets(str(item["id"]))
                if asset["sha256"] == digest and asset["relation"] == relation
            ),
            None,
        )
        return existing or self.catalogue.add_asset(
            str(item["id"]), filename, content, digest, relation
        )

    async def document_asset(
        self, native_id: str, asset_id: str, profile: UserProfile
    ) -> NativeAdminResponse:
        """附件下载每次重新验证锚点资料权限，不接受任意资源路径。"""
        item, _ = await self.document(native_id, profile)
        asset = next(
            (
                asset
                for asset in self.catalogue.assets(str(item["id"]))
                if asset["id"] == asset_id
            ),
            None,
        )
        if not asset:
            raise HTTPException(404, "未找到该资料的附件。")
        try:
            content = (
                self.catalogue.asset_root / str(uuid.UUID(asset_id))
            ).read_bytes()
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "附件原件暂不可用。") from None
        return NativeAdminResponse(
            200,
            content,
            {
                "content-type": "application/octet-stream",
                "content-disposition": "attachment; filename*=UTF-8''"
                + quote(asset["filename"], safe=""),
                "Cache-Control": "private, no-store",
            },
        )

    async def _personal_listing(
        self,
        owner_id: str,
        items: list[dict[str, Any]],
        native_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        await self._reconcile_personal(owner_id, native_items)
        known_ids = {str(item["id"]) for item in items}
        for job in self._quota.jobs(owner_id):
            if job.native_id not in known_ids:
                items.append(self._job_item(job))
                known_ids.add(job.native_id or job.job_id)
            elif job.kind == "retry" and job.state in {"queued", "uncertain"}:
                for item in items:
                    if item["id"] == job.native_id:
                        item["status"] = self._job_status(job)
                        item["error"] = job.error
        return self._quota.summary(owner_id, native_items)

    async def upload(  # noqa: PLR0913 - 上传内容及可选原件来源/版本标记保持显式。
        self,
        scope: LibraryScope,
        profile: UserProfile,
        filename: str,
        content: bytes,
        *,
        source_id: str | None = None,
        replaces_id: str | None = None,
    ) -> dict[str, Any]:
        """上传到服务端决定的目标库，共享副本必须由当前用户已授权的原件产生。"""
        if source_id is not None:
            self._require_personal_sharing()
        self._check_scope(scope, profile)
        if not self.can_manage_scope(scope, profile):
            raise HTTPException(403, "没有向该资料区新增资料的权限。")
        filename = (
            filename.replace("\\", "/")
            .rsplit("/", 1)[-1]
            .replace('"', "_")
            .replace("\r", "_")
            .replace("\n", "_")
        )
        if Path(filename).suffix.lower() not in _EXTENSIONS:
            raise HTTPException(
                422, "支持 DOCX、PDF、XLSX、PPTX、TXT 和 MD 文件。"
            )
        if not content or len(content) > _MAX_FILE_BYTES:
            raise HTTPException(413, "文件须非空且不超过 50MB。")
        if scope == "personal":
            async with self._personal_lock(profile.user_id):
                # Key 或私库恢复失败必须在接收原件前返回，避免悄悄变成排队失败。
                kb_id = await self._scope_kb(scope, profile)
                native_items = await self._personal_native_items(
                    profile.user_id
                )
                await self._reconcile_personal(profile.user_id, native_items)
                job = self._quota.reserve_upload(
                    profile.user_id,
                    profile.display_name,
                    filename,
                    len(content),
                    native_items,
                )
                if replaces_id:
                    self.catalogue.stage(job.job_id, kb_id, replaces_id)
                write_task = asyncio.create_task(
                    asyncio.to_thread(self._write_spool, job, content)
                )
                try:
                    await asyncio.shield(write_task)
                except BaseException:
                    # to_thread 取消不等于磁盘线程结束，先等写入结束才归还额度。
                    with contextlib.suppress(OSError):
                        await asyncio.shield(write_task)
                    self._quota.forget_staging(job.job_id)
                    self.catalogue.forget(job.job_id)
                    self._remove_spool(job.job_id)
                    raise
                self._quota.set_state(job.job_id, "queued")
            await self._advance_personal(
                profile.user_id, wait_for_transfer=False
            )
            accepted = self._quota.job(job.job_id)
            if accepted is None:
                raise HTTPException(502, "原件上传状态未确认。")
            return self._job_response(accepted)
        kb_id = await self._scope_kb(scope, profile)
        multipart = self._multipart(
            filename,
            content,
            profile,
            source_id=source_id,
            replaces_id=replaces_id,
        )
        result = await self._request(
            "POST",
            f"knowledge-bases/{kb_id}/knowledge/file",
            content=multipart.read(),
            content_type=multipart.headers["content-type"],
        )
        item = result["data"]
        if replaces_id:
            self.catalogue.stage(str(item["id"]), kb_id, replaces_id)
        self._remember(item["id"], profile, source_id)
        return {"id": item["id"], "status": item.get("parse_status", "pending")}

    def _write_spool(self, job: PersonalJob, content: bytes) -> None:
        final = self._spool_file(job.job_id)
        part = final.with_suffix(".part")
        with part.open("xb") as stream:
            part.chmod(0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        part.replace(final)

    @staticmethod
    def _multipart(  # noqa: PLR0913 - 原生multipart来源与恢复标记必须分别传递。
        filename: str,
        content: bytes,
        profile: UserProfile,
        *,
        source_id: str | None = None,
        upload_id: str | None = None,
        replaces_id: str | None = None,
    ) -> httpx.Request:
        return httpx.Request(
            "POST",
            "http://native.invalid/",
            files={"file": (filename, content)},
            data={
                "fileName": filename,
                # 原生从 multipart 的 process_config 读取并保存文档级覆盖；
                # 已有个人/人力库也可能留有不可用的模板 PDF 规则。
                **(
                    {"process_config": json.dumps(_pdf_process_config())}
                    if Path(filename).suffix.lower() == ".pdf"
                    else {}
                ),
                "metadata": json.dumps(
                    {
                        "wxd_owner_id": profile.user_id,
                        **({"wxd_upload_id": upload_id} if upload_id else {}),
                        **(
                            {"wxd_replaces_id": replaces_id}
                            if replaces_id
                            else {}
                        ),
                        **(
                            {"wxd_source_id": source_id}
                            if source_id is not None
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                ),
            },
        )

    async def download(
        self, native_id: str, profile: UserProfile
    ) -> NativeAdminResponse:
        """原件下载沿用同一资料权限，浏览器无需接触原生管理凭据。"""
        item, _ = await self.document(native_id, profile)
        job = self._quota.job(native_id)
        if job is not None and job.native_id is None:
            if job.state == "staging":
                raise HTTPException(409, "原件尚未上传完成，请稍后下载。")
            try:
                content = await asyncio.to_thread(
                    self._spool_file(job.job_id).read_bytes
                )
            except FileNotFoundError:
                raise HTTPException(404, "原件暂不可用，请稍后重试。") from None
            return NativeAdminResponse(
                200,
                content,
                {
                    "content-type": "application/octet-stream",
                    "content-disposition": "attachment; filename*=UTF-8''"
                    + quote(job.filename, safe=""),
                    "Cache-Control": "private, no-store",
                },
            )
        response = await self._native_request(
            "GET", f"knowledge/{quote(str(item['id']), safe='')}/download"
        )
        if not isinstance(response, NativeAdminResponse):
            raise HTTPException(502, "原生资料下载格式无效。")
        return response

    def _document_lock(self, native_id: str) -> asyncio.Lock:
        record = self._record(native_id)
        source_id = record.get("source_id") if record else None
        lock_id = str(source_id or native_id)
        return self._document_locks.setdefault(lock_id, asyncio.Lock())

    async def delete(self, native_id: str, profile: UserProfile) -> None:
        """删除本人原件不授予公共区写权限，已有公共副本由管理员维护。"""
        _, initial_scope = await self.document(native_id, profile, write=True)
        owner_lock = (
            self._personal_lock(profile.user_id)
            if initial_scope["scope"] == "personal"
            else contextlib.nullcontext()
        )
        async with (
            self._workflow_lock,
            owner_lock,
            self._document_lock(native_id),
        ):
            item, scope = await self.document(native_id, profile, write=True)
            if self.catalogue.pending(str(item["id"])):
                raise HTTPException(
                    409, "这份资料有待发布的新版本，请先处理或删除新版本。"
                )
            job = self._quota.job(native_id)
            if job is not None and job.native_id is None:
                native_items = await self._personal_native_items(
                    profile.user_id
                )
                await self._reconcile_personal(profile.user_id, native_items)
                job = self._quota.job(native_id)
                if job is None or job.state == "deleted":
                    return
                if job.native_id is None:
                    if job.state in {"staging", "submitting", "uncertain"} and (
                        time.time()
                        < (
                            job.updated_at + SUBMISSION_LEASE_SECONDS
                            if job.state == "staging"
                            else job.lease_until
                        )
                        or any(native_pending(entry) for entry in native_items)
                    ):
                        raise HTTPException(
                            409,
                            "原件投递结果正在确认，暂不能删除，请稍后重试。",
                        )
                    for identifier in self._quota.mark_deleted(
                        native_id, profile.user_id
                    ):
                        self._remove_spool(identifier)
                    self.catalogue.forget(native_id)
                    return
                item, scope = await self.document(
                    native_id, profile, write=True
                )
            effective_id = str(item["id"])
            if scope["scope"] == "personal" and any(
                entry.native_id == effective_id
                and entry.state in {"submitting", "uncertain"}
                and time.time() < entry.lease_until
                for entry in self._quota.jobs(profile.user_id)
            ):
                raise HTTPException(
                    409, "原件投递结果正在确认，暂不能删除，请稍后重试。"
                )
            if self._personal_sharing_enabled and self.can_manage_scope(
                "public", profile
            ):
                await self._unshare_locked(effective_id)
            await self._delete_native(effective_id)
            self._forget(effective_id)
            self.catalogue.forget(effective_id)
            self.catalogue.forget(native_id)
            if scope["scope"] == "personal":
                for identifier in self._quota.mark_deleted(
                    effective_id, profile.user_id
                ):
                    self._remove_spool(identifier)

    async def retry(
        self, native_id: str, profile: UserProfile
    ) -> dict[str, Any]:
        """个人重试与上传共用解析槽；公共和人力资料保持原生直接重试。"""
        item, scope = await self.document(native_id, profile, write=True)
        if scope["scope"] != "personal":
            return await self._request(
                "POST",
                f"knowledge/{quote(str(item['id']), safe='')}/reparse",
                body={},
            )
        async with self._personal_lock(profile.user_id):
            native_items = await self._personal_native_items(profile.user_id)
            await self._reconcile_personal(profile.user_id, native_items)
            job = self._quota.job(native_id)
            if job is None:
                job = next(
                    (
                        entry
                        for entry in self._quota.jobs(profile.user_id)
                        if entry.native_id == str(item["id"])
                        and entry.kind == "retry"
                        and entry.state == "uncertain"
                    ),
                    None,
                )
            if (
                job is not None
                and job.kind == "retry"
                and job.state == "uncertain"
            ):
                self._quota.reset_retry(job, native_items)
                queued = job
            elif job is not None and job.native_id is None:
                if not self._spool_file(job.job_id).is_file():
                    raise HTTPException(404, "暂存原件不存在，请重新上传。")
                self._quota.reset_retry(job, native_items)
                queued = job
            else:
                item, _ = await self.document(native_id, profile, write=True)
                queued = self._quota.reserve_retry(
                    profile.user_id, profile.display_name, item, native_items
                )
        await self._advance_personal(profile.user_id, wait_for_transfer=False)
        accepted = self._quota.job(queued.job_id)
        if accepted is None:
            raise HTTPException(502, "重试状态未确认。")
        return self._job_response(accepted)

    async def share(
        self, native_id: str, profile: UserProfile
    ) -> dict[str, Any]:
        """显式发布个人原件副本，不扩大整个个人知识库的权限。"""
        self._require_personal_sharing()
        if not self.can_manage_scope("public", profile):
            raise HTTPException(403, "只有管理员和超级管理员可以发布公共资料。")
        async with self._document_lock(native_id):
            item, scope = await self.document(native_id, profile, write=True)
            if scope["scope"] != "personal":
                raise HTTPException(422, "只有个人资料需要主动共享。")
            previous = self._shared(native_id)
            if previous:
                return {"id": previous["native_id"], "shared": True}
            raw = await self.download(native_id, profile)
            published = await self.upload(
                "public",
                profile,
                item.get("file_name") or item["title"],
                raw.content,
                source_id=native_id,
            )
            return {**published, "shared": True}

    async def unshare(self, native_id: str, profile: UserProfile) -> None:
        """撤回共享保留原件，已被阅读的回答不作伪造性抹除。"""
        self._require_personal_sharing()
        if not self.can_manage_scope("public", profile):
            raise HTTPException(403, "只有管理员和超级管理员可以撤回公共资料。")
        async with self._document_lock(native_id):
            await self.document(native_id, profile, write=True)
            await self._unshare_locked(native_id)

    def _require_personal_sharing(self) -> None:
        """共享暂不开放，保留实现供以后显式启用。"""
        if not self._personal_sharing_enabled:
            raise HTTPException(403, "个人资料共享暂未开放。")

    async def _delete_native(self, native_id: str) -> None:
        """等待原生异步删除实际完成，提交任务不等于撤回已生效。"""
        try:
            await self._request(
                "DELETE", "knowledge/" + quote(native_id, safe="")
            )
        except HTTPException as error:
            if error.status_code == HTTPStatus.NOT_FOUND:
                return
            raise
        deadline = time.monotonic() + _DELETE_TIMEOUT_SECONDS
        while True:
            try:
                await self._knowledge(native_id)
            except HTTPException as error:
                if error.status_code == HTTPStatus.NOT_FOUND:
                    return
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(502, "撤回仍处理中，请稍后重试。")
            await asyncio.sleep(min(_DELETE_POLL_SECONDS, remaining))

    async def _unshare_locked(self, native_id: str) -> None:
        shared = self._shared(native_id)
        if shared:
            await self._delete_native(shared["native_id"])
            self._forget(shared["native_id"])


def _native_error(error: NativeHttpError) -> HTTPException:
    if error.status_code == HTTPStatus.NOT_FOUND:
        return HTTPException(404, "资料不存在或已删除。")
    return HTTPException(502, "原生资料服务暂时不可用，请稍后重试。")


def register_library_routes(
    app: FastAPI, auth: GatewayAuth, service: LibraryService
) -> None:
    """注册员工资料工作台；角色来自受信会话与网关角色表。"""

    @app.get("/api/public/library")
    async def list_library(
        request: Request, scope: LibraryScope = "personal"
    ) -> dict[str, Any]:
        return await service.list_documents(
            scope, auth.require_profile(request)
        )

    @app.post("/api/public/library")
    async def upload_library(
        request: Request,
        file: Annotated[UploadFile, File()],
        scope: Annotated[LibraryScope, Form()] = "personal",
        replaces_id: Annotated[str | None, Form()] = None,
        kind: Annotated[
            Literal["policy", "illustration", "template"] | None, Form()
        ] = None,
    ) -> dict[str, Any]:
        profile = auth.require_profile(request)
        try:
            content = await file.read(_MAX_FILE_BYTES + 1)
        finally:
            await file.close()
        return await service.receive_upload(
            scope,
            profile,
            file.filename or "",
            content,
            replaces_id=replaces_id,
            kind=kind or "",
        )

    @app.post("/api/public/library/{native_id}/health")
    async def library_health(
        native_id: str, request: Request
    ) -> dict[str, Any]:
        return await service.check_document_health(
            native_id, auth.require_profile(request)
        )

    @app.get("/api/public/library/{native_id}/details")
    async def library_details(
        native_id: str, request: Request
    ) -> dict[str, Any]:
        item, _ = await service.document(
            native_id, auth.require_profile(request)
        )
        identifier = str(item["id"])
        entry = service.catalogue.entry(identifier)
        title = item.get("file_name") or item.get("title") or "未命名资料"
        return {
            "id": identifier,
            "kind": entry.get("kind") or infer_kind(title),
            "version_state": entry.get("state", "current"),
            "group_name": entry.get("group_name") or guide_group(title),
            "assets": service.catalogue.assets(identifier),
            "health": json.loads(entry["health_json"])
            if entry.get("health_json")
            else None,
        }

    @app.post("/api/public/library/{native_id}/publish")
    async def publish_library_version(
        native_id: str, request: Request
    ) -> dict[str, Any]:
        return await service.publish_version(
            native_id, auth.require_profile(request)
        )

    @app.post("/api/public/library/{native_id}/assets")
    async def upload_library_asset(
        request: Request,
        native_id: str,
        file: Annotated[UploadFile, File()],
        relation: Annotated[str, Form()] = "template",
    ) -> dict[str, Any]:
        try:
            content = await file.read(MAX_FILE_BYTES + 1)
        finally:
            await file.close()
        return await service.add_document_asset(
            native_id,
            auth.require_profile(request),
            file.filename or "",
            content,
            relation,
        )

    @app.get("/api/public/library/{native_id}/assets/{asset_id}")
    async def download_library_asset(
        native_id: str, asset_id: str, request: Request
    ) -> Response:
        upstream = await service.document_asset(
            native_id, asset_id, auth.require_profile(request)
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=upstream.headers,
        )

    @app.post("/api/public/library/{native_id}/asset-links")
    async def link_library_asset(
        native_id: str, request: Request, source_id: str, asset_id: str
    ) -> dict[str, bool]:
        profile = auth.require_profile(request)
        item, zone = await service.document(native_id, profile, write=True)
        source, source_zone = await service.document(
            source_id, profile, write=True
        )
        if zone["scope"] == "personal" or zone["kb_id"] != source_zone["kb_id"]:
            raise HTTPException(403, "附件仅可关联到同一公共或人力资料区。")
        if not any(
            asset["id"] == asset_id
            for asset in service.catalogue.assets(str(source["id"]))
        ):
            raise HTTPException(404, "原资料没有这份附件。")
        service.catalogue.link_asset(str(item["id"]), asset_id)
        return {"success": True}

    @app.get("/api/public/library/{native_id}/download")
    async def download_library(native_id: str, request: Request) -> Response:
        upstream = await service.download(
            native_id, auth.require_profile(request)
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=upstream.headers,
        )

    @app.delete("/api/public/library/{native_id}")
    async def delete_library(
        native_id: str, request: Request
    ) -> dict[str, bool]:
        await service.delete(native_id, auth.require_profile(request))
        return {"success": True}

    @app.post("/api/public/library/{native_id}/retry")
    async def retry_library(native_id: str, request: Request) -> dict[str, Any]:
        return await service.retry(native_id, auth.require_profile(request))

    @app.post("/api/public/library/{native_id}/share")
    async def share_library(native_id: str, request: Request) -> dict[str, Any]:
        return await service.share(native_id, auth.require_profile(request))

    @app.delete("/api/public/library/{native_id}/share")
    async def unshare_library(
        native_id: str, request: Request
    ) -> dict[str, bool]:
        await service.unshare(native_id, auth.require_profile(request))
        return {"success": True}
