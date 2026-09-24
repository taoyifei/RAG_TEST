"""管理员预览与真实候选分块共享配置，且不创建索引。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from tests.product_support import build_product_harness


def test_candidate_preview_uses_go_without_creating_document_or_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    monkeypatch.setenv("RAG_WK_DOCUMENT_FORMATS", "md")
    harness = build_product_harness(
        tmp_path, weknora_chunker_mode="parent-child"
    )
    endpoint = ADMIN_BASE_PATH + "/candidate/chunk-preview"
    source = (
        "# 第一章\r\n\r\n"
        + "说明含 🧭 字符，并保留这一段的完整来源。" * 40
        + "\r\n\r\n## 第二节\r\n后续内容。"
    )
    try:
        binding = harness.client.app.state.wanshitong_scope_service.binding()
        with TestClient(harness.client.app) as anonymous:
            denied = anonymous.post(
                endpoint,
                params={"relative_path": "资料/合成.md"},
                headers={"Content-Type": "text/markdown"},
                content=source.encode(),
            )
        assert denied.status_code == 401
        csrf_denied = harness.client.post(
            endpoint,
            params={"relative_path": "资料/合成.md"},
            headers={"Content-Type": "text/markdown"},
            content=source.encode(),
        )
        assert csrf_denied.status_code == 403
        response = harness.client.post(
            endpoint,
            params={"relative_path": "资料/合成.md", "include_content": True},
            headers={**harness.write_headers, "Content-Type": "text/markdown"},
            content=source.encode(),
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["chunk_count"] > 0
        assert result["parent_count"] > 0
        assert result["reading_view_revision"] == "weknora-reading-domain-v2"
        assert result["domains"][0]["selected_tier"]
        assert result["domains"][0]["tier_chain"]
        assert "🧭" in "".join(
            item["citation_text"] for item in result["chunks"]
        )
        assert all(item["source_spans"] for item in result["chunks"])
        assert all(
            item["domain_id"] and 0 <= item["start_char"] < item["end_char"]
            for item in result["chunks"]
        )
        assert (
            harness.runtime.sdk.list_documents(
                binding.project_id, binding.knowledge_base_id, limit=200
            )
            == ()
        )
        assert (
            harness.runtime.sdk.list_jobs(
                project_id=binding.project_id,
                knowledge_base_id=binding.knowledge_base_id,
                page_size=200,
            ).total
            == 0
        )
        safe_response = harness.client.post(
            endpoint,
            params={"relative_path": "资料/合成.md"},
            headers={**harness.write_headers, "Content-Type": "text/markdown"},
            content=source.encode(),
        )
        assert safe_response.status_code == 200
        assert "citation_text" not in safe_response.json()["chunks"][0]
        uploaded = harness.client.post(
            ADMIN_BASE_PATH + "/documents",
            params={"relative_path": "资料/合成.md"},
            headers={
                **harness.write_headers,
                "Content-Type": "text/markdown",
                "Idempotency-Key": "preview-ingestion-differential",
            },
            content=source.encode(),
        )
        assert uploaded.status_code == 202, uploaded.text
        deadline = monotonic() + 10
        while monotonic() < deadline:
            job = harness.runtime.sdk.get_job(uploaded.json()["job"]["job_id"])
            if job.state.value not in {"queued", "running"}:
                break
            sleep(0.01)
        else:
            raise AssertionError("候选 Markdown 入库 Job 未按时结束。")
        assert job.state.value == "succeeded", job.model_dump()
        persistence = harness.runtime.p09.retrieval_runtime.persistence
        indexed = persistence.control.chunk_rows(job.revision_id)
        parents = persistence.control.parent_rows(job.revision_id)
        assert Counter(item.citation_text for item in indexed) == Counter(
            item["citation_text"] for item in result["chunks"]
        )
        assert len(parents) == result["parent_count"]
        assert all(
            dict(item.metadata)["reading_view_revision"]
            == result["reading_view_revision"]
            for item in indexed
        )
    finally:
        harness.close()


def test_candidate_preview_requires_candidate_chunker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    harness = build_product_harness(tmp_path)
    try:
        response = harness.client.post(
            ADMIN_BASE_PATH + "/candidate/chunk-preview",
            params={"relative_path": "资料/合成.docx"},
            headers={
                **harness.write_headers,
                "Content-Type": "application/octet-stream",
            },
            content=b"invalid",
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CANDIDATE_CHUNKER_DISABLED"
    finally:
        harness.close()
