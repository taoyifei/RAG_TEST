"""湾事通 DOCX-only 上传、安全路径与独立 Job 回归。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.upload_validation import (
    DOCX_MEDIA_TYPE,
    DOCX_ONLY_MESSAGE,
)
from tests.adapters.parsers.docx.fixtures import build_package
from tests.wanshitong.support import PublicHarness


def _docx(text: str) -> bytes:
    return build_package(
        "<w:p><w:r><w:t>" + text + "</w:t></w:r></w:p>"
    )


def _upload(
    harness: PublicHarness,
    content: bytes,
    relative_path: str,
    idempotency_key: str,
    *,
    media_type: str = DOCX_MEDIA_TYPE,
) -> httpx.Response:
    return harness.client.post(
        ADMIN_BASE_PATH + "/documents",
        params={"relative_path": relative_path},
        headers={
            **harness.product.write_headers,
            "Content-Type": media_type,
            "Idempotency-Key": idempotency_key,
        },
        content=content,
    )


def _scope_counts(harness: PublicHarness) -> tuple[int, int]:
    binding = harness.scope_service.binding()
    documents = harness.product.runtime.sdk.list_documents(
        binding.project_id, binding.knowledge_base_id, limit=200
    )
    jobs = harness.product.runtime.sdk.list_jobs(
        project_id=binding.project_id,
        knowledge_base_id=binding.knowledge_base_id,
        page_size=200,
    )
    return len(documents), jobs.total


def test_single_docx_upload_persists_unicode_directory_metadata(
    public_harness: PublicHarness,
) -> None:
    relative_path = "01 科管/02 科研项目管理/某制度.docx"
    response = _upload(
        public_harness,
        _docx("仅用于 WB-05 定向测试。"),
        relative_path,
        "single-docx",
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["job"]["document_id"] == payload["document"]["document_id"]
    assert payload["document"]["relative_path"] == relative_path
    assert payload["document"]["department"] == "01 科管"
    assert payload["document"]["category_path"] == ["02 科研项目管理"]
    listed = public_harness.client.get(ADMIN_BASE_PATH + "/documents")
    assert listed.json()["items"][0]["relative_path"] == relative_path


def test_multiple_upload_requests_are_independent(
    public_harness: PublicHarness,
) -> None:
    first = _upload(
        public_harness, _docx("第一份"), "批次/第一份.docx", "multi-1"
    )
    failed = _upload(
        public_harness, b"PK\x03\x04not-ooxml", "批次/损坏.docx", "multi-bad"
    )
    second = _upload(
        public_harness, _docx("第二份"), "批次/第二份.docx", "multi-2"
    )

    assert first.status_code == 202
    assert failed.status_code == 422
    assert failed.json()["error"]["code"] == "INVALID_DOCUMENT"
    assert second.status_code == 202
    assert first.json()["job"]["job_id"] != second.json()["job"]["job_id"]
    assert _scope_counts(public_harness) == (2, 2)


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("文件.pdf", "application/pdf"),
        ("文件.doc", "application/msword"),
        ("文件.xls", "application/vnd.ms-excel"),
        (
            "文件.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("文件.zip", "application/zip"),
        ("文件.html", "text/html"),
        ("文件.txt", "text/plain"),
    ],
)
def test_non_docx_is_blocked_before_job_creation(
    public_harness: PublicHarness, name: str, media_type: str
) -> None:
    before = _scope_counts(public_harness)
    response = _upload(
        public_harness,
        b"synthetic",
        name,
        "blocked-" + Path(name).suffix.removeprefix("."),
        media_type=media_type,
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "DOCX_ONLY"
    assert response.json()["error"]["message"] == DOCX_ONLY_MESSAGE
    assert _scope_counts(public_harness) == before


@pytest.mark.parametrize(
    "relative_path",
    [
        "../越界.docx",
        "/absolute.docx",
        "C:\\Users\\文件.docx",
        "C:/Users/文件.docx",
        "目录//文件.docx",
        "目录/./文件.docx",
        "目录/\x01文件.docx",
    ],
)
def test_unsafe_relative_path_is_rejected_without_job(
    public_harness: PublicHarness, relative_path: str
) -> None:
    before = _scope_counts(public_harness)
    response = _upload(
        public_harness,
        _docx("不会进入队列"),
        relative_path,
        "unsafe-path-" + str(abs(hash(relative_path))),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RELATIVE_PATH"
    assert _scope_counts(public_harness) == before


def test_wrong_media_and_empty_docx_create_no_job(
    public_harness: PublicHarness,
) -> None:
    before = _scope_counts(public_harness)
    wrong_media = _upload(
        public_harness,
        _docx("媒体类型错误"),
        "文件.docx",
        "wrong-media",
        media_type="application/octet-stream",
    )
    empty = _upload(
        public_harness, b"", "空文件.docx", "empty-docx"
    )

    assert wrong_media.status_code == 415
    assert empty.status_code == 422
    assert _scope_counts(public_harness) == before


def test_new_version_and_delete_stay_in_fixed_scope(
    public_harness: PublicHarness,
) -> None:
    relative_path = "部门/分类/版本文档.docx"
    created = _upload(
        public_harness, _docx("版本一"), relative_path, "version-create"
    )
    document_id = created.json()["document"]["document_id"]
    version = public_harness.client.post(
        ADMIN_BASE_PATH + f"/documents/{document_id}/versions",
        params={"relative_path": relative_path},
        headers={
            **public_harness.product.write_headers,
            "Content-Type": DOCX_MEDIA_TYPE,
            "Idempotency-Key": "version-two",
        },
        content=_docx("版本二"),
    )
    deleted = public_harness.client.delete(
        ADMIN_BASE_PATH + f"/documents/{document_id}",
        headers=public_harness.product.write_headers,
    )

    assert created.status_code == 202
    assert version.status_code == 202
    assert version.json()["document"]["relative_path"] == relative_path
    assert deleted.status_code == 204
