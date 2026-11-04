"""指定文件之外的资料版本、原件和附件权限回归。"""

from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException

from tests.wanshitong_gateway.test_library import (
    _ALICE,
    _BOB,
    _HR,
    _PUBLISHER,
    _SUPER,
    _Native,
)
from wanshitong_gateway.document_catalogue import guide_group, infer_kind
from wanshitong_gateway.library import LibraryService
from wanshitong_gateway.roles import UserProfile

pytest_plugins = ("tests.wanshitong_gateway.test_library",)


def _hash(native: _Native, identifier: str) -> None:
    native.documents[identifier]["file_hash"] = hashlib.md5(
        native.files[identifier], usedforsecurity=False
    ).hexdigest()


@pytest.mark.asyncio
async def test_duplicate_reuses_owned_document_without_quota_reservation(
    library: tuple[LibraryService, _Native, list[str]],
):
    service, native, _ = library
    first = await service.receive_upload("personal", _ALICE, "one.txt", b"same")
    _hash(native, first["id"])
    before = (await service.list_documents("personal", _ALICE))["quota"]
    duplicate = await service.receive_upload(
        "personal", _ALICE, "other.txt", b"same"
    )
    after = (await service.list_documents("personal", _ALICE))["quota"]
    assert duplicate["id"] == first["id"]
    assert duplicate["duplicate"] is True
    assert after["used_files"] == before["used_files"] == 1
    assert after["used_bytes"] == before["used_bytes"] == 4
    other = await service.receive_upload("personal", _BOB, "one.txt", b"same")
    assert other["id"] != first["id"]
    assert not other.get("duplicate")


@pytest.mark.asyncio
async def test_staged_version_requires_health_then_excludes_previous(
    library: tuple[LibraryService, _Native, list[str]],
):
    service, native, _ = library
    old = await service.receive_upload(
        "public", _PUBLISHER, "policy.txt", b"old"
    )
    staged = await service.receive_upload(
        "public", _PUBLISHER, "policy.txt", b"new", replaces_id=old["id"]
    )
    assert await service.searchable_document_ids(("public",)) == (old["id"],)
    native.documents[staged["id"]]["parse_status"] = "failed"
    with pytest.raises(HTTPException) as failed:
        await service.publish_version(staged["id"], _PUBLISHER)
    assert failed.value.status_code == 409
    assert service.catalogue.searchable(old["id"])
    native.documents[staged["id"]]["parse_status"] = "completed"
    await service.publish_version(staged["id"], _PUBLISHER)
    assert await service.searchable_document_ids(("public",)) == (staged["id"],)
    assert (await service.download(old["id"], _BOB)).content == b"old"
    rows = (await service.list_documents("public", _ALICE))["items"]
    assert {row["id"]: row["version_state"] for row in rows} == {
        old["id"]: "archived",
        staged["id"]: "current",
    }


@pytest.mark.asyncio
async def test_personal_versions_remain_in_personal_quota_and_owner_only(
    library: tuple[LibraryService, _Native, list[str]],
):
    service, _, _ = library
    old = await service.receive_upload("personal", _ALICE, "own.txt", b"old")
    staged = await service.receive_upload(
        "personal", _ALICE, "own.txt", b"new", replaces_id=old["id"]
    )
    await service.publish_version(staged["id"], _ALICE)
    summary = (await service.list_documents("personal", _ALICE))["quota"]
    assert (summary["used_files"], summary["used_bytes"]) == (2, 6)
    for actor in (_BOB, _SUPER):
        with pytest.raises(HTTPException) as denied:
            await service.receive_upload(
                "personal", actor, "hack.txt", b"no", replaces_id=staged["id"]
            )
        assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied:
        await service.add_document_asset(
            staged["id"], _ALICE, "bypass.xlsx", b"no", "template"
        )
    assert denied.value.status_code == 422


@pytest.mark.asyncio
async def test_missing_original_cannot_publish_and_exact_hash_is_required(
    library: tuple[LibraryService, _Native, list[str]],
):
    service, native, _ = library
    old = await service.receive_upload("public", _PUBLISHER, "a.txt", b"old")
    new = await service.receive_upload(
        "public", _PUBLISHER, "b.txt", b"new", replaces_id=old["id"]
    )
    native.documents[new["id"]]["file_hash"] = "wrong"
    health = await service.check_document_health(new["id"], _ALICE)
    assert health["original_available"] is True
    assert health["hash_ok"] is False
    assert health["ready"] is False
    with pytest.raises(HTTPException) as denied:
        await service.publish_version(new["id"], _PUBLISHER)
    assert denied.value.status_code == 409
    assert service.catalogue.searchable(old["id"])


@pytest.mark.asyncio
async def test_internal_hr_asset_is_never_readable_by_regular_admin(
    library: tuple[LibraryService, _Native, list[str]],
):
    service, _, _ = library
    internal = await service.receive_upload(
        "hr_internal", _HR, "hr.txt", b"private"
    )
    asset = await service.add_document_asset(
        internal["id"], _HR, "form.xlsx", b"actual-template", "template"
    )
    assert (
        await service.document_asset(internal["id"], asset["id"], _HR)
    ).content == b"actual-template"
    for actor in (_ALICE, _PUBLISHER):
        with pytest.raises(HTTPException) as denied:
            await service.document_asset(internal["id"], asset["id"], actor)
        assert denied.value.status_code == 403
    public = await service.receive_upload(
        "public", _PUBLISHER, "pub.txt", b"public"
    )
    with pytest.raises(HTTPException) as missing:
        await service.document_asset(public["id"], asset["id"], _ALICE)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_same_name_upload_requires_deliberate_version_update(
    library: tuple[LibraryService, _Native, list[str]],
):
    service, native, _ = library
    original = await service.receive_upload(
        "public", _PUBLISHER, "same.txt", b"old"
    )
    with pytest.raises(HTTPException) as denied:
        await service.receive_upload("public", _PUBLISHER, "same.txt", b"new")
    assert denied.value.status_code == 409
    assert list(native.documents) == [original["id"]]


def test_existing_screenshot_guides_are_not_claimed_to_be_editable_templates():
    assert infer_kind("0302_示例模板与图示.docx") == "illustration"
    assert guide_group("0307_湾区研究院业务招待费 报账操作指引.docx")
    assert not guide_group("0309_湾区研究院业务招待费.docx")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,actor", [("personal", _ALICE), ("public", _PUBLISHER)]
)
async def test_lost_version_receipt_stays_unpublished_after_reconciliation(
    library: tuple[LibraryService, _Native, list[str]],
    scope: str,
    actor: UserProfile,
):
    service, native, _ = library
    old = await service.receive_upload(scope, actor, "old.txt", b"old")
    native.lose_upload_response = True
    try:
        await service.receive_upload(
            scope, actor, "next.txt", b"new", replaces_id=old["id"]
        )
    except HTTPException as error:
        assert error.status_code == 502
    listing = await service.list_documents(scope, actor)
    new = next(item for item in listing["items"] if item["id"] != old["id"])
    assert new["version_state"] == "staging"
    kb_id = native.documents[old["id"]]["knowledge_base_id"]
    assert await service.searchable_document_ids((kb_id,)) == (old["id"],)
    await service.publish_version(new["id"], actor)
    assert await service.searchable_document_ids((kb_id,)) == (new["id"],)


@pytest.mark.asyncio
async def test_native_download_server_error_is_unknown_health_not_success(
    library: tuple[LibraryService, _Native, list[str]],
    monkeypatch: pytest.MonkeyPatch,
):
    service, _, _ = library
    document = await service.receive_upload(
        "public", _PUBLISHER, "a.txt", b"old"
    )

    async def unavailable(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise HTTPException(502, "原生资料服务暂时不可用。")

    monkeypatch.setattr(service, "download", unavailable)
    health = await service.check_document_health(document["id"], _ALICE)
    assert health["ready"] is False
    assert health["original_available"] is None
    assert health["check_error"]
    assert service.catalogue.entry(document["id"])["health_json"]


@pytest.mark.asyncio
async def test_replacement_keeps_its_original_published_kb(
    library: tuple[LibraryService, _Native, list[str]],
):
    service, native, binding = library
    old = await service.receive_upload("public", _PUBLISHER, "old.txt", b"old")
    binding.append("public-new")
    native.documents[old["id"]]["knowledge_base_id"] = "public-new"
    new = await service.receive_upload(
        "public", _PUBLISHER, "new.txt", b"new", replaces_id=old["id"]
    )
    assert native.documents[new["id"]]["knowledge_base_id"] == "public-new"
    await service.publish_version(new["id"], _PUBLISHER)


@pytest.mark.asyncio
async def test_public_listing_preserves_hr_document_write_scope(
    library: tuple[LibraryService, _Native, list[str]],
):
    service, _, _ = library
    document = await service.receive_upload(
        "hr_public", _HR, "hr.txt", b"hr-public"
    )
    listing = await service.list_documents("public", _HR)
    row = next(
        item for item in listing["items"] if item["id"] == document["id"]
    )
    assert listing["can_manage"] is False
    assert row["can_manage"] is True
    assert row["scope"] == "hr_public"
