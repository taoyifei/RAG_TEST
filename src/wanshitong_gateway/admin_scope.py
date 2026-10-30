"""在原生管理代理前校验资料归属，避免共享服务账号扩大内部区权限。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, NoReturn
from urllib.parse import parse_qs, unquote

from fastapi import HTTPException

from wanshitong_gateway.library import LibraryService
from wanshitong_gateway.roles import UserProfile, can_access_hr_internal
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
    raise HTTPException(403, "该操作涉及人力内部资料或无法核对资料范围。")


def _values(value: object) -> list[str]:
    """统一查询参数和JSON列表中的对象标识。"""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [identifier for item in value for identifier in _values(item)]
    return []


class AdminScopeGuard:
    """普通管理员保留公开及个人资料管理，内部区由人力和超管访问。"""

    def __init__(self, library: LibraryService, store: GatewayStore) -> None:
        self._library = library
        self._store = store

    def _contains_internal(self, value: object) -> bool:
        if isinstance(value, str):
            return any(
                kb_id in value for kb_id in self._library.hr_internal_kbs()
            )
        if isinstance(value, dict):
            return any(self._contains_internal(item) for item in value.values())
        if isinstance(value, list):
            return any(self._contains_internal(item) for item in value)
        return False

    def _check_kb(self, kb_id: str) -> None:
        if kb_id in self._library.hr_internal_kbs():
            _deny()

    async def _check_knowledge(self, knowledge_id: str) -> None:
        item = await self._library._knowledge(knowledge_id)
        kb_id = item.get("knowledge_base_id")
        if not isinstance(kb_id, str) or not kb_id:
            _deny()
        self._check_kb(kb_id)

    async def _check_chunk(self, chunk_id: str) -> None:
        result = await self._library._request("GET", "chunks/by-id/" + chunk_id)
        item = result.get("data")
        if not isinstance(item, dict):
            _deny()
        kb_id = item.get("knowledge_base_id")
        if isinstance(kb_id, str) and kb_id:
            self._check_kb(kb_id)
            return
        knowledge_id = item.get("knowledge_id")
        if not isinstance(knowledge_id, str) or not knowledge_id:
            _deny()
        await self._check_knowledge(knowledge_id)

    def _session_allowed(self, session_id: str) -> bool:
        """会话须由本地映射证实，任一旧轮次涉及内部区便整段拒绝。"""
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
        current_kbs = set(self._library.public_kb_ids) | {
            row["kb_id"]
            for row in self._library.registered_kbs()
            if row["scope"] != "hr_internal"
        }
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
        if can_access_hr_internal(profile):
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
            if self._contains_internal(body):
                _deny()
            parameters.update(body)
        if self._contains_internal(unquote(query)):
            _deny()
        await self._check_path(category, tail, method, parameters)

    async def _check_path(  # noqa: PLR0912 - 内容路由按对象种类分别绑定。
        self,
        category: str,
        tail: list[str],
        method: str,
        parameters: dict[str, Any],
    ) -> None:
        if category in {"knowledge-bases", "knowledgebase"}:
            if tail and tail[0] not in {
                "copy",
                "copy-progress",
                "move-targets",
            }:
                self._check_kb(tail[0])
            elif tail and tail[0] == "copy-progress":
                _deny()
            await self._check_parameters(parameters, check_ids=False)
            return
        if category == "knowledge":
            await self._check_knowledge_path(tail, method, parameters)
            return
        if category == "chunks":
            if len(tail) >= _OBJECT_ROUTE_SEGMENTS and tail[0] == "by-id":
                await self._check_chunk(tail[1])
            elif tail:
                await self._check_knowledge(tail[0])
            else:
                _deny()
            return
        if category == "knowledge-search":
            if not _values(
                parameters.get("knowledge_base_ids")
            ) and not _values(parameters.get("knowledge_ids")):
                _deny()
            await self._check_parameters(parameters, check_ids=True)
            return
        if category == "sessions":
            if tail and not self._session_allowed(tail[0]):
                _deny()
            if method not in _SAFE_METHODS:
                _deny()
            return
        if category == "messages":
            if not tail or not self._session_allowed(tail[0]):
                _deny()
            return
        if category == "faq":
            _deny()

    async def _check_knowledge_path(
        self, tail: list[str], method: str, parameters: dict[str, Any]
    ) -> None:
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
            await self._check_parameters(parameters, check_ids=True)
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
            await self._check_knowledge(tail[1])
        else:
            await self._check_knowledge(tail[0])

    async def _check_parameters(
        self, parameters: dict[str, Any], *, check_ids: bool
    ) -> None:
        for key, value in parameters.items():
            if key in _KB_BODY_KEYS:
                for kb_id in _values(value):
                    self._check_kb(kb_id)
            if check_ids and key in _KNOWLEDGE_BODY_KEYS:
                for knowledge_id in _values(value):
                    await self._check_knowledge(knowledge_id)
            if key == "updates" and isinstance(value, list):
                for update in value:
                    if isinstance(update, dict):
                        await self._check_parameters(
                            update, check_ids=check_ids
                        )
            elif key == "updates" and isinstance(value, dict) and check_ids:
                for knowledge_id in value:
                    await self._check_knowledge(knowledge_id)

    async def filter_response(
        self, profile: UserProfile, path: str, response: NativeAdminResponse
    ) -> NativeAdminResponse:
        """聚合列表隐藏内部区，无法可靠归属的响应拒绝透传。"""
        if (
            can_access_hr_internal(profile)
            or not HTTPStatus.OK
            <= response.status_code
            < HTTPStatus.MULTIPLE_CHOICES
        ):
            return response
        if response.stream is not None:
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
                item for item in data if not self._contains_internal(item)
            ]
            if path == "api/v1/sessions":
                filtered = [
                    item
                    for item in filtered
                    if isinstance(item, dict)
                    and self._session_allowed(str(item.get("id", "")))
                ]
            payload["data"] = filtered
            if "total" in payload:
                payload["total"] = len(filtered)
            if "has_more" in payload:
                payload["has_more"] = False
        elif self._contains_internal(payload):
            _deny()
        return NativeAdminResponse(
            status_code=response.status_code,
            content=json.dumps(payload, ensure_ascii=False).encode(),
            headers=response.headers,
        )
