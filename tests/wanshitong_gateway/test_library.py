"""个人资料隔离、人力权限和共享生命周期的必要行为。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from email.parser import BytesParser
from pathlib import Path
from threading import Event
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request

import wanshitong_gateway.library as library_module
from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.compat.core.errors import ProviderUnavailable
from wanshitong_gateway.library import LibraryService, register_library_routes
from wanshitong_gateway.roles import UserProfile
from wanshitong_gateway.weknora.admin import NativeAdminClient

_ALICE = UserProfile("a", "alice", "甲", None, "user")
_PUBLISHER = replace(_ALICE, role="admin")
_BOB = UserProfile("b", "bob", "乙", None, "user")
_HR = UserProfile("h", "hr", "人力", "42", "hr")
_SUPER = UserProfile("root", "root", "超级管理员", None, "admin", True)
_SERVICE_KEY = "synthetic-key"


class _Native:
    """模拟已核对的0.8.3资料接口，保留真实客户端与Key范围更新。"""

    def __init__(self) -> None:
        self.kbs: dict[str, dict[str, Any]] = {
            "public": {
                "id": "public",
                "embedding_model_id": "embedding-old",
                "chunking_config": {"chunk_size": 512},
            },
            "public-new": {
                "id": "public-new",
                "embedding_model_id": "embedding-new",
                "chunking_config": {"chunk_size": 512},
            },
        }
        self.next_kb_id = len(self.kbs)
        self.reject_key_updates = False
        self.documents: dict[str, dict[str, Any]] = {}
        self.files: dict[str, bytes] = {}
        self.key_scope = ["public", "public-new"]
        self.updates = 0
        self.retries = 0
        self.upload_fields: dict[str, dict[str, str]] = {}
        self.upload_content_types: dict[str, str] = {}
        self.error: str | None = None
        self.deferred_deletes: dict[str, int] = {}
        self.pending_deletes: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self.parse_status = "completed"
        self.upload_error_status: int | None = None
        self.lose_upload_response = False
        self.delay_retry_state = False
        self.lose_retry_response = False

    def respond(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - 模拟固定原生接口。
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/api/v1/auth/login":
            return httpx.Response(
                200, json={"success": True, "token": "admin-token"}
            )
        if self.error == "connection":
            raise httpx.ConnectError("offline", request=request)
        if self.error == "malformed":
            return httpx.Response(200, content=b"not-json")
        if path == "/api/v1/tenants/9/api-keys":
            return self.ok(
                [
                    {
                        "id": 1,
                        "name": "service",
                        "api_key": _SERVICE_KEY,
                        "scope_type": "tenant",
                        "full_access": False,
                        "knowledge_base_ids": list(self.key_scope),
                        "capabilities": ["chat"],
                    }
                ]
            )
        if path == "/api/v1/tenants/9/api-keys/1":
            body = json.loads(request.content)
            if self.reject_key_updates:
                return httpx.Response(503, json={"success": False})
            if any(
                kb_id not in self.kbs for kb_id in body["knowledge_base_ids"]
            ):
                return httpx.Response(400, json={"success": False})
            assert body["full_access"] is False
            self.key_scope = body["knowledge_base_ids"]
            self.updates += 1
            return self.ok({})
        if path == "/api/v1/knowledge-bases" and request.method == "POST":
            kb_id = f"kb-{self.next_kb_id}"
            self.next_kb_id += 1
            self.kbs[kb_id] = {**json.loads(request.content), "id": kb_id}
            return self.ok(self.kbs[kb_id])
        if path.startswith("/api/v1/knowledge-bases/"):
            return self.knowledge_base(request)
        if path.startswith("/api/v1/knowledge/"):
            return self.knowledge(request)
        return httpx.Response(404, json={"success": False})

    def knowledge_base(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        kb_id = parts[4]
        if kb_id not in self.kbs:
            return httpx.Response(404, json={"success": False})
        if len(parts) == 5:
            return self.ok(self.kbs[kb_id])
        if request.method == "GET":
            return self.ok(
                [
                    item
                    for item in self.documents.values()
                    if item["knowledge_base_id"] == kb_id
                ]
            )
        if self.upload_error_status is not None:
            return httpx.Response(
                self.upload_error_status, json={"success": False}
            )
        mime = BytesParser().parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        part = next(item for item in mime.get_payload() if item.get_filename())
        content = part.get_payload(decode=True)
        native_id = f"doc-{len(self.files)}-{len(self.documents)}"
        self.upload_content_types[native_id] = mime.get_content_type()
        self.upload_fields[native_id] = {
            item.get_param("name", header="content-disposition"): (
                item.get_payload(decode=True).decode()
            )
            for item in mime.get_payload()
            if not item.get_filename()
        }
        self.files[native_id] = content
        self.documents[native_id] = {
            "id": native_id,
            "knowledge_base_id": kb_id,
            "file_name": part.get_filename(),
            "parse_status": self.parse_status,
            "file_size": len(content),
            "metadata": json.loads(self.upload_fields[native_id]["metadata"]),
        }
        if self.lose_upload_response:
            self.lose_upload_response = False
            raise httpx.ReadTimeout("响应丢失", request=request)
        return self.ok(self.documents[native_id])

    def knowledge(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        native_id = parts[4]
        if native_id not in self.documents:
            return httpx.Response(404, json={"success": False})
        if parts[-1] == "download":
            return httpx.Response(
                200,
                content=self.files[native_id],
                headers={"content-type": "application/octet-stream"},
            )
        if parts[-1] == "reparse":
            assert json.loads(request.content) == {}
            self.retries += 1
            if not self.delay_retry_state:
                self.documents[native_id]["parse_status"] = self.parse_status
                self.documents[native_id]["updated_at"] = (
                    f"reparse-{self.retries}"
                )
            if self.lose_retry_response:
                self.lose_retry_response = False
                raise httpx.ReadTimeout("重试响应丢失", request=request)
            return self.ok(self.documents[native_id])
        if request.method == "DELETE":
            if native_id in self.deferred_deletes:
                self.documents[native_id]["parse_status"] = "deleting"
                self.pending_deletes.add(native_id)
            else:
                self.documents.pop(native_id)
            return self.ok({})
        if native_id in self.pending_deletes:
            if self.deferred_deletes[native_id] > 0:
                self.deferred_deletes[native_id] -= 1
            else:
                self.documents.pop(native_id)
                self.pending_deletes.remove(native_id)
                return httpx.Response(404, json={"success": False})
        return self.ok(self.documents[native_id])

    @staticmethod
    def ok(data: object) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": data})


@pytest_asyncio.fixture
async def library(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[LibraryService, _Native, list[str]]]:
    state = _Native()
    native = NativeAdminClient(
        base_url="http://native.test",
        email="admin@example.test",
        password="synthetic",  # noqa: S106 - 固定测试凭据。
        tenant_id=9,
        transport=httpx.MockTransport(state.respond),
    )
    public_scope = ["public"]
    service = LibraryService(
        tmp_path / "gateway.sqlite3",
        native,
        ("public",),
        hashlib.sha256(_SERVICE_KEY.encode()).digest(),
        public_scope=lambda: tuple(public_scope),
        personal_sharing_enabled=bool(getattr(request, "param", False)),
    )
    try:
        yield service, state, public_scope
    finally:
        await service.close()
        await native.close()


@pytest.mark.asyncio
async def test_personal_upload_isolated_and_scope_tracks_binding(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, binding = library
    first = await service.upload("personal", _ALICE, "one.txt", b"alice-file")
    first_kb = native.documents[first["id"]]["knowledge_base_id"]
    assert first_kb in native.key_scope
    with pytest.raises(HTTPException) as denied:
        await service.download(first["id"], _BOB)
    assert denied.value.status_code == 403
    assert (await service.list_documents("personal", _BOB))["items"] == []
    service.check_turn(_ALICE, {"kb_scope_json": '["public"]'})

    binding[:] = ["public-new"]
    assert "public" not in service.readable_kbs(_ALICE)
    second = await service.upload("personal", _BOB, "two.md", b"bob-file")
    second_kb = native.documents[second["id"]]["knowledge_base_id"]
    assert native.kbs[second_kb]["embedding_model_id"] == "embedding-new"
    assert first_kb != second_kb
    assert set(native.key_scope) >= {first_kb, second_kb}
    assert [
        item["id"]
        for item in (await service.list_documents("personal", _ALICE))["items"]
    ] == [first["id"]]
    assert (await service.download(second["id"], _BOB)).content == b"bob-file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,profile",
    [("personal", _ALICE), ("hr_public", _HR), ("hr_internal", _HR)],
)
async def test_library_pdf_upload_uses_builtin_with_existing_bad_template(
    library: tuple[LibraryService, _Native, list[str]],
    scope: str,
    profile: UserProfile,
) -> None:
    service, native, _ = library
    original = {
        "chunk_size": 512,
        "chunk_overlap": 80,
        "parser_engine_rules": [
            {"engine": "paddleocr_vl", "file_types": [".PDF", "png"]},
            {
                "engine": "builtin",
                "file_types": ["xlsx"],
                "xlsx_first_row_as_header": False,
            },
        ],
    }
    native.kbs["public"]["chunking_config"] = copy.deepcopy(original)
    created = await service.upload(
        cast(library_module.LibraryScope, scope), profile, "seed.txt", b"seed"
    )
    kb_id = native.documents[created["id"]]["knowledge_base_id"]
    new_chunking = native.kbs[kb_id]["chunking_config"]
    assert new_chunking["chunk_size"] == 512
    assert new_chunking["chunk_overlap"] == 80
    assert new_chunking["parser_engine_rules"] == [
        {"file_types": ["pdf"], "engine": "builtin"},
        {"file_types": ["png"], "engine": "paddleocr_vl"},
        {
            "engine": "builtin",
            "file_types": ["xlsx"],
            "xlsx_first_row_as_header": False,
        },
    ]
    assert native.kbs["public"]["chunking_config"] == original

    # 模拟修复前已建立的资料库；上传策略不能只依赖新建库时的规则。
    native.kbs[kb_id]["chunking_config"] = copy.deepcopy(original)
    uploaded = await service.upload(
        cast(library_module.LibraryScope, scope), profile, "guide.PDF", b"pdf"
    )
    fields = native.upload_fields[uploaded["id"]]
    assert native.upload_content_types[uploaded["id"]] == "multipart/form-data"
    assert fields["fileName"] == "guide.PDF"
    assert json.loads(fields["metadata"])["wxd_owner_id"] == profile.user_id
    assert json.loads(fields["process_config"]) == {
        "parser_engine_rules": [{"file_types": ["pdf"], "engine": "builtin"}]
    }
    assert native.kbs[kb_id]["chunking_config"] == original
    assert native.kbs["public"]["chunking_config"] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
async def test_shared_pdf_keeps_builtin_override_without_changing_other_uploads(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _PUBLISHER, "guide.pdf", b"pdf")
    published = await service.share(original["id"], _PUBLISHER)
    fields = native.upload_fields[published["id"]]
    assert (
        json.loads(fields["process_config"])["parser_engine_rules"][0]["engine"]
        == "builtin"
    )
    assert json.loads(fields["metadata"])["wxd_source_id"] == original["id"]
    markdown = await service.upload("personal", _PUBLISHER, "guide.md", b"text")
    assert "process_config" not in native.upload_fields[markdown["id"]]


@pytest.mark.asyncio
async def test_hr_public_qa_read_does_not_grant_maintenance(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    uploaded = await service.upload("hr_public", _HR, "policy.md", b"policy")
    identifier = uploaded["id"]
    kb_id = native.documents[identifier]["knowledge_base_id"]

    assert service.search_kbs(_ALICE, ("hr_public",)) == (kb_id,)
    document, scope = await service.document(identifier, _ALICE)
    assert document["id"] == identifier
    assert scope["scope"] == "hr_public"
    assert (await service.download(identifier, _ALICE)).content == b"policy"

    for operation in (
        lambda: service.upload("hr_public", _ALICE, "new.md", b"new-policy"),
        lambda: service.document(identifier, _ALICE, write=True),
        lambda: service.delete(identifier, _ALICE),
    ):
        with pytest.raises(HTTPException) as denied:
            await operation()
        assert denied.value.status_code == 403
    assert identifier in native.documents


@pytest.mark.asyncio
async def test_hr_internal_access_and_downgraded_history(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    uploaded = await service.upload("hr_internal", _HR, "hr.txt", b"hr-only")
    kb_id = native.documents[uploaded["id"]]["knowledge_base_id"]
    with pytest.raises(HTTPException) as denied:
        await service.list_documents("hr_internal", _ALICE)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        await service.document(uploaded["id"], _ALICE)
    assert denied.value.status_code == 403
    turn = {"kb_scope_json": json.dumps(["public", kb_id])}
    service.check_turn(_HR, turn)
    with pytest.raises(HTTPException) as denied:
        service.check_turn(replace(_HR, role="user"), turn)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_regular_admin_cannot_read_or_manage_hr_internal(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    uploaded = await service.upload("hr_internal", _HR, "hr.txt", b"hr-only")
    identifier = uploaded["id"]
    kb_id = native.documents[identifier]["knowledge_base_id"]
    administrator = UserProfile("admin", "admin", "管理员", None, "admin")
    super_admin = replace(administrator, is_super_admin=True)
    assert kb_id not in service.readable_kbs(administrator)
    for operation in (
        lambda: service.list_documents("hr_internal", administrator),
        lambda: service.document(identifier, administrator),
        lambda: service.download(identifier, administrator),
        lambda: service.delete(identifier, administrator),
        lambda: service.upload("hr_internal", administrator, "new.txt", b"x"),
    ):
        with pytest.raises(HTTPException) as denied:
            await operation()
        assert denied.value.status_code == 403
    assert identifier in native.documents
    assert kb_id in service.readable_kbs(super_admin)
    assert (
        await service.download(identifier, super_admin)
    ).content == b"hr-only"
    # 即使绑定里误选了人力内部库，也不能变成公开检索范围。
    library[2].append(kb_id)
    assert kb_id not in service.public_kb_ids
    assert kb_id not in service.search_kbs(_ALICE, ("public",))


@pytest.mark.asyncio
async def test_recreated_hr_library_keeps_old_scope_protected(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    old_document = await service.upload("hr_internal", _HR, "old.txt", b"old")
    old_kb = native.documents[old_document["id"]]["knowledge_base_id"]
    native.kbs.pop(old_kb)
    current = await service.upload("hr_internal", _HR, "new.txt", b"new")
    new_kb = native.documents[current["id"]]["knowledge_base_id"]
    administrator = UserProfile("admin", "admin", "管理员", None, "admin")
    assert old_kb != new_kb
    assert set(service.restricted_kbs(administrator)) == {old_kb, new_kb}
    assert service.search_kbs(_HR, ("hr_internal",)) == (new_kb,)
    with pytest.raises(HTTPException):
        service.check_turn(
            administrator, {"kb_scope_json": json.dumps([old_kb])}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
async def test_share_withdraw_and_delete_keep_private_original(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", _PUBLISHER, "one.txt", b"private"
    )
    shared = await asyncio.gather(
        service.share(original["id"], _PUBLISHER),
        service.share(original["id"], _PUBLISHER),
    )
    assert shared[0]["id"] == shared[1]["id"]
    published_id = shared[0]["id"]
    assert (await service.download(published_id, _BOB)).content == b"private"
    with pytest.raises(HTTPException) as denied:
        await service.delete(published_id, _BOB)
    assert denied.value.status_code == 403
    await service.unshare(original["id"], _PUBLISHER)
    assert published_id not in native.documents
    assert original["id"] in native.documents
    assert (await service.list_documents("personal", _PUBLISHER))["items"][0][
        "shared"
    ] is False
    await service.share(original["id"], _PUBLISHER)
    await service.delete(original["id"], _PUBLISHER)
    assert native.documents == {}


class _Auth:
    def __init__(self, profile: UserProfile = _ALICE) -> None:
        self.profile = profile

    def require_profile(self, request: Request) -> UserProfile:
        del request
        return self.profile


@pytest.mark.asyncio
async def test_library_http_upload_retry_and_upstream_failures(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    app = FastAPI()
    register_library_routes(app, cast(GatewayAuth, _Auth()), service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as browser:
        response = await browser.post(
            "/api/public/library",
            files={"file": ("one.txt", b"hello")},
            data={"scope": "personal"},
        )
        assert response.status_code == 200
        native_id = response.json()["id"]
        retry = await browser.post(f"/api/public/library/{native_id}/retry")
        assert retry.status_code == 200
        assert native.retries == 1
        missing = await browser.get("/api/public/library/missing/download")
        assert missing.status_code == 404
        native.error = "connection"
        failed = await browser.get(f"/api/public/library/{native_id}/download")
        assert failed.status_code == 502
        native.error = "malformed"
        failed = await browser.get(f"/api/public/library/{native_id}/download")
        assert failed.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
async def test_delete_serializes_with_inflight_share(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", _PUBLISHER, "one.txt", b"private"
    )
    await asyncio.gather(
        service.share(original["id"], _PUBLISHER),
        service.delete(original["id"], _PUBLISHER),
    )
    assert native.documents == {}
    assert (await service.list_documents("personal", _PUBLISHER))["items"] == []
    assert (await service.list_documents("public", _BOB))["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
async def test_unshare_waits_until_async_delete_is_no_longer_downloadable(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", _PUBLISHER, "one.txt", b"private"
    )
    published = await service.share(original["id"], _PUBLISHER)
    published_id = published["id"]
    native.deferred_deletes[published_id] = 2
    monkeypatch.setattr(library_module, "_DELETE_POLL_SECONDS", 0.0)
    await service.unshare(original["id"], _PUBLISHER)
    assert published_id not in native.documents
    assert original["id"] in native.documents
    with pytest.raises(HTTPException) as gone:
        await service.download(published_id, _BOB)
    assert gone.value.status_code == 404
    assert (await service.list_documents("personal", _PUBLISHER))["items"][0][
        "shared"
    ] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
async def test_unshare_timeout_keeps_mapping_until_retry_confirms_deleted(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", _PUBLISHER, "one.txt", b"private"
    )
    published = await service.share(original["id"], _PUBLISHER)
    native.deferred_deletes[published["id"]] = 10
    monkeypatch.setattr(library_module, "_DELETE_TIMEOUT_SECONDS", 0.0)
    with pytest.raises(HTTPException) as pending:
        await service.unshare(original["id"], _PUBLISHER)
    assert pending.value.status_code == 502
    assert "撤回仍处理中" in pending.value.detail
    assert (await service.list_documents("personal", _PUBLISHER))["items"][0][
        "shared"
    ] is True
    native.documents.pop(published["id"])
    await service.unshare(original["id"], _PUBLISHER)
    assert (await service.list_documents("personal", _PUBLISHER))["items"][0][
        "shared"
    ] is False
    assert original["id"] in native.documents


@pytest.mark.asyncio
async def test_original_delete_also_waits_until_async_delete_finishes(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"private")
    native.deferred_deletes[original["id"]] = 2
    monkeypatch.setattr(library_module, "_DELETE_POLL_SECONDS", 0.0)
    await service.delete(original["id"], _ALICE)
    assert original["id"] not in native.documents
    with pytest.raises(HTTPException) as gone:
        await service.download(original["id"], _ALICE)
    assert gone.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
async def test_upload_recovers_deleted_kb_without_removing_other_data(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _PUBLISHER, "one.txt", b"alice")
    published = await service.share(original["id"], _PUBLISHER)
    other = await service.upload("personal", _BOB, "two.txt", b"bob")
    deleted_kb_id = native.documents[original["id"]]["knowledge_base_id"]
    other_kb_id = native.documents[other["id"]]["knowledge_base_id"]
    previous_scope = set(native.key_scope)
    native.kbs.pop(deleted_kb_id)
    native.documents.pop(original["id"])

    uploaded = await service.upload("personal", _PUBLISHER, "new.txt", b"new")
    new_kb_id = native.documents[uploaded["id"]]["knowledge_base_id"]
    assert new_kb_id != deleted_kb_id
    assert deleted_kb_id not in service.readable_kbs(_PUBLISHER)
    assert new_kb_id in service.readable_kbs(_PUBLISHER)
    assert set(native.key_scope) == previous_scope - {deleted_kb_id} | {
        new_kb_id
    }
    assert other_kb_id in native.key_scope
    assert (await service.download(other["id"], _BOB)).content == b"bob"
    assert (await service.download(published["id"], _BOB)).content == b"alice"


@pytest.mark.asyncio
async def test_temporary_kb_failure_does_not_recreate_existing_library(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"alice")
    previous_kbs = set(native.kbs)
    previous_scope = set(native.key_scope)
    native.error = "connection"
    with pytest.raises(HTTPException) as failure:
        await service.upload("personal", _ALICE, "new.txt", b"new")
    assert failure.value.status_code == 502
    assert set(native.kbs) == previous_kbs
    assert set(native.key_scope) == previous_scope
    native.error = None
    assert (await service.download(original["id"], _ALICE)).content == b"alice"


@pytest.mark.asyncio
async def test_kb_recovery_keeps_old_registry_until_key_update_succeeds(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"alice")
    old_kb_id = native.documents[original["id"]]["knowledge_base_id"]
    native.kbs.pop(old_kb_id)
    native.documents.pop(original["id"])
    native.reject_key_updates = True
    with pytest.raises(HTTPException) as failure:
        await service.upload("personal", _ALICE, "new.txt", b"new")
    assert failure.value.status_code == 502
    assert old_kb_id in service.readable_kbs(_ALICE)
    assert old_kb_id in native.key_scope
    native.reject_key_updates = False
    uploaded = await service.upload("personal", _ALICE, "new.txt", b"new")
    new_kb_id = native.documents[uploaded["id"]]["knowledge_base_id"]
    assert new_kb_id in service.readable_kbs(_ALICE)
    assert old_kb_id not in service.readable_kbs(_ALICE)
    assert old_kb_id not in native.key_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [_ALICE, _PUBLISHER, _HR, _SUPER])
@pytest.mark.parametrize(
    "scope", ["public", "hr_public", "hr_internal", "personal"]
)
async def test_library_role_matrix_checks_read_download_and_writes(
    library: tuple[LibraryService, _Native, list[str]],
    profile: UserProfile,
    scope: library_module.LibraryScope,
) -> None:
    service, native, _ = library
    creator = profile if scope == "personal" else _SUPER
    original = await service.upload(scope, creator, "matrix.txt", b"matrix")
    identifier = original["id"]
    kb_id = native.documents[identifier]["knowledge_base_id"]
    may_read = scope != "hr_internal" or profile in {_HR, _SUPER}
    may_write = (
        scope == "personal"
        or (scope == "public" and profile in {_PUBLISHER, _SUPER})
        or (scope == "hr_public" and profile in {_PUBLISHER, _HR, _SUPER})
        or (scope == "hr_internal" and profile in {_HR, _SUPER})
    )
    assert service.can_read_kb(kb_id, profile) is may_read
    assert service.can_manage_kb(kb_id, profile) is may_write
    assert service.can_manage_scope(scope, profile) is may_write

    if may_read:
        listing = await service.list_documents(scope, profile)
        assert listing["can_manage"] is may_write
        assert listing["can_share"] is False
        listed = next(
            item for item in listing["items"] if item["id"] == identifier
        )
        assert listed["can_manage"] is may_write
        assert (
            await service.download(identifier, profile)
        ).content == b"matrix"
    else:
        for operation in (
            lambda: service.list_documents(scope, profile),
            lambda: service.download(identifier, profile),
        ):
            with pytest.raises(HTTPException) as denied:
                await operation()
            assert denied.value.status_code == 403

    app = FastAPI()
    register_library_routes(app, cast(GatewayAuth, _Auth(profile)), service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as browser:
        retry = await browser.post(f"/api/public/library/{identifier}/retry")
        assert retry.status_code == (200 if may_write else 403)
        uploaded = await browser.post(
            "/api/public/library",
            files={"file": ("new.txt", b"new")},
            data={"scope": scope},
        )
        assert uploaded.status_code == (200 if may_write else 403)
        deleted = await browser.delete(f"/api/public/library/{identifier}")
        assert deleted.status_code == (200 if may_write else 403)
    assert (identifier in native.documents) is not may_write


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
@pytest.mark.parametrize("profile", [_ALICE, _PUBLISHER, _HR, _SUPER])
async def test_no_role_can_access_another_users_personal_documents(
    library: tuple[LibraryService, _Native, list[str]],
    profile: UserProfile,
) -> None:
    service, native, binding = library
    original = await service.upload("personal", _BOB, "private.txt", b"private")
    identifier = original["id"]
    kb_id = native.documents[identifier]["knowledge_base_id"]
    # 误把私库加入公共绑定也不能改变原来的个人归属。
    binding.append(kb_id)
    assert kb_id not in service.public_kb_ids
    assert kb_id not in service.readable_kbs(profile)
    assert kb_id in service.restricted_kbs(profile)
    assert not service.can_read_kb(kb_id, profile)
    assert not service.can_manage_kb(kb_id, profile)
    assert not service.can_read_kb("unregistered-kb", profile)
    assert not service.can_manage_kb("unregistered-kb", profile)
    assert kb_id not in service.search_kbs(profile, ("public", "personal"))
    assert (await service.list_documents("personal", profile))["items"] == []
    assert (await service.list_documents("public", profile))["items"] == []

    for operation in (
        lambda: service.document(identifier, profile),
        lambda: service.download(identifier, profile),
        lambda: service.delete(identifier, profile),
        lambda: service.share(identifier, profile),
        lambda: service.unshare(identifier, profile),
    ):
        with pytest.raises(HTTPException) as denied:
            await operation()
        assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        service.check_turn(profile, {"kb_scope_json": json.dumps([kb_id])})
    assert denied.value.status_code == 403
    app = FastAPI()
    register_library_routes(app, cast(GatewayAuth, _Auth(profile)), service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as browser:
        retry = await browser.post(f"/api/public/library/{identifier}/retry")
    assert retry.status_code == 403
    assert native.retries == 0
    assert (await service.download(identifier, _BOB)).content == b"private"
    assert service._scope_for_kb(kb_id)["scope"] == "personal"


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
@pytest.mark.parametrize("profile", [_ALICE, _HR])
async def test_public_upload_cannot_be_bypassed_by_sharing_or_source_id(
    library: tuple[LibraryService, _Native, list[str]],
    profile: UserProfile,
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", profile, "private.txt", b"private"
    )
    identifier = original["id"]
    previous_documents = set(native.documents)
    for operation in (
        lambda: service.upload("public", profile, "new.txt", b"new"),
        lambda: service.upload(
            "public", profile, "new.txt", b"new", source_id=identifier
        ),
        lambda: service.share(identifier, profile),
        lambda: service.unshare(identifier, profile),
    ):
        with pytest.raises(HTTPException) as denied:
            await operation()
        assert denied.value.status_code == 403
    assert set(native.documents) == previous_documents
    assert (await service.list_documents("personal", profile))[
        "can_share"
    ] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
async def test_downgraded_publisher_can_only_delete_private_original(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", _PUBLISHER, "one.txt", b"private"
    )
    published = await service.share(original["id"], _PUBLISHER)
    public_id = published["id"]
    for operation in (
        lambda: service.delete(public_id, _ALICE),
        lambda: service.share(original["id"], _ALICE),
        lambda: service.unshare(original["id"], _ALICE),
    ):
        with pytest.raises(HTTPException) as denied:
            await operation()
        assert denied.value.status_code == 403
    assert (await service.download(public_id, _ALICE)).content == b"private"
    await service.delete(original["id"], _ALICE)
    assert original["id"] not in native.documents
    assert public_id in native.documents
    assert (await service.download(public_id, _BOB)).content == b"private"
    await service.delete(public_id, _SUPER)
    assert public_id not in native.documents


@pytest.mark.asyncio
async def test_recreated_personal_kb_keeps_old_history_private(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, binding = library
    original = await service.upload("personal", _BOB, "old.txt", b"old-private")
    old_kb_id = native.documents[original["id"]]["knowledge_base_id"]
    old_turn = {"kb_scope_json": json.dumps([old_kb_id])}
    native.kbs.pop(old_kb_id)
    native.documents.pop(original["id"])
    replacement = await service.upload("personal", _BOB, "new.txt", b"new")
    new_kb_id = native.documents[replacement["id"]]["knowledge_base_id"]
    assert old_kb_id != new_kb_id
    assert old_kb_id not in service.readable_kbs(_BOB)
    assert new_kb_id in service.readable_kbs(_BOB)
    assert service.personal_kbs() == {
        old_kb_id: _BOB.user_id,
        new_kb_id: _BOB.user_id,
    }
    # 历史登记不增加检索集合，错误公共绑定也不能让旧私库重新公开。
    binding.append(old_kb_id)
    assert old_kb_id not in service.public_kb_ids
    assert service._scope_for_kb(old_kb_id) == {
        "kb_id": old_kb_id,
        "scope": "personal",
        "owner_id": _BOB.user_id,
    }
    for profile in (_ALICE, _HR, _PUBLISHER, _SUPER):
        assert old_kb_id in service.restricted_kbs(profile)
        assert new_kb_id in service.restricted_kbs(profile)
        assert not service.can_read_kb(old_kb_id, profile)
        assert not service.can_manage_kb(old_kb_id, profile)
        assert old_kb_id not in service.search_kbs(profile, ("public",))
        with pytest.raises(HTTPException) as denied:
            service.check_turn(profile, old_turn)
        assert denied.value.status_code == 403
    assert old_kb_id not in service.restricted_kbs(_BOB)


@pytest.mark.asyncio
async def test_existing_personal_registration_is_backfilled_on_restart(
    library: tuple[LibraryService, _Native, list[str]],
    tmp_path: Path,
) -> None:
    service, _, binding = library
    original = await service.upload("personal", _BOB, "old.txt", b"private")
    kb_id = next(
        row["kb_id"]
        for row in service.registered_kbs()
        if row["owner_id"] == _BOB.user_id
    )
    # 模拟升级前没有历史归属表的旧数据库。
    with service._connections.transaction(write=True) as connection:
        connection.execute("DROP TABLE library_personal_kbs")
    reloaded = LibraryService(
        tmp_path / "gateway.sqlite3",
        service._native,
        ("public",),
        hashlib.sha256(_SERVICE_KEY.encode()).digest(),
        public_scope=lambda: tuple(binding),
    )
    assert reloaded.personal_kbs() == {kb_id: _BOB.user_id}
    assert (await reloaded.download(original["id"], _BOB)).content == b"private"
    assert kb_id in reloaded.restricted_kbs(_SUPER)
    # 当前登记被恢复流程替换后，历史归属仍不能被公共绑定覆盖。
    with reloaded._connections.transaction(write=True) as connection:
        connection.execute("DELETE FROM library_kbs WHERE kb_id=?", (kb_id,))
    binding.append(kb_id)
    assert kb_id not in reloaded.public_kb_ids
    assert kb_id in reloaded.restricted_kbs(_SUPER)
    assert reloaded._scope_for_kb(kb_id)["owner_id"] == _BOB.user_id


@pytest.mark.asyncio
async def test_failed_personal_key_grant_preserves_created_kb_owner(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, binding = library
    previous_kbs = set(native.kbs)
    native.reject_key_updates = True
    with pytest.raises(HTTPException) as unavailable:
        await service.upload("personal", _BOB, "new.txt", b"private")
    assert unavailable.value.status_code == 502
    created_kb_id = (set(native.kbs) - previous_kbs).pop()
    assert service.registered_kbs() == []
    assert service.personal_kbs()[created_kb_id] == _BOB.user_id
    binding.append(created_kb_id)
    assert created_kb_id not in service.public_kb_ids
    assert created_kb_id in service.restricted_kbs(_SUPER)
    assert not service.can_read_kb(created_kb_id, _SUPER)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [_ALICE, _PUBLISHER, _HR, _SUPER])
async def test_personal_sharing_is_closed_by_default_for_every_role(
    library: tuple[LibraryService, _Native, list[str]],
    profile: UserProfile,
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", profile, "private.txt", b"private"
    )
    identifier = original["id"]
    listing = await service.list_documents("personal", profile)
    assert listing["can_manage"] is True
    assert listing["can_share"] is False
    initial_requests = list(native.requests)
    for operation in (
        lambda: service.share(identifier, profile),
        lambda: service.unshare(identifier, profile),
        lambda: service.upload(
            "public", profile, "copy.txt", b"private", source_id=identifier
        ),
    ):
        with pytest.raises(HTTPException) as denied:
            await operation()
        assert denied.value.status_code == 403
        assert denied.value.detail == "个人资料共享暂未开放。"
    app = FastAPI()
    register_library_routes(app, cast(GatewayAuth, _Auth(profile)), service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as browser:
        for action in ("share", "unshare"):
            response = await (
                browser.post(f"/api/public/library/{identifier}/share")
                if action == "share"
                else browser.delete(f"/api/public/library/{identifier}/share")
            )
            assert response.status_code == 403
            assert response.json()["detail"] == "个人资料共享暂未开放。"
    assert native.requests == initial_requests
    assert set(native.documents) == {identifier}


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
@pytest.mark.parametrize("profile", [_ALICE, _PUBLISHER, _HR, _SUPER])
async def test_explicit_sharing_opt_in_still_requires_public_write_permission(
    library: tuple[LibraryService, _Native, list[str]],
    profile: UserProfile,
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", profile, "private.txt", b"private"
    )
    may_share = profile in {_PUBLISHER, _SUPER}
    assert (await service.list_documents("personal", profile))["can_share"] is (
        may_share
    )
    if may_share:
        published = await service.share(original["id"], profile)
        assert (
            native.documents[published["id"]]["knowledge_base_id"] == "public"
        )
        assert (
            await service.download(published["id"], _BOB)
        ).content == b"private"
    else:
        with pytest.raises(HTTPException) as denied:
            await service.share(original["id"], profile)
        assert denied.value.status_code == 403
        assert len(native.documents) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("library", [True], indirect=True)
@pytest.mark.parametrize("profile", [_PUBLISHER, _SUPER])
async def test_disabling_sharing_keeps_old_public_copy_when_deleting_private(
    library: tuple[LibraryService, _Native, list[str]],
    profile: UserProfile,
) -> None:
    service, native, _ = library
    original = await service.upload(
        "personal", profile, "private.txt", b"private"
    )
    published = await service.share(original["id"], profile)
    service._personal_sharing_enabled = False
    assert (await service.list_documents("personal", profile))[
        "can_share"
    ] is False
    await service.delete(original["id"], profile)
    assert original["id"] not in native.documents
    assert published["id"] in native.documents
    assert (await service.download(published["id"], _BOB)).content == b"private"
    # 公共副本仍可由有权的管理员独立删除。
    await service.delete(published["id"], profile)
    assert published["id"] not in native.documents


@pytest.mark.asyncio
async def test_personal_quota_warns_blocks_new_files_and_delete_releases_space(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    service.set_personal_limits(_ALICE.user_id, 100, 50)
    first = await service.upload("personal", _ALICE, "one.txt", b"a" * 80)
    quota = (await service.list_documents("personal", _ALICE))["quota"]
    assert quota["used_bytes"] == 80 and quota["warning"] is True
    assert quota["full"] is False
    second = await service.upload("personal", _ALICE, "two.txt", b"b" * 20)
    quota = (await service.list_documents("personal", _ALICE))["quota"]
    assert quota["used_bytes"] == 100 and quota["used_files"] == 2
    assert quota["full"] is True and quota["upload_allowed"] is False
    with pytest.raises(HTTPException) as full:
        await service.upload("personal", _ALICE, "three.txt", b"c")
    assert full.value.status_code == 409
    assert len(native.files) == 2
    assert (await service.download(first["id"], _ALICE)).content == b"a" * 80
    assert (await service.download(second["id"], _ALICE)).content == b"b" * 20
    await service.delete(first["id"], _ALICE)
    quota = (await service.list_documents("personal", _ALICE))["quota"]
    assert quota["used_bytes"] == 20 and quota["used_files"] == 1
    assert quota["upload_allowed"] is True
    assert (await service.list_documents("personal", _BOB))["quota"][
        "used_bytes"
    ] == 0


@pytest.mark.asyncio
async def test_public_and_hr_uploads_do_not_use_or_expose_personal_quota(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    service.set_personal_limits(_PUBLISHER.user_id, 1, 1)
    for scope, profile in (
        ("public", _PUBLISHER),
        ("hr_public", _HR),
        ("hr_internal", _HR),
    ):
        uploaded = await service.upload(
            cast(library_module.LibraryScope, scope),
            profile,
            f"{scope}.txt",
            b"large" * 20,
        )
        assert uploaded["id"] in native.documents
        assert "quota" not in await service.list_documents(
            cast(library_module.LibraryScope, scope), profile
        )
        assert service._quota.jobs(profile.user_id) == []
    assert (await service.list_documents("personal", _PUBLISHER))["quota"][
        "used_files"
    ] == 0


@pytest.mark.asyncio
async def test_personal_queue_limit_and_local_original_owner_isolation(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    native.parse_status = "processing"
    first = await service.upload("personal", _ALICE, "active.txt", b"active")
    queued = [
        await service.upload(
            "personal", _ALICE, f"queued-{index}.txt", b"queued"
        )
        for index in range(5)
    ]
    assert len(native.files) == 1
    listed = await service.list_documents("personal", _ALICE)
    assert len(listed["items"]) == 6
    assert listed["quota"]["active_parses"] == 1
    assert listed["quota"]["queued_parses"] == 5
    with pytest.raises(HTTPException) as full:
        await service.upload("personal", _ALICE, "seventh.txt", b"seven")
    assert full.value.status_code == 429
    local = queued[0]["id"]
    assert (await service.download(local, _ALICE)).content == b"queued"
    for profile in (_BOB, _PUBLISHER, _SUPER):
        # 同 owner 的管理员仍是本人，另两人和超管绝不获得访问权限。
        if profile.user_id == _ALICE.user_id:
            continue
        with pytest.raises(HTTPException) as denied:
            await service.download(local, profile)
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException):
            await service.delete(local, profile)
    native_delete_count = sum(
        method == "DELETE" for method, _ in native.requests
    )
    await service.delete(local, _ALICE)
    assert (
        sum(method == "DELETE" for method, _ in native.requests)
        == native_delete_count
    )
    quota = (await service.list_documents("personal", _ALICE))["quota"]
    assert quota["used_files"] == 5 and quota["queued_parses"] == 4
    assert native.documents[first["id"]]["parse_status"] == "processing"
    other = await service.upload("personal", _BOB, "other.txt", b"other")
    assert other["id"] in native.documents and len(native.files) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_next_personal_file_starts_only_after_native_terminal(
    library: tuple[LibraryService, _Native, list[str]], terminal: str
) -> None:
    service, native, _ = library
    native.parse_status = "processing"
    first = await service.upload("personal", _ALICE, "first.txt", b"first")
    second = await service.upload("personal", _ALICE, "second.txt", b"second")
    await service._advance_personal(_ALICE.user_id)
    assert len(native.files) == 1
    native.documents[first["id"]]["parse_status"] = "finalizing"
    await service._advance_personal(_ALICE.user_id)
    assert len(native.files) == 1
    native.documents[first["id"]]["parse_status"] = terminal
    await service._advance_personal(_ALICE.user_id)
    assert len(native.files) == 2
    job = service._quota.job(second["id"])
    assert job is not None and job.native_id in native.documents
    assert (
        sum(
            item["parse_status"] == "processing"
            for item in native.documents.values()
        )
        == 1
    )
    assert (await service.download(second["id"], _ALICE)).content == b"second"


@pytest.mark.asyncio
async def test_untracked_native_personal_work_also_holds_the_slot(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    first = await service.upload("personal", _ALICE, "one.txt", b"one")
    kb_id = native.documents[first["id"]]["knowledge_base_id"]
    native.documents["legacy"] = {
        "id": "legacy",
        "knowledge_base_id": kb_id,
        "parse_status": "processing",
        "file_name": "legacy.txt",
        "file_size": 6,
    }
    native.files["legacy"] = b"legacy"
    queued = await service.upload("personal", _ALICE, "queued.txt", b"new")
    assert queued["status"] == "queued" and len(native.files) == 2
    listed = await service.list_documents("personal", _ALICE)
    assert listed["quota"]["active_parses"] == 1
    assert listed["quota"]["queued_parses"] == 1
    native.documents["legacy"]["parse_status"] = "completed"
    await service._advance_personal(_ALICE.user_id)
    assert len(native.files) == 3


@pytest.mark.asyncio
async def test_response_loss_is_adopted_by_upload_marker_after_restart(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    native.parse_status = "processing"
    native.lose_upload_response = True
    response = await service.upload("personal", _ALICE, "one.txt", b"original")
    assert response["status"] == "failed" and len(native.files) == 1
    job_id = response["id"]
    assert service._spool_file(job_id).read_bytes() == b"original"
    restarted = LibraryService(
        service._connections.database_path,
        service._native,
        ("public",),
        hashlib.sha256(_SERVICE_KEY.encode()).digest(),
    )
    await restarted._advance_personal(_ALICE.user_id)
    job = restarted._quota.job(job_id)
    assert job is not None and job.native_id in native.documents
    assert job.state == "processing"
    assert len(native.files) == 1
    assert not restarted._spool_file(job_id).exists()
    quota = (await restarted.list_documents("personal", _ALICE))["quota"]
    assert quota["used_files"] == 1 and quota["used_bytes"] == len(b"original")
    assert (await restarted.download(job_id, _ALICE)).content == b"original"


@pytest.mark.asyncio
async def test_uncertain_upload_keeps_original_and_stops_automatic_next_file(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    native.upload_error_status = 503
    first = await service.upload("personal", _ALICE, "one.txt", b"original")
    assert first["status"] == "failed" and native.files == {}
    assert (await service.download(first["id"], _ALICE)).content == b"original"
    native.upload_error_status = None
    await service.upload("personal", _ALICE, "two.txt", b"queued")
    await service._advance_personal(_ALICE.user_id)
    assert native.files == {}
    quota = (await service.list_documents("personal", _ALICE))["quota"]
    assert quota["active_parses"] == 1 and quota["queued_parses"] == 1
    assert quota["used_files"] == 2
    with pytest.raises(HTTPException) as waiting:
        await service.retry(first["id"], _ALICE)
    assert waiting.value.status_code == 409
    assert native.files == {}
    with service._connections.transaction(write=True) as connection:
        connection.execute(
            "UPDATE library_personal_jobs SET lease_until=0,updated_at=0 "
            "WHERE job_id=?",
            (first["id"],),
        )
    resumed = await service.retry(first["id"], _ALICE)
    assert resumed["id"] in native.documents and len(native.files) == 1
    assert (await service.list_documents("personal", _ALICE))["quota"][
        "used_files"
    ] == 2


@pytest.mark.asyncio
async def test_personal_retry_uses_same_slot_without_double_counting_full_quota(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"x")
    service.set_personal_limits(_ALICE.user_id, 1, 1)
    native.parse_status = "processing"
    await service.retry(original["id"], _ALICE)
    assert native.retries == 1
    listed = await service.list_documents("personal", _ALICE)
    assert listed["quota"]["used_bytes"] == 1
    assert listed["quota"]["used_files"] == 1
    assert listed["quota"]["active_parses"] == 1
    with pytest.raises(HTTPException) as busy:
        await service.retry(original["id"], _ALICE)
    assert busy.value.status_code == 409 and native.retries == 1
    await service.delete(original["id"], _ALICE)
    assert (await service.list_documents("personal", _ALICE))["quota"][
        "used_files"
    ] == 0


@pytest.mark.asyncio
async def test_worker_start_close_advances_persisted_queue_once(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, native, _ = library
    native.parse_status = "processing"
    first = await service.upload("personal", _ALICE, "one.txt", b"one")
    await service.upload("personal", _ALICE, "two.txt", b"two")
    native.documents[first["id"]]["parse_status"] = "completed"
    monkeypatch.setattr(library_module, "_PERSONAL_POLL_SECONDS", 0.01)
    await service.start()
    await service.start()
    try:
        async with asyncio.timeout(2):
            while len(native.files) != 2:
                await asyncio.sleep(0.01)
    finally:
        await service.close()
    assert len(native.files) == 2 and service._personal_worker is None


@pytest.mark.asyncio
async def test_staging_is_not_downloadable_and_cancel_waits_for_disk_writer(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, native, _ = library
    started, finish = Event(), Event()
    original_fsync = library_module.os.fsync

    def paused_fsync(descriptor: int) -> None:
        original_fsync(descriptor)
        started.set()
        finish.wait(timeout=3)

    monkeypatch.setattr(library_module.os, "fsync", paused_fsync)
    upload = asyncio.create_task(
        service.upload("personal", _ALICE, "one.txt", b"original")
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        job = service._quota.jobs(_ALICE.user_id)[0]
        assert not service._spool_file(job.job_id).exists()
        assert service._spool_file(job.job_id).with_suffix(".part").exists()
        with pytest.raises(HTTPException) as partial:
            await service.download(job.job_id, _ALICE)
        assert partial.value.status_code == 409
        upload.cancel()
        await asyncio.sleep(0)
        assert not upload.done()
        assert service._quota.summary(_ALICE.user_id, [])["used_files"] == 1
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await upload
    assert service._quota.jobs(_ALICE.user_id) == []
    assert list(service._quota.spool_path.iterdir()) == []
    assert native.files == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["owners", "advance"])
async def test_worker_recovers_from_temporary_sqlite_busy(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    service, native, _ = library
    native.parse_status = "processing"
    first = await service.upload("personal", _ALICE, "one.txt", b"one")
    await service.upload("personal", _ALICE, "two.txt", b"two")
    native.documents[first["id"]]["parse_status"] = "completed"
    attempts = [0]
    original_owners = service._quota.owner_jobs
    original_advance = service._advance_personal

    def owners() -> list[str]:
        if stage == "owners":
            attempts[0] += 1
            if attempts[0] == 1:
                raise ProviderUnavailable("暂忙", stage="sqlite.connect")
        return original_owners()

    async def advance(owner_id: str) -> None:
        if stage == "advance":
            attempts[0] += 1
            if attempts[0] == 1:
                raise ProviderUnavailable("暂忙", stage="sqlite.transaction")
        await original_advance(owner_id)

    monkeypatch.setattr(service._quota, "owner_jobs", owners)
    monkeypatch.setattr(service, "_advance_personal", advance)
    monkeypatch.setattr(library_module, "_PERSONAL_POLL_SECONDS", 0.01)
    await service.start()
    try:
        async with asyncio.timeout(2):
            while len(native.files) != 2:
                await asyncio.sleep(0.01)
        assert attempts[0] >= 2
        assert service._personal_worker is not None
        assert not service._personal_worker.done()
    finally:
        await service.close()
    assert len(native.files) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_retry_old_completed_receipt_does_not_release_personal_slot(
    library: tuple[LibraryService, _Native, list[str]], lost_response: bool
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"original")
    native.delay_retry_state = True
    native.lose_retry_response = lost_response
    submitted = await service.retry(original["id"], _ALICE)
    assert submitted["status"] == ("failed" if lost_response else "pending")
    assert native.retries == 1
    second = await service.upload("personal", _ALICE, "two.txt", b"second")
    assert second["status"] == "queued"
    await service._advance_personal(_ALICE.user_id)
    listed = await service.list_documents("personal", _ALICE)
    assert listed["quota"]["active_parses"] == 1
    assert listed["quota"]["queued_parses"] == 1
    assert len(native.files) == 1
    assert (
        await service.download(original["id"], _ALICE)
    ).content == b"original"
    with pytest.raises(HTTPException) as unconfirmed:
        await service.delete(original["id"], _ALICE)
    assert unconfirmed.value.status_code == 409
    # 原生真实开始新尝试后才接纳状态，绝不把旧 completed 当新结果。
    native.documents[original["id"]]["parse_status"] = "processing"
    await service._advance_personal(_ALICE.user_id)
    assert len(native.files) == 1
    native.documents[original["id"]]["parse_status"] = "completed"
    native.documents[original["id"]]["updated_at"] = "confirmed-new-attempt"
    await service._advance_personal(_ALICE.user_id)
    assert len(native.files) == 2
    assert (
        service._quota.summary(_ALICE.user_id, list(native.documents.values()))[
            "used_files"
        ]
        == 2
    )
