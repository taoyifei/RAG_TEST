"""模板原件可回读，索引只包含目录提示。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from time import monotonic, sleep
from typing import Literal

import pytest

from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import (
    ChunkingContext,
    DocumentRef,
    ParseContext,
    ParseSource,
)
from rag_app.core.models.common import freeze_json_object
from rag_app.product.provider_runtime import build_offline_mock_transport
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.upload_validation import DOCX_MEDIA_TYPE
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import build_product_harness


@pytest.mark.parametrize("chunker_mode", ["legacy", "parent-child"])
def test_template_source_survives_upload_and_runtime_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chunker_mode: Literal["legacy", "parent-child"],
) -> None:
    monkeypatch.setenv("RAG_PRODUCT_MODE", "wanshitong")
    source = build_package(
        "<w:p><w:r><w:t>合成模板内部填写说明，不应进入索引。</w:t></w:r></w:p>"
    )
    expected_hash = hashlib.sha256(source).hexdigest()
    harness = build_product_harness(tmp_path, weknora_chunker_mode=chunker_mode)
    settings = harness.runtime.settings
    binding = harness.client.app.state.wanshitong_scope_service.binding()
    try:
        response = harness.client.post(
            ADMIN_BASE_PATH + "/documents",
            params={"relative_path": "合成资料/模板/申请表.docx"},
            headers={
                **harness.write_headers,
                "Content-Type": DOCX_MEDIA_TYPE,
                "Idempotency-Key": "template-original-source-v3",
            },
            content=source,
        )
        assert response.status_code == 202, response.text
        document_id = response.json()["document"]["document_id"]
        job_id = response.json()["job"]["job_id"]
        deadline = monotonic() + 10
        while monotonic() < deadline:
            job = harness.runtime.sdk.get_job(job_id)
            if job.state.value not in {"queued", "running"}:
                break
            sleep(0.01)
        else:
            raise AssertionError("模板入库 Job 未在期限内结束。")
        assert job.state.value == "succeeded", job.model_dump()
        versions = harness.runtime.sdk.list_document_versions(
            binding.project_id,
            binding.knowledge_base_id,
            document_id,
        )
        assert len(versions) == 1
        version = versions[0]
        assert version.content_sha256 == expected_hash
        artifacts = harness.runtime.sdk.list_artifacts(
            binding.project_id,
            binding.knowledge_base_id,
            document_id,
            version.document_version_id,
        )
        source_artifact = next(
            item for item in artifacts if item.role == "source_document"
        )
        downloaded = harness.runtime.sdk.read_artifact(
            binding.project_id,
            binding.knowledge_base_id,
            document_id,
            version.document_version_id,
            source_artifact.artifact_id,
        )
        assert downloaded.content == source
        downloaded_http = harness.client.get(
            f"/api/v1/projects/{binding.project_id}"
            f"/knowledge-bases/{binding.knowledge_base_id}"
            f"/artifacts/{source_artifact.artifact_id}",
            params={
                "document_id": document_id,
                "document_version_id": version.document_version_id,
            },
        )
        assert downloaded_http.status_code == 200
        assert downloaded_http.content == source
        persistence = harness.runtime.p09.retrieval_runtime.persistence
        rows = persistence.control.chunk_rows(job.revision_id)
        assert rows
        assert any("模板目录项" in row.citation_text for row in rows)
        assert all("内部填写说明" not in row.citation_text for row in rows)
    finally:
        harness.close()

    with build_product_runtime(
        settings, transport_factory=build_offline_mock_transport
    ) as reopened:
        persistence = reopened.p09.retrieval_runtime.persistence
        blob = persistence.components.blob_store.read(
            source_artifact.artifact_id
        )
        assert blob is not None and blob.content == source
        components = reopened.p09.retrieval_runtime.persistence.components
        document = DocumentRef(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
            document_id=document_id,
            display_name="申请表.docx",
            metadata=freeze_json_object(
                {
                    "source_relative_path": "合成资料/模板/申请表.docx",
                    "document_title": "申请表",
                }
            ),
        )
        parsed = components.parser.parse(
            ParseSource(
                media_type=DOCX_MEDIA_TYPE,
                display_name="申请表.docx",
                extension=".docx",
                content=blob.content,
            ),
            components.parsing_policy,
            ParseContext(document=document),
        )
        assert parsed.document_ir.version.document_version_id == (
            version.document_version_id
        )
        chunker = components.chunker
        rebuilt = chunker.chunk(
            parsed.document_ir,
            ChunkingContext(
                chunker_fingerprint=chunker.fingerprint,
                index_revision_id=deterministic_id(
                    "irev", "template-rebuild", document_id
                ),
            ),
        )
        assert rebuilt.chunks
        assert all(
            "内部填写说明" not in item.citation_text for item in rebuilt.chunks
        )
