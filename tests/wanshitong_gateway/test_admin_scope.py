"""普通管理员的原生后台绕过、人力内部区与历史会话边界。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException

from wanshitong_gateway.admin_scope import AdminScopeGuard
from wanshitong_gateway.library import LibraryService
from wanshitong_gateway.roles import UserProfile
from wanshitong_gateway.store import GatewayStore
from wanshitong_gateway.weknora.admin import NativeAdminResponse

_ADMIN = UserProfile("admin", "admin", "管理员", None, "admin")
_SUPER = UserProfile("root", "root", "超管", None, "admin", True)
_HR = UserProfile("hr", "hr", "人力", None, "hr")
_HR_KB_ID = "kb-hr-internal"


class _Library:
    """只提供守卫需要的归属读取，服务器请求不在本专项中执行。"""

    public_kb_ids = ("kb-public",)

    @staticmethod
    def registered_kbs() -> list[dict[str, str]]:
        return [
            {"kb_id": _HR_KB_ID, "scope": "hr_internal"},
            {"kb_id": "kb-personal", "scope": "personal"},
        ]

    @staticmethod
    def hr_internal_kbs() -> frozenset[str]:
        return frozenset({_HR_KB_ID})

    @staticmethod
    async def _knowledge(identifier: str) -> dict[str, Any]:
        if identifier not in {"hr-doc", "public-doc", "private-doc"}:
            raise HTTPException(404, "资料不存在。")
        return {
            "id": identifier,
            "knowledge_base_id": _HR_KB_ID
            if identifier == "hr-doc"
            else "kb-public",
        }

    @staticmethod
    async def _request(method: str, path: str) -> dict[str, Any]:
        assert method == "GET"
        return {
            "data": {
                "knowledge_id": "hr-doc"
                if path.endswith("hr-chunk")
                else "public-doc"
            }
        }


@pytest.fixture
def guard(tmp_path: Path) -> tuple[AdminScopeGuard, GatewayStore]:
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    return AdminScopeGuard(cast(LibraryService, _Library()), store), store


async def _check(  # noqa: PLR0913 - 测试代理请求显式覆盖协议字段。
    guard: AdminScopeGuard,
    path: str,
    *,
    body: object = None,
    query: str = "",
    profile: UserProfile = _ADMIN,
    method: str = "GET",
    content_type: str = "application/json",
) -> None:
    await guard.check_request(
        profile,
        method,
        path,
        query,
        json.dumps(body).encode() if body is not None else b"",
        {"Content-Type": content_type},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        f"api/v1/knowledge-bases/{_HR_KB_ID}",
        f"api/v1/knowledge-bases/{_HR_KB_ID}/knowledge/file",
        f"api/v1/knowledge-bases/{_HR_KB_ID}/files",
        f"api/v1/knowledgebase/{_HR_KB_ID}/wiki/pages/secret",
        "api/v1/knowledge/hr-doc",
        "api/v1/knowledge/hr-doc/download",
        "api/v1/knowledge/manual/hr-doc",
        "api/v1/chunks/hr-doc",
        "api/v1/chunks/by-id/hr-chunk",
    ],
)
async def test_hr_content_paths_reject_plain_admin(
    guard: tuple[AdminScopeGuard, GatewayStore], path: str
) -> None:
    with pytest.raises(HTTPException) as denied:
        await _check(guard[0], path)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [_HR, _SUPER])
async def test_hr_and_super_admin_remain_authorized(
    guard: tuple[AdminScopeGuard, GatewayStore], profile: UserProfile
) -> None:
    await _check(
        guard[0], f"api/v1/knowledge-bases/{_HR_KB_ID}", profile=profile
    )


@pytest.mark.asyncio
async def test_public_and_other_personal_management_stay_available(
    guard: tuple[AdminScopeGuard, GatewayStore],
) -> None:
    for identifier in ("public-doc", "private-doc"):
        await _check(guard[0], f"api/v1/knowledge/{identifier}/download")
        await _check(
            guard[0],
            f"api/v1/chunks/{identifier}/chunk-1",
            method="PUT",
            body={"content": "更新"},
        )
    await _check(
        guard[0],
        "api/v1/knowledge-bases/kb-public/knowledge/file",
        method="POST",
    )


@pytest.mark.asyncio
async def test_batch_copy_and_explicit_search_check_body_targets(
    guard: tuple[AdminScopeGuard, GatewayStore],
) -> None:
    for path, body in (
        (
            "api/v1/knowledge/batch-delete",
            {"kb_id": "kb-public", "ids": ["hr-doc"]},
        ),
        (
            "api/v1/knowledge-bases/copy",
            {"source_id": _HR_KB_ID, "target_id": "kb-public"},
        ),
        (
            "api/v1/knowledge-search",
            {"knowledge_base_ids": [_HR_KB_ID], "query": "查资料"},
        ),
        (
            "api/v1/knowledge-search",
            {"knowledge_ids": ["hr-doc"], "query": "查资料"},
        ),
    ):
        with pytest.raises(HTTPException) as denied:
            await _check(
                guard[0],
                path,
                method="POST",
                body=body,
                content_type="text/plain",
            )
        assert denied.value.status_code == 403
    await _check(
        guard[0], "api/v1/knowledge/batch", query="ids=public-doc,private-doc"
    )
    await _check(
        guard[0],
        "api/v1/knowledge-search",
        method="POST",
        body={"knowledge_base_ids": ["kb-public"], "query": "查资料"},
    )
    await _check(
        guard[0],
        "api/v1/knowledge/tags",
        method="PUT",
        body={"updates": {"public-doc": ["tag-1"]}},
    )
    with pytest.raises(HTTPException) as denied:
        await _check(
            guard[0],
            "api/v1/knowledge/tags",
            method="PUT",
            body={"updates": {"hr-doc": ["tag-1"]}},
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "files",
        "api/v1/knowledge/search",
        "api/v1/knowledge-search",
        "api/v1/tenants/10000/api-principal-config",
        "api/v1/tenants/10000/api-principal-test-token",
        "api/v1/tenants/10000/api-keys",
        "api/v1/tenants/10000/audit-log",
        "api/v1/tenants/all",
        "api/v1/tenants/kv/storage-engine-config",
        "api/v1/artifacts",
        "api/v1/knowledge-chat/session",
        "api/v1/agent-chat/session",
        "api/v1/agent/session",
        "api/v1/organizations",
        "api/v1/datasource",
    ],
)
async def test_unscoped_content_and_owner_credential_routes_are_denied(
    guard: tuple[AdminScopeGuard, GatewayStore], path: str
) -> None:
    with pytest.raises(HTTPException) as denied:
        await _check(guard[0], path)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_normal_settings_routes_remain_available(
    guard: tuple[AdminScopeGuard, GatewayStore],
) -> None:
    for path in (
        "api/v1/models",
        "api/v1/system/info",
        "api/v1/tenants/kv/prompt-templates",
    ):
        await _check(guard[0], path)


@pytest.mark.asyncio
async def test_plain_admin_can_read_and_validate_image_pipeline_config(
    guard: tuple[AdminScopeGuard, GatewayStore],
) -> None:
    await _check(guard[0], "api/v1/image-pipelines")
    # 参数组合是否可用由原生规则回答，网关只校验配置接口的边界。
    await _check(
        guard[0],
        "api/v1/image-pipelines/validate",
        method="POST",
        body={
            "pipeline_id": "default",
            "params": {"caption_enabled": False, "ocr_enabled": False},
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "query", "body"),
    [
        ("GET", "api/v1/image-pipelines", "kb_id=kb-public", None),
        ("GET", "api/v1/image-pipelines", "", {"kb_id": _HR_KB_ID}),
        ("GET", "api/v1/image-pipelines/validate", "", None),
        ("POST", "api/v1/image-pipelines/validate/extra", "", {}),
        (
            "POST",
            "api/v1/image-pipelines/validate",
            "knowledge_id=hr-doc",
            {"pipeline_id": "default", "params": {}},
        ),
        (
            "POST",
            "api/v1/image-pipelines/validate",
            "",
            {"pipeline_id": "default", "params": {"kb_id": _HR_KB_ID}},
        ),
    ],
)
async def test_image_pipeline_config_rejects_resource_targets(
    guard: tuple[AdminScopeGuard, GatewayStore],
    method: str,
    path: str,
    query: str,
    body: object,
) -> None:
    with pytest.raises(HTTPException) as denied:
        await _check(guard[0], path, method=method, query=query, body=body)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"pipeline_id": "", "params": {}},
        {"pipeline_id": "default", "params": []},
        {"pipeline_id": "default", "params": {}, "knowledge_id": "hr-doc"},
    ],
)
async def test_image_pipeline_validation_requires_native_config_shape(
    guard: tuple[AdminScopeGuard, GatewayStore], body: object
) -> None:
    with pytest.raises(HTTPException) as denied:
        await _check(
            guard[0],
            "api/v1/image-pipelines/validate",
            method="POST",
            body=body,
        )
    assert denied.value.status_code == 422


def _remember_session(
    store: GatewayStore, identifier: str, kb_scope: tuple[str, ...]
) -> None:
    store.bind_session(
        deployment_id="test",
        owner_id="owner",
        conversation_id=identifier,
        native_session_id=identifier,
    )
    store.start_turn(
        trace_id="trace_" + identifier,
        deployment_id="test",
        owner_id="owner",
        conversation_id=identifier,
        native_session_id=identifier,
        question="问题",
        kb_scope=kb_scope,
    )


@pytest.mark.asyncio
async def test_native_session_must_have_known_non_hr_history(
    guard: tuple[AdminScopeGuard, GatewayStore],
) -> None:
    protector, store = guard
    _remember_session(store, "public-session", ("kb-public",))
    _remember_session(store, "hr-session", (_HR_KB_ID, "kb-public"))
    _remember_session(store, "old-session", ("deleted-hr-kb",))
    await _check(protector, "api/v1/messages/public-session/load")
    for path in (
        "api/v1/sessions/hr-session",
        "api/v1/messages/hr-session/load",
        "api/v1/sessions/unknown/messages/message/files",
        "api/v1/sessions/old-session",
    ):
        with pytest.raises(HTTPException) as denied:
            await _check(protector, path)
        assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_kb_list_filters_internal_items_and_counts(
    guard: tuple[AdminScopeGuard, GatewayStore],
) -> None:
    upstream = NativeAdminResponse(
        200,
        json.dumps(
            {
                "success": True,
                "total": 3,
                "data": [
                    {"id": "kb-public", "name": "公共"},
                    {"id": _HR_KB_ID, "name": "人力内部"},
                    {"id": "kb-personal", "name": "个人"},
                ],
            }
        ).encode(),
        {"content-type": "application/json"},
    )
    result = await guard[0].filter_response(
        _ADMIN, "api/v1/knowledge-bases", upstream
    )
    payload = json.loads(result.content)
    assert payload["total"] == 2
    assert [row["id"] for row in payload["data"]] == [
        "kb-public",
        "kb-personal",
    ]
    assert _HR_KB_ID.encode() not in result.content
    assert (
        await guard[0].filter_response(
            _SUPER, "api/v1/knowledge-bases", upstream
        )
        is upstream
    )


@pytest.mark.asyncio
async def test_native_session_list_omits_internal_and_unknown_sessions(
    guard: tuple[AdminScopeGuard, GatewayStore],
) -> None:
    protector, store = guard
    _remember_session(store, "public-session", ("kb-public",))
    _remember_session(store, "hr-session", (_HR_KB_ID,))
    upstream = NativeAdminResponse(
        200,
        json.dumps(
            {
                "data": [
                    {"id": "public-session"},
                    {"id": "hr-session"},
                    {"id": "untracked"},
                ],
                "total": 3,
            }
        ).encode(),
        {"content-type": "application/json"},
    )
    result = await protector.filter_response(
        _ADMIN, "api/v1/sessions", upstream
    )
    assert json.loads(result.content)["data"] == [{"id": "public-session"}]
