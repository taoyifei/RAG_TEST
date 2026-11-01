"""以独立知识库隔离公开、人力与个人资料，复用原生解析和检索。"""

from __future__ import annotations

import asyncio
import copy
import json
import time
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
from wanshitong_gateway.models import SEARCH_SCOPE_ORDER, SearchScope
from wanshitong_gateway.roles import UserProfile, can_access_hr_internal
from wanshitong_gateway.weknora.admin import (
    NativeAdminClient,
    NativeAdminResponse,
)
from wanshitong_gateway.weknora.client import NativeHttpError

LibraryScope = Literal["personal", "public", "hr_public", "hr_internal"]
_MAX_FILE_BYTES = 50 * 1024 * 1024
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

    def __init__(
        self,
        database_path: Path,
        native: NativeAdminClient,
        public_kb_ids: tuple[str, ...],
        key_fingerprint: bytes,
        *,
        public_scope: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self._connections = SqliteConnectionFactory(database_path)
        self._native = native
        self._initial_public_kb_ids = public_kb_ids
        self._public_scope = public_scope
        self._key_fingerprint = key_fingerprint
        self._creation_lock = asyncio.Lock()
        self._document_locks: dict[str, asyncio.Lock] = {}
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
                "CREATE TABLE IF NOT EXISTS library_hr_internal_kbs ("
                "kb_id TEXT PRIMARY KEY);"
                "INSERT OR IGNORE INTO library_hr_internal_kbs(kb_id) "
                "SELECT kb_id FROM library_kbs WHERE scope='hr_internal';"
            )

    @property
    def public_kb_ids(self) -> tuple[str, ...]:
        """读取当前已发布的公共范围，避免继续使用已撤销的旧绑定。"""
        candidates = (
            self._public_scope()
            if self._public_scope is not None
            else self._initial_public_kb_ids
        )
        internal = self.hr_internal_kbs()
        return tuple(kb_id for kb_id in candidates if kb_id not in internal)

    def hr_internal_kbs(self) -> frozenset[str]:
        """内部区即使误选进公共绑定，也不能按公共资料对外提供。"""
        with self._connections.transaction() as connection:
            rows = connection.execute(
                "SELECT kb_id FROM library_hr_internal_kbs UNION "
                "SELECT kb_id FROM library_kbs WHERE scope='hr_internal'"
            ).fetchall()
        return frozenset(row["kb_id"] for row in rows)

    def restricted_kbs(self, profile: UserProfile) -> tuple[str, ...]:
        """普通管理员也不能通过后台记录读取人力内部资料。"""
        return (
            ()
            if can_access_hr_internal(profile)
            else tuple(sorted(self.hr_internal_kbs()))
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
            if (
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
        """每次问答根据当前角色确定检索范围，管理员问答也默认只用本人私库。"""
        values = list(self.public_kb_ids)
        for row in self.registered_kbs():
            scope = row["scope"]
            if (
                scope in {"public", "hr_public"}
                or (scope == "personal" and row["owner_id"] == profile.user_id)
                or (scope == "hr_internal" and can_access_hr_internal(profile))
            ):
                values.append(row["kb_id"])
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
        if kb_id in self.public_kb_ids:
            return {"kb_id": kb_id, "scope": "public", "owner_id": ""}
        return next(
            (row for row in self.registered_kbs() if row["kb_id"] == kb_id),
            None,
        )

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
        """所有文件操作先校验知识库归属，系统管理员可管理已登记的个人库。"""
        item = await self._knowledge(native_id)
        kb_id = str(item.get("knowledge_base_id", ""))
        scope = self._scope_for_kb(kb_id)
        if scope is None:
            raise HTTPException(404, "资料不存在。")
        if scope["scope"] == "hr_internal":
            self._check_scope("hr_internal", profile)
        if profile.role != "admin" and kb_id not in self.readable_kbs(profile):
            raise HTTPException(403, "没有访问这份资料的权限。")
        if write and not self._can_manage(item, scope, profile):
            raise HTTPException(403, "只能维护自己的资料或授权的人力资料。")
        return item, scope

    def _can_manage(
        self,
        item: dict[str, Any],
        scope: dict[str, Any],
        profile: UserProfile,
    ) -> bool:
        if scope["scope"] == "hr_internal":
            return can_access_hr_internal(profile)
        if profile.role == "admin":
            return True
        if scope["scope"] in {"hr_public", "hr_internal"}:
            return profile.role == "hr"
        if scope["scope"] == "personal":
            return bool(scope["owner_id"] == profile.user_id)
        record = self._record(item["id"])
        return bool(record and record["owner_id"] == profile.user_id)

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
        if scope == "hr_internal":
            with self._connections.transaction(write=True) as connection:
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
        if scope == "hr_public" and profile.role not in {
            "admin",
            "hr",
        }:
            raise HTTPException(403, "该资料区仅供人力角色使用。")

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
        for kb_id in dict.fromkeys(kb_ids):
            page = 1
            while True:
                result = await self._request(
                    "GET",
                    f"knowledge-bases/{kb_id}/knowledge",
                    query=f"page_size={_PAGE_SIZE}&page={page}",
                )
                batch = result.get("data", [])
                for item in batch:
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
                            "can_manage": self._can_manage(
                                item, kb_scope, profile
                            )
                            if kb_scope
                            else False,
                        }
                    )
                if len(batch) < _PAGE_SIZE or page * _PAGE_SIZE >= result.get(
                    "total", page * _PAGE_SIZE + 1
                ):
                    break
                page += 1
        return {
            "scope": scope,
            "can_manage": scope == "personal"
            or profile.role == "admin"
            or (profile.role == "hr" and scope in {"hr_public", "hr_internal"}),
            "items": items,
        }

    async def upload(
        self,
        scope: LibraryScope,
        profile: UserProfile,
        filename: str,
        content: bytes,
        *,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """上传到服务端决定的目标库，共享副本必须由当前用户已授权的原件产生。"""
        self._check_scope(scope, profile)
        if scope == "public" and profile.role != "admin" and source_id is None:
            raise HTTPException(403, "请先上传个人资料，再主动共享给全员。")
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
        kb_id = await self._scope_kb(scope, profile)
        multipart = httpx.Request(
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
        result = await self._request(
            "POST",
            f"knowledge-bases/{kb_id}/knowledge/file",
            content=multipart.read(),
            content_type=multipart.headers["content-type"],
        )
        item = result["data"]
        self._remember(item["id"], profile, source_id)
        return {"id": item["id"], "status": item.get("parse_status", "pending")}

    async def download(
        self, native_id: str, profile: UserProfile
    ) -> NativeAdminResponse:
        """原件下载沿用同一资料权限，浏览器无需接触原生管理凭据。"""
        await self.document(native_id, profile)
        response = await self._native_request(
            "GET", f"knowledge/{quote(native_id, safe='')}/download"
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
        """删除原件时撤回公开副本，与共享操作使用同一把原件锁。"""
        async with self._document_lock(native_id):
            await self.document(native_id, profile, write=True)
            await self._unshare_locked(native_id)
            await self._delete_native(native_id)
            self._forget(native_id)

    async def share(
        self, native_id: str, profile: UserProfile
    ) -> dict[str, Any]:
        """显式发布个人原件副本，不扩大整个个人知识库的权限。"""
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
        async with self._document_lock(native_id):
            await self.document(native_id, profile, write=True)
            await self._unshare_locked(native_id)

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
    ) -> dict[str, Any]:
        profile = auth.require_profile(request)
        try:
            content = await file.read(_MAX_FILE_BYTES + 1)
        finally:
            await file.close()
        return await service.upload(
            scope, profile, file.filename or "", content
        )

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
        await service.document(
            native_id, auth.require_profile(request), write=True
        )
        return await service._request(
            "POST", f"knowledge/{quote(native_id, safe='')}/reparse", body={}
        )

    @app.post("/api/public/library/{native_id}/share")
    async def share_library(native_id: str, request: Request) -> dict[str, Any]:
        return await service.share(native_id, auth.require_profile(request))

    @app.delete("/api/public/library/{native_id}/share")
    async def unshare_library(
        native_id: str, request: Request
    ) -> dict[str, bool]:
        await service.unshare(native_id, auth.require_profile(request))
        return {"success": True}
