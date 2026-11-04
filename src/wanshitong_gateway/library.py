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
        item = await self._knowledge(native_id)
        kb_id = str(item.get("knowledge_base_id", ""))
        scope = self._scope_for_kb(kb_id)
        if scope is None:
            raise HTTPException(404, "资料不存在。")
        if not self._can_read_scope(scope, profile):
            raise HTTPException(403, "没有访问这份资料的权限。")
        if write and not self.can_manage_kb(kb_id, profile):
            raise HTTPException(403, "没有维护这份资料的权限。")
        return item, scope

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
        return {
            "scope": scope,
            "can_manage": self.can_manage_scope(scope, profile),
            "can_share": self._personal_sharing_enabled
            and scope == "personal"
            and self.can_manage_scope("public", profile),
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
        """删除本人原件不授予公共区写权限，已有公共副本由管理员维护。"""
        async with self._document_lock(native_id):
            await self.document(native_id, profile, write=True)
            if self._personal_sharing_enabled and self.can_manage_scope(
                "public", profile
            ):
                await self._unshare_locked(native_id)
            await self._delete_native(native_id)
            self._forget(native_id)

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
