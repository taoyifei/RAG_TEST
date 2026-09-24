"""候选 Markdown 入库与格式开关的产品入口验证。"""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

import pytest

from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from tests.wanshitong.support import build_public_harness


def test_markdown_candidate_upload_reaches_index_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_WK_DOCUMENT_FORMATS", "md")
    harness = build_public_harness(tmp_path, monkeypatch)
    try:
        overview = harness.client.get(ADMIN_BASE_PATH + "/overview")
        assert overview.status_code == 200
        assert overview.json()["supported_formats"]["md"] == "enabled"
        assert overview.json()["supported_formats"]["xlsx"] == "disabled"

        response = harness.client.post(
            ADMIN_BASE_PATH + "/documents",
            params={"relative_path": "合成资料/说明.md"},
            headers={
                **harness.product.write_headers,
                "Content-Type": "text/markdown",
                "Idempotency-Key": "candidate-md-upload",
            },
            content="# 合成标题\n\n说明文本可检索。".encode(),
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job"]["job_id"]
        deadline = monotonic() + 10
        while monotonic() < deadline:
            job = harness.product.runtime.sdk.get_job(job_id)
            if job.state.value not in {"queued", "running"}:
                break
            sleep(0.01)
        else:
            raise AssertionError(f"Markdown Job 未在期限内结束：{job_id}")
        assert job.state.value == "succeeded", job.model_dump()
        document_id = response.json()["document"]["document_id"]
        with harness.product.runtime.connections.transaction() as connection:
            rows = connection.execute(
                "SELECT source_spans_json FROM chunks WHERE document_id=?",
                (document_id,),
            ).fetchall()
        assert rows
        spans = [row["source_spans_json"] for row in rows]
        assert any("parsed_artifact_text" in item for item in spans)
    finally:
        harness.close()
