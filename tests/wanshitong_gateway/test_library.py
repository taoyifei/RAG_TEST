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
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request

import wanshitong_gateway.library as library_module
from wanshitong_gateway.auth import GatewayAuth
from wanshitong_gateway.library import LibraryService, register_library_routes
from wanshitong_gateway.roles import UserProfile
from wanshitong_gateway.weknora.admin import NativeAdminClient

_ALICE = UserProfile("a", "alice", "甲", None, "user")
_BOB = UserProfile("b", "bob", "乙", None, "user")
_HR = UserProfile("h", "hr", "人力", "42", "hr")
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

    def respond(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - 模拟固定原生接口。
        path = request.url.path
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
            "parse_status": "completed",
            "file_size": len(content),
        }
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
    )
    try:
        yield service, state, public_scope
    finally:
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
async def test_shared_pdf_keeps_builtin_override_without_changing_other_uploads(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "guide.pdf", b"pdf")
    published = await service.share(original["id"], _ALICE)
    fields = native.upload_fields[published["id"]]
    assert (
        json.loads(fields["process_config"])["parser_engine_rules"][0]["engine"]
        == "builtin"
    )
    assert json.loads(fields["metadata"])["wxd_source_id"] == original["id"]
    markdown = await service.upload("personal", _ALICE, "guide.md", b"text")
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
        lambda: service.list_documents("hr_public", _ALICE),
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
async def test_share_withdraw_and_delete_keep_private_original(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"private")
    shared = await asyncio.gather(
        service.share(original["id"], _ALICE),
        service.share(original["id"], _ALICE),
    )
    assert shared[0]["id"] == shared[1]["id"]
    published_id = shared[0]["id"]
    assert (await service.download(published_id, _BOB)).content == b"private"
    with pytest.raises(HTTPException) as denied:
        await service.delete(published_id, _BOB)
    assert denied.value.status_code == 403
    await service.unshare(original["id"], _ALICE)
    assert published_id not in native.documents
    assert original["id"] in native.documents
    assert (await service.list_documents("personal", _ALICE))["items"][0][
        "shared"
    ] is False
    await service.share(original["id"], _ALICE)
    await service.delete(original["id"], _ALICE)
    assert native.documents == {}


class _Auth:
    def require_profile(self, request: Request) -> UserProfile:
        del request
        return _ALICE


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
async def test_delete_serializes_with_inflight_share(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"private")
    await asyncio.gather(
        service.share(original["id"], _ALICE),
        service.delete(original["id"], _ALICE),
    )
    assert native.documents == {}
    assert (await service.list_documents("personal", _ALICE))["items"] == []
    assert (await service.list_documents("public", _BOB))["items"] == []


@pytest.mark.asyncio
async def test_unshare_waits_until_async_delete_is_no_longer_downloadable(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"private")
    published = await service.share(original["id"], _ALICE)
    published_id = published["id"]
    native.deferred_deletes[published_id] = 2
    monkeypatch.setattr(library_module, "_DELETE_POLL_SECONDS", 0.0)
    await service.unshare(original["id"], _ALICE)
    assert published_id not in native.documents
    assert original["id"] in native.documents
    with pytest.raises(HTTPException) as gone:
        await service.download(published_id, _BOB)
    assert gone.value.status_code == 404
    assert (await service.list_documents("personal", _ALICE))["items"][0][
        "shared"
    ] is False


@pytest.mark.asyncio
async def test_unshare_timeout_keeps_mapping_until_retry_confirms_deleted(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"private")
    published = await service.share(original["id"], _ALICE)
    native.deferred_deletes[published["id"]] = 10
    monkeypatch.setattr(library_module, "_DELETE_TIMEOUT_SECONDS", 0.0)
    with pytest.raises(HTTPException) as pending:
        await service.unshare(original["id"], _ALICE)
    assert pending.value.status_code == 502
    assert "撤回仍处理中" in pending.value.detail
    assert (await service.list_documents("personal", _ALICE))["items"][0][
        "shared"
    ] is True
    native.documents.pop(published["id"])
    await service.unshare(original["id"], _ALICE)
    assert (await service.list_documents("personal", _ALICE))["items"][0][
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
async def test_upload_recovers_deleted_kb_without_removing_other_data(
    library: tuple[LibraryService, _Native, list[str]],
) -> None:
    service, native, _ = library
    original = await service.upload("personal", _ALICE, "one.txt", b"alice")
    published = await service.share(original["id"], _ALICE)
    other = await service.upload("personal", _BOB, "two.txt", b"bob")
    deleted_kb_id = native.documents[original["id"]]["knowledge_base_id"]
    other_kb_id = native.documents[other["id"]]["knowledge_base_id"]
    previous_scope = set(native.key_scope)
    native.kbs.pop(deleted_kb_id)
    native.documents.pop(original["id"])

    uploaded = await service.upload("personal", _ALICE, "new.txt", b"new")
    new_kb_id = native.documents[uploaded["id"]]["knowledge_base_id"]
    assert new_kb_id != deleted_kb_id
    assert deleted_kb_id not in service.readable_kbs(_ALICE)
    assert new_kb_id in service.readable_kbs(_ALICE)
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
