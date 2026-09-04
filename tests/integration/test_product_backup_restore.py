"""真实双 Qdrant Server 的 Product 备份与新目录恢复验收。"""

from __future__ import annotations

import os
from pathlib import Path
from time import monotonic, sleep

import pytest
from qdrant_client import QdrantClient

from rag_app.composition.product_runtime import (
    ProductRuntimeSettings,
    build_product_runtime,
)
from rag_app.core.models import Job
from rag_app.product.backup import create_backup, restore_backup, verify_backup
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)

pytestmark = pytest.mark.local_integration

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"需要显式配置 {name}")
    return value


def _wait(harness: ProductHarness, job: Job) -> Job:
    deadline = monotonic() + 30
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            return current
        sleep(0.05)
    raise AssertionError("Product 构建作业未在期限内结束。")


def test_backup_restores_all_product_state_and_qdrant_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url = _required_environment("RAG_TEST_QDRANT_SOURCE_URL")
    target_url = _required_environment("RAG_TEST_QDRANT_TARGET_URL")
    source_key = Path(_required_environment("RAG_TEST_QDRANT_SOURCE_KEY_FILE"))
    target_key = Path(_required_environment("RAG_TEST_QDRANT_TARGET_KEY_FILE"))
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    source = build_product_harness(
        tmp_path / "source",
        qdrant_url=source_url,
        qdrant_api_key_file=source_key,
    )
    revision_id = ""
    project_id = ""
    knowledge_base_id = ""
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            source
        )
        _, _, jina_connection, aliyun_connection = create_provider_connections(
            source
        )
        validate_five_operations(source, jina_connection, aliyun_connection)
        activate_hot_standby_profile(
            source,
            knowledge_base_id,
            jina_connection,
            aliyun_connection,
        )
        source.runtime.auth.create_access_token(
            name="恢复验收 Token",
            scopes=("query:read",),
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        content = build_package(
            "<w:p><w:r><w:t>公开合成备份恢复引用文本。</w:t></w:r></w:p>"
        )
        job = _wait(
            source,
            source.runtime.sdk.create_document(
                project_id,
                knowledge_base_id,
                display_name="公开合成备份文档.docx",
                content=content,
                media_type=_DOCX_MEDIA_TYPE,
                idempotency_key="backup-restore-source",
            ),
        )
        assert job.state.value == "succeeded"
        revision_id = job.revision_id
        archive = tmp_path / "release-backup.tar.gz"
        report = create_backup(
            data_dir=source.runtime.data_dir,
            output=archive,
            compatibility_manifest=(
                _REPOSITORY_ROOT / "compatibility-manifest.json"
            ),
            qdrant_url=source_url,
            qdrant_api_key_file=source_key,
        )
        assert report.collection_count == 1
        assert verify_backup(archive) == report
    finally:
        source.close()
        _delete_collection(source_url, source_key, revision_id)

    restored_dir = tmp_path / "restored-data"
    restored = None
    try:
        restore_backup(
            archive_path=archive,
            target_data_dir=restored_dir,
            qdrant_url=target_url,
            qdrant_api_key_file=target_key,
        )
        restored = build_product_runtime(
            ProductRuntimeSettings(
                data_dir=restored_dir,
                frontend_dir=tmp_path / "source" / "frontend",
                bootstrap_token_file=(
                    tmp_path / "source" / "bootstrap-token"
                ),
                master_key_file=tmp_path / "source" / "master-key",
                qdrant_mode="url",
                qdrant_url=target_url,
                qdrant_api_key_file=target_key,
                compatibility_manifest=(
                    _REPOSITORY_ROOT / "compatibility-manifest.json"
                ),
            ),
            transport_factory=build_offline_mock_transport,
        )
        result = restored.sdk.search(
            project_id,
            knowledge_base_id,
            "备份恢复引用",
        )

        assert len(restored.sdk.list_projects()) == 1
        assert len(restored.control.list_connections()) == 2
        assert len(restored.auth.list_access_tokens()) == 1
        assert result.evidence
        assert result.selected_embedding_slot == "primary"
        assert result.active_index_revision_id == revision_id
    finally:
        if restored is not None:
            restored.close()
        _delete_collection(target_url, target_key, revision_id)


def _delete_collection(url: str, key_file: Path, collection: str) -> None:
    if not collection:
        return
    client = QdrantClient(
        url=url,
        api_key=key_file.read_text(encoding="utf-8").strip(),
        check_compatibility=False,
    )
    try:
        if client.collection_exists(collection):
            client.delete_collection(collection)
    finally:
        client.close()
