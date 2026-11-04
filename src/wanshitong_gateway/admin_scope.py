"""在原生管理代理前校验资料归属，避免共享服务账号扩大内部区权限。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, NoReturn
from urllib.parse import parse_qs, unquote

from fastapi import HTTPException

from wanshitong_gateway.library import LibraryService
from wanshitong_gateway.roles import UserProfile
from wanshitong_gateway.store import GatewayStore
from wanshitong_gateway.weknora.admin import NativeAdminResponse

_SAFE_METHODS = frozenset({"GET", "HEAD"})
_API_PREFIX_SEGMENTS = 3
_OBJECT_ROUTE_SEGMENTS = 2
_UNSCOPED_CONTENT = frozenset(
    {
        "agent",
        "agent-chat",
        "artifacts",
        "datasource",
        "embed-channels",
        "im-channels",
        "knowledge-chat",
        "organizations",
        "shared-agents",
        "shared-knowledge-bases",
    }
)
_SAFE_TENANT_KEYS = frozenset(
    {
        "parser-engine-config",
        "prompt-templates",
        "retrieval-config",
        "web-search-config",
    }
)
_KB_BODY_KEYS = frozenset(
    {
        "kb_id",
        "knowledge_base_id",
        "knowledge_base_ids",
        "source_id",
        "target_id",
        "source_kb_id",
        "target_kb_id",
    }
)
_KNOWLEDGE_BODY_KEYS = frozenset({"ids", "knowledge_id", "knowledge_ids"})


def _deny() -> NoReturn:
    raise HTTPException(403, "没有访问或维护该资料范围的权限。")


def _values(value: object) -> list[str]:
    """统一查询参数和JSON列表中的对象标识。"""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [identifier for item in value for identifier in _values(item)]
    return []


class AdminScopeGuard:
    """原生管理凭据不扩大操作者的资料权限，超级管理员也只访问本人私库。"""

    def __init__(self, library: LibraryService, store: GatewayStore) -> None:
        self._library = library
        self._store = store

    def _contains_forbidden(self, value: object, profile: UserProfile) -> bool:
        if isinstance(value, str):
            return any(
                kb_id in value
                for kb_id in self._library.restricted_kbs(profile)
            )
        if isinstance(value, dict):
            return any(
                self._contains_forbidden(item, profile)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(
                self._contains_forbidden(item, profile) for item in value
            )
        return False

    def _check_kb(
        self, kb_id: str, profile: UserProfile, *, write: bool = False
    ) -> None:
        allowed = (
            self._library.can_manage_kb(kb_id, profile)
            if write
            else self._library.can_read_kb(kb_id, profile)
        )
        if not allowed:
            _deny()
        scope = self._library._scope_for_kb(kb_id)
        if write and scope is not None and scope["scope"] == "personal":
            # 私件写入统一经过容量预占和本人解析队列，原生代理不能绕过。
            raise HTTPException(
                409, "个人资料请在“我的资料”上传、删除或重新解析。"
            )

    async def _check_knowledge(
        self, knowledge_id: str, profile: UserProfile, *, write: bool = False
    ) -> None:
        item = await self._library._knowledge(knowledge_id)
        kb_id = item.get("knowledge_base_id")
        if not isinstance(kb_id, str) or not kb_id:
            _deny()
        self._check_kb(kb_id, profile, write=write)

    async def _check_chunk(
        self, chunk_id: str, profile: UserProfile, *, write: bool = False
    ) -> None:
        result = await self._library._request("GET", "chunks/by-id/" + chunk_id)
        item = result.get("data")
        if not isinstance(item, dict):
            _deny()
        kb_id = item.get("knowledge_base_id")
        if isinstance(kb_id, str) and kb_id:
            self._check_kb(kb_id, profile, write=write)
            return
        knowledge_id = item.get("knowledge_id")
        if not isinstance(knowledge_id, str) or not knowledge_id:
            _deny()
        await self._check_knowledge(knowledge_id, profile, write=write)

    def _session_allowed(self, session_id: str, profile: UserProfile) -> bool:
        """会话须由本地映射证实，任一旧轮次涉及不可读资料便整段拒绝。"""
        with self._store._connect() as connection:
            known = connection.execute(
                "SELECT 1 FROM gateway_conversations WHERE native_session_id=?",
                (session_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT kb_scope_json FROM gateway_turns "
                "WHERE native_session_id=?",
                (session_id,),
            ).fetchall()
        if known is None or not rows:
            return False
        current_kbs = set(self._library.readable_kbs(profile))
        for row in rows:
            try:
                scope = json.loads(row["kb_scope_json"])
            except (TypeError, ValueError):
                return False
            if (
                not isinstance(scope, list)
                or not scope
                or any(
                    not isinstance(kb_id, str) or kb_id not in current_kbs
                    for kb_id in scope
                )
            ):
                return False
        return True

    async def check_request(  # noqa: PLR0913, PLR0917 - 原生代理请求边界需逐项校验。
        self,
        profile: UserProfile,
        method: str,
        path: str,
        query: str,
        content: bytes,
        browser_headers: Mapping[str, str],
    ) -> None:
        """先检查路径、参数和对象归属，再允许共享原生管理凭据发请求。"""
        if path.split("/")[2:3] == ["image-pipelines"]:
            self._check_image_pipeline_request(
                method, path, query, content, profile
            )
            return
        if path == "files":
            _deny()
        segments = path.split("/")
        if (
            segments[:2] != ["api", "v1"]
            or len(segments) < _API_PREFIX_SEGMENTS
        ):
            _deny()
        category, tail = segments[2], segments[3:]
        if category in _UNSCOPED_CONTENT:
            _deny()
        if category == "tenants" and (
            len(tail) != _OBJECT_ROUTE_SEGMENTS
            or tail[0] != "kv"
            or tail[1] not in _SAFE_TENANT_KEYS
        ):
            # 禁止借tenant-owner凭据创建测试令牌、改API principal或读取全权Key。
            _deny()
        parameters: dict[str, Any] = parse_qs(query, keep_blank_values=True)
        content_type = next(
            (
                value
                for key, value in browser_headers.items()
                if key.lower() == "content-type"
            ),
            "",
        )
        if content and (
            "json" in content_type.lower()
            or content.lstrip().startswith((b"{", b"[", b"\xef\xbb\xbf"))
        ):
            try:
                body = json.loads(content)
            except ValueError as error:
                raise HTTPException(422, "管理请求JSON格式无效。") from error
            if not isinstance(body, dict):
                raise HTTPException(422, "管理请求须为JSON对象。")
            if self._contains_forbidden(body, profile):
                _deny()
            parameters.update(body)
        if self._contains_forbidden(unquote(query), profile):
            _deny()
        await self._check_path(category, tail, method, parameters, profile)

    def _check_image_pipeline_request(
        self,
        method: str,
        path: str,
        query: str,
        content: bytes,
        profile: UserProfile,
    ) -> None:
        """图片流水线目录与校验不接受资料目标，也不触发解析或生成。"""
        if query:
            _deny()
        if method == "GET" and path == "api/v1/image-pipelines":
            if content:
                _deny()
            return
        if method != "POST" or path != "api/v1/image-pipelines/validate":
            _deny()
        try:
            body = json.loads(content)
        except ValueError as error:
            raise HTTPException(422, "图片流水线校验须为JSON对象。") from error
        if (
            not isinstance(body, dict)
            or set(body) != {"pipeline_id", "params"}
            or not isinstance(body.get("pipeline_id"), str)
            or not body["pipeline_id"].strip()
            or not isinstance(body.get("params"), dict)
        ):
            raise HTTPException(422, "图片流水线校验参数格式无效。")
        if self._contains_forbidden(body, profile):
            _deny()

    async def _check_path(  # noqa: PLR0912 - 内容路由按对象种类分别绑定。
        self,
        category: str,
        tail: list[str],
        method: str,
        parameters: dict[str, Any],
        profile: UserProfile,
    ) -> None:
        write = method not in _SAFE_METHODS
        if category in {"knowledge-bases", "knowledgebase"}:
            if tail and tail[0] not in {
                "copy",
                "copy-progress",
                "move-targets",
            }:
                self._check_kb(tail[0], profile, write=write)
            elif tail and (
                tail[0] == "copy-progress"
                or (tail[0] == "copy" and method in _SAFE_METHODS)
            ):
                _deny()
            await self._check_parameters(
                parameters, profile, check_ids=True, write=write
            )
            return
        if category == "knowledge":
            await self._check_knowledge_path(tail, method, parameters, profile)
            return
        if category == "chunks":
            if len(tail) >= _OBJECT_ROUTE_SEGMENTS and tail[0] == "by-id":
                await self._check_chunk(tail[1], profile, write=write)
            elif tail:
                await self._check_knowledge(tail[0], profile, write=write)
            else:
                _deny()
            return
        if category == "knowledge-search":
            if not _values(
                parameters.get("knowledge_base_ids")
            ) and not _values(parameters.get("knowledge_ids")):
                _deny()
            await self._check_parameters(
                parameters, profile, check_ids=True, write=False
            )
            return
        if category == "sessions":
            if tail and not self._session_allowed(tail[0], profile):
                _deny()
            if method not in _SAFE_METHODS:
                _deny()
            return
        if category == "messages":
            if not tail or not self._session_allowed(tail[0], profile):
                _deny()
            return
        if category == "faq":
            _deny()

    async def _check_knowledge_path(
        self,
        tail: list[str],
        method: str,
        parameters: dict[str, Any],
        profile: UserProfile,
    ) -> None:
        write = method not in _SAFE_METHODS
        await self._check_parameters(
            parameters, profile, check_ids=True, write=write
        )
        if not tail or (tail[0] == "move" and method in _SAFE_METHODS):
            _deny()
        if tail[0] == "search":
            # 原生文件搜索不支持明确KB筛选，普通管理员改用库内搜索。
            _deny()
        if tail[0] in {
            "batch",
            "batch-delete",
            "batch-reparse",
            "folder",
            "move",
            "tags",
        }:
            if tail[0] == "tags" and isinstance(
                parameters.get("updates"), dict
            ):
                return
            if not any(
                _values(parameters.get(key)) for key in _KNOWLEDGE_BODY_KEYS
            ):
                _deny()
            return
        if tail[0] in {"manual", "image"}:
            if len(tail) < _OBJECT_ROUTE_SEGMENTS:
                _deny()
            await self._check_knowledge(tail[1], profile, write=write)
        else:
            await self._check_knowledge(tail[0], profile, write=write)

    async def _check_parameters(
        self,
        parameters: dict[str, Any],
        profile: UserProfile,
        *,
        check_ids: bool,
        write: bool,
    ) -> None:
        for key, value in parameters.items():
            if key in _KB_BODY_KEYS:
                for kb_id in _values(value):
                    self._check_kb(kb_id, profile, write=write)
            if check_ids and key in _KNOWLEDGE_BODY_KEYS:
                for knowledge_id in _values(value):
                    await self._check_knowledge(
                        knowledge_id, profile, write=write
                    )
            if key == "updates" and isinstance(value, list):
                for update in value:
                    if isinstance(update, dict):
                        await self._check_parameters(
                            update,
                            profile,
                            check_ids=check_ids,
                            write=write,
                        )
            elif key == "updates" and isinstance(value, dict) and check_ids:
                for knowledge_id in value:
                    await self._check_knowledge(
                        knowledge_id, profile, write=write
                    )

    async def filter_response(
        self, profile: UserProfile, path: str, response: NativeAdminResponse
    ) -> NativeAdminResponse:
        """聚合列表隐藏不可读资料，超级管理员不跳过过滤。"""
        if (
            not HTTPStatus.OK
            <= response.status_code
            < HTTPStatus.MULTIPLE_CHOICES
        ):
            return response
        if response.stream is not None:
            # 对象下载已经过请求归属校验，二进制流无需按 JSON 列表过滤。
            if path.startswith("api/v1/knowledge/") and path.endswith(
                "/download"
            ):
                return response
            await response.stream.aclose()
            _deny()
        if not response.content or "json" not in response.headers.get(
            "content-type", ""
        ):
            return response
        try:
            payload = json.loads(response.content)
        except ValueError as error:
            raise HTTPException(502, "原生资料响应格式无效。") from error
        if not isinstance(payload, dict):
            _deny()
        data = payload.get("data")
        if isinstance(data, list):
            filtered = [
                item
                for item in data
                if not self._contains_forbidden(item, profile)
            ]
            if path == "api/v1/knowledge-bases" or path.endswith(
                "/move-targets"
            ):
                filtered = [
                    item
                    for item in filtered
                    if isinstance(item, dict)
                    and self._library.can_read_kb(
                        str(item.get("id", "")), profile
                    )
                ]
            if path == "api/v1/sessions":
                filtered = [
                    item
                    for item in filtered
                    if isinstance(item, dict)
                    and self._session_allowed(str(item.get("id", "")), profile)
                ]
            payload["data"] = filtered
            if "total" in payload:
                payload["total"] = len(filtered)
            if "has_more" in payload:
                payload["has_more"] = False
        elif self._contains_forbidden(payload, profile):
            _deny()
        return NativeAdminResponse(
            status_code=response.status_code,
            content=json.dumps(payload, ensure_ascii=False).encode(),
            headers=response.headers,
        )
