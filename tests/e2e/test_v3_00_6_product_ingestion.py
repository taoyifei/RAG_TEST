"""V3-00.6 默认 Product DOCX 入库、查询、原件与重启闭环。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from time import monotonic, sleep

from fastapi.testclient import TestClient

from rag_app.api.product import create_product_app
from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.models import Chunk
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.fixtures.docx_v4.generate_fixtures import (
    _list_item,
    _numbering,
    build_package,
)
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _source() -> bytes:
    """生成含标题、列表、空单元格和独立空白章节的通用 DOCX。"""
    blocks = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>设备管理</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>设备 ZK-204 的巡检周期为九天。</w:t></w:r></w:p>"
        + _list_item("复核压力值 18 kPa。", 7, 0)
        + "<w:p><w:r><w:t>A) 手工编号保持为普通正文。</w:t></w:r></w:p>"
        + """
<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
  <w:tr><w:trPr><w:tblHeader/></w:trPr>
    <w:tc><w:p><w:r><w:t>设备</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>备注</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>校验码</w:t></w:r></w:p></w:tc>
  </w:tr>
  <w:tr>
    <w:tc><w:p><w:r><w:t>ZK-204</w:t></w:r></w:p></w:tc>
    <w:tc><w:p/></w:tc>
    <w:tc><w:p><w:r><w:t>VAL-77</w:t></w:r></w:p></w:tc>
  </w:tr>
</w:tbl>
"""
        + '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        "<w:r><w:t>预留章节</w:t></w:r></w:p>"
        + '<w:p><w:r><w:t xml:space="preserve">        </w:t>'
        "</w:r></w:p>"
    )
    return build_package(blocks, numbering=_numbering())


def _wait_job(harness: ProductHarness, job_id: str) -> dict[str, object]:
    deadline = monotonic() + 15
    while monotonic() < deadline:
        response = harness.client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = dict(response.json())
        if job["state"] not in {"queued", "running"}:
            assert job["state"] == "succeeded", job
            return job
        sleep(0.01)
    raise AssertionError("V3-00.6 Product 入库超时。")


def _assert_answer(
    client: TestClient, headers: dict[str, str], base: str
) -> None:
    response = client.post(
        base + ":answer",
        headers=headers,
        json={"query": "ZK-204 的巡检周期是多少？"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["confidence"]["status"] == "ANSWERABLE", payload
    assert "九天" in payload["answer"]
    assert payload["evidence"]


def test_default_product_ingestion_roundtrip_query_and_restart(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """默认 Product 完成上传到激活，并在冷启动后回读原件与回答。"""
    source = _source()
    harness = build_product_harness(tmp_path)
    project_id, knowledge_base_id = create_project_and_knowledge_base(harness)
    base = f"/api/v1/projects/{project_id}/knowledge-bases/{knowledge_base_id}"
    settings = harness.runtime.settings
    bootstrap_token = harness.bootstrap_token
    try:
        uploaded = harness.client.post(
            base + "/documents",
            params={"display_name": "合成设备工作规范.docx"},
            content=source,
            headers={
                **harness.write_headers,
                "Idempotency-Key": "v3-00-6-product",
                "Content-Type": _MEDIA_TYPE,
            },
        )
        assert uploaded.status_code == 202, uploaded.text
        job = _wait_job(harness, str(uploaded.json()["job_id"]))
        document_id = str(job["document_id"])
        document = harness.runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        assert document.current_version_id is not None
        assert document.active_index_revision_id is not None
        version = harness.runtime.sdk.get_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            document.current_version_id,
        )
        assert version.content_sha256 == hashlib.sha256(source).hexdigest()
        original = harness.client.get(
            base + f"/artifacts/{version.source_artifact_id}",
            params={
                "document_id": document_id,
                "document_version_id": version.document_version_id,
            },
        )
        assert original.status_code == 200, original.text
        assert original.headers["content-type"] == _MEDIA_TYPE
        assert original.content == source
        _assert_answer(harness.client, harness.write_headers, base)

        rows = harness.runtime.p09.control.parse_rows(
            document.active_index_revision_id
        )
        assert len(rows) == 1
        document_ir, parse_report, chunking_report = rows[0]
        assert parse_report == document_ir.parse_report
        assert chunking_report.source_span_coverage == 1.0
        assert chunking_report.missing_source_chars == 0
        assert chunking_report.whitespace_only_citable_node_count == 1
        assert chunking_report.whitespace_only_citable_char_count == 8
        assert chunking_report.list_label_count == 1
        assert chunking_report.represented_list_label_count == 1
        assert chunking_report.table_row_count == 2
        assert chunking_report.represented_table_row_count == 2
        chunks = harness.runtime.p09.control.chunk_rows(
            document.active_index_revision_id
        )
        assert chunks
        assert all(
            Chunk.model_validate_json(chunk.model_dump_json()) == chunk
            for chunk in chunks
        )
        active_revision_id = document.active_index_revision_id
    finally:
        harness.close()

    runtime = build_product_runtime(
        settings,
        transport_factory=build_offline_mock_transport,
    )
    client = TestClient(create_product_app(runtime))
    try:
        session = client.post(
            "/api/v1/console/session",
            json={"bootstrap_token": bootstrap_token},
        )
        assert session.status_code == 200, session.text
        headers = {"X-CSRF-Token": str(session.json()["csrf_token"])}
        reopened = runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        assert reopened.active_index_revision_id == active_revision_id
        _assert_answer(client, headers, base)
        original = client.get(
            base + f"/artifacts/{version.source_artifact_id}",
            params={
                "document_id": document_id,
                "document_version_id": version.document_version_id,
            },
        )
        assert original.status_code == 200, original.text
        assert original.content == source
    finally:
        client.close()
        runtime.close()
