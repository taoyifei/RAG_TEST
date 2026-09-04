"""P10.5 Product Runtime 唯一组合根回归。"""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

from rag_app.core.models import Job
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
)

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _wait_for_job(harness: ProductHarness, job: Job) -> Job:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            return current
        sleep(0.01)
    raise AssertionError(f"Job 未在期限内结束：{job.job_id}")


def test_product_runtime_migrates_and_keeps_offline_base_mode(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path, master_key=False)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        status = harness.runtime.sdk.health()
        with harness.runtime.connections.transaction() as connection:
            migration_count = int(
                connection.execute(
                    "SELECT count(*) FROM schema_migrations"
                ).fetchone()[0]
            )

        assert project_id.startswith("prj_")
        assert knowledge_base_id.startswith("kb_")
        assert status.runtime_identity == "product-runtime-p10.5"
        assert status.primary_live_evaluation_status == "not_verified"
        assert status.remote_production_profile_ready is False
        assert migration_count == 14
    finally:
        harness.close()


def test_product_runtime_without_provider_keeps_fts_exact_flow(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path, master_key=False)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        content = build_package(
            "<w:p><w:r><w:t>青岛啤酒采购流程使用公开合成文本。</w:t></w:r></w:p>"
        )
        job = _wait_for_job(
            harness,
            harness.runtime.sdk.create_document(
                project_id,
                knowledge_base_id,
                display_name="公开合成文档.docx",
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="offline-base-mode",
            ),
        )
        result = harness.runtime.sdk.search(
            project_id,
            knowledge_base_id,
            "青岛啤酒",
        )

        assert job.state.value == "succeeded"
        assert result.evidence
        assert result.diagnostics is not None
        channels = dict(result.diagnostics.channel_chunk_ids)
        assert channels["lexical"]
    finally:
        harness.close()
