"""PDF Product API 从上传到版本化物理页证据的定向回归。"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)

_PDF_MEDIA_TYPE = "application/pdf"


def _pdf_bytes(page_count: int) -> bytes:
    """生成不含私有资料的多页 PDF。"""
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=200)
    writer.write(output)
    return output.getvalue()


def _raw_page(text: str, *, page_number: int) -> dict[str, object]:
    """返回官方文档解析结果中的单页公开合同形状。"""
    blocks: list[dict[str, object]] = []
    if text:
        blocks.append(
            {
                "block_id": f"page-{page_number}-answer",
                "block_label": "text",
                "block_order": 0,
                "block_content": text,
                "block_bbox": [5, 10, 95, 40],
            }
        )
    return {
        "prunedResult": {
            "width": 100,
            "height": 200,
            "parsing_res_list": blocks,
        }
    }


class _OfficialPdfClient:
    """按输入物理页数返回连续 PaddleOCR-VL 页面。"""

    def __init__(self, observed_page_counts: list[int]) -> None:
        self._observed_page_counts = observed_page_counts

    def parse_document(self, **kwargs: object) -> object:
        """为连接验证和 Product 上传返回完整页集合。"""
        path = Path(str(kwargs["file_path"]))
        page_count = len(PdfReader(path, strict=True).pages)
        self._observed_page_counts.append(page_count)
        return SimpleNamespace(
            pages=[
                _raw_page(
                    "设备维护周期为 45 days。"
                    if index == page_count - 1
                    else "",
                    page_number=index + 1,
                )
                for index in range(page_count)
            ],
            job_id=f"public-job-{page_count}",
        )

    def close(self) -> None:
        """合同替身没有外部资源。"""


def _validate_jina_profile(harness: ProductHarness, connection_id: str) -> None:
    """完成 PDF 入库复用的 Jina 三项合同验证。"""
    for operation, model, dimension, policy in (
        (
            "embedding.document",
            "jina-embeddings-v5-text-small",
            1024,
            {"task": "retrieval.passage", "normalized": True},
        ),
        (
            "embedding.query",
            "jina-embeddings-v5-text-small",
            1024,
            {"task": "retrieval.query", "normalized": True},
        ),
        ("reranking", "jina-reranker-v3.5", None, {}),
    ):
        response = harness.client.post(
            f"/api/v1/provider-connections/{connection_id}:validate",
            headers=harness.write_headers,
            json={
                "operation": operation,
                "model": model,
                "expected_dimension": dimension,
                "request_policy": policy,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "succeeded"


def _wait_job(
    harness: ProductHarness, job_id: str, *, timeout: float = 15.0
) -> dict[str, object]:
    """等待 PDF Job 到达持久终态。"""
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        response = harness.client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] not in {"queued", "running"}:
            return job
        sleep(0.01)
    raise AssertionError(f"PDF Product Job 未在期限内结束：{job_id}")


def test_pdf_product_api_preserves_page_12_and_versioned_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """官方 PDF 上传复用同一检索链并引用不可变物理第十二页。"""
    harness = build_product_harness(tmp_path)
    observed_page_counts: list[int] = []
    monkeypatch.setattr(
        harness.runtime.pdf,
        "_official_client_factory",
        lambda _token, _request_timeout, _poll_timeout: _OfficialPdfClient(
            observed_page_counts
        ),
    )
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _, _, jina_connection_id, _ = create_provider_connections(harness)
        _validate_jina_profile(harness, jina_connection_id)
        profile = harness.client.post(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/retrieval-profiles",
            headers=harness.write_headers,
            json={
                "primary_connection_id": jina_connection_id,
                "primary_embedding_model": "jina-embeddings-v5-text-small",
                "primary_dimension": 1024,
                "primary_document_policy": {
                    "task": "retrieval.passage",
                    "normalized": True,
                },
                "primary_query_policy": {
                    "task": "retrieval.query",
                    "normalized": True,
                },
                "reranker_connection_id": jina_connection_id,
                "reranker_model": "jina-reranker-v3.5",
            },
        )
        assert profile.status_code == 201, profile.text
        activated = harness.client.post(
            "/api/v1/retrieval-profiles/"
            f"{profile.json()['profile_revision_id']}:activate",
            headers=harness.write_headers,
            json={"confirmed_impact": "NEW_INDEX_REVISION_REQUIRED"},
        )
        assert activated.status_code == 200, activated.text

        paddle = harness.client.post(
            "/api/v1/provider-connections",
            headers=harness.write_headers,
            json={
                "display_name": "PaddleOCR 官方合同",
                "provider_type": "paddleocr",
                "endpoint_mode": "official_api",
                "credential": {
                    "provider_type": "paddleocr",
                    "source": "database_encrypted",
                    "secret_value": hashlib.sha256(
                        b"public-contract-token"
                    ).hexdigest(),
                },
                "request_budget": 10,
                "token_budget": 1,
            },
        )
        assert paddle.status_code == 201, paddle.text
        paddle_id = paddle.json()["connection_id"]
        validated = harness.client.post(
            f"/api/v1/provider-connections/{paddle_id}:validate",
            headers=harness.write_headers,
            json={
                "operation": "document.parse",
                "model": "PaddleOCR-VL-1.6",
            },
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["status"] == "succeeded"
        assert validated.json()["validation_mode"] == "mock"
        settings = harness.client.put(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings",
            headers=harness.write_headers,
            json={
                "pdf_parser_connection_id": paddle_id,
                "pdf_parser_model": "PaddleOCR-VL-1.6",
                "pdf_parser_enabled": True,
                "pdf_request_timeout_seconds": 300.0,
                "pdf_poll_timeout_seconds": 600.0,
            },
        )
        assert settings.status_code == 200, settings.text

        content = _pdf_bytes(12)
        uploaded = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/documents",
            params={"display_name": "page12_only.pdf"},
            content=content,
            headers={
                **harness.write_headers,
                "Idempotency-Key": "pdf-page-12",
                "Content-Type": _PDF_MEDIA_TYPE,
            },
        )
        assert uploaded.status_code == 202, uploaded.text
        job = _wait_job(harness, str(uploaded.json()["job_id"]))
        assert job["state"] == "succeeded", job
        assert job["pdf_progress"] == {
            "parser_mode": "paddle_official_api",
            "parser_model": "PaddleOCR-VL-1.6",
            "total_pages": 12,
            "parsed_pages": 12,
            "failed_page_indices": [],
            "truncated": False,
        }
        assert observed_page_counts == [1, 12]

        search = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:search",
            headers=harness.write_headers,
            json={"query": "45 days", "limit": 5},
        )
        assert search.status_code == 200, search.text
        evidence = next(
            item
            for item in search.json()["evidence"]
            if "45 days" in item["citation_text"]
        )
        assert evidence["page_index"] == 11
        assert evidence["source_label"].endswith("PDF第12页 · PDF解析文字")
        assert evidence["document_version_id"] == job["document_version_id"]

        base = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/documents/{job['document_id']}"
            f"/versions/{job['document_version_id']}"
        )
        artifacts = harness.client.get(base + "/artifacts")
        assert artifacts.status_code == 200, artifacts.text
        source = next(
            item
            for item in artifacts.json()["items"]
            if item["role"] == "source_document"
        )
        downloaded = harness.client.get(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/artifacts/{source['artifact_id']}",
            params={
                "document_id": job["document_id"],
                "document_version_id": job["document_version_id"],
            },
        )
        assert downloaded.status_code == 200
        assert downloaded.content == content
        wrong_version = harness.client.get(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}/artifacts/{source['artifact_id']}",
            params={
                "document_id": job["document_id"],
                "document_version_id": "dver_" + "0" * 32,
            },
        )
        assert wrong_version.status_code == 404
    finally:
        harness.close()
