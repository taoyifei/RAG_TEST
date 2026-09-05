"""P11-R2 配置必须驱动真实请求和运行策略。"""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest

from rag_app.core.models import EmbeddingRequest, EmbeddingRequestRole
from rag_app.product.models import (
    ImpactKind,
    ProviderConnection,
    RetrievalProfileDraft,
)
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


def _draft(kb: str, jina: str, aliyun: str) -> RetrievalProfileDraft:
    return RetrievalProfileDraft(
        knowledge_base_id=kb,
        primary_connection_id=jina,
        primary_embedding_model="jina-embeddings-v5-text-small",
        primary_dimension=1024,
        primary_document_policy={"task": "retrieval.passage"},
        primary_query_policy={"task": "retrieval.query"},
        standby_connection_id=aliyun,
        standby_embedding_model="qwen3.7-text-embedding",
        standby_dimension=1024,
        standby_document_policy={"text_type": "document"},
        standby_query_policy={"text_type": "query"},
        failover_enabled=True,
    )


def test_qwen_profile_instruction_reaches_actual_http_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    bodies: list[dict[str, object]] = []

    def _transport(connection: ProviderConnection) -> httpx.MockTransport:
        successful = build_offline_mock_transport(connection)

        def _handler(request: httpx.Request) -> httpx.Response:
            if connection.provider_type == "aliyun-model-studio":
                bodies.append(json.loads(request.content))
            return successful.handle_request(request)

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        for instruction in ("检索设备维护资料", "检索采购合同资料"):
            draft = _draft(kb, jina, aliyun).model_copy(
                update={
                    "standby_query_policy": {
                        "text_type": "query",
                        "query_instruct": instruction,
                    },
                }
            )
            response = harness.client.post(
                f"/api/v1/knowledge-bases/{kb}/retrieval-profiles",
                headers=harness.write_headers,
                json=draft.model_dump(
                    mode="json", exclude={"knowledge_base_id"}
                ),
            )
            assert response.status_code == 201
            assert (
                response.json()["standby_query_policy"]["query_instruct"]
                == instruction
            )
            profile = harness.runtime.control.get_profile(
                response.json()["profile_revision_id"]
            )
            services = harness.runtime.profiles._resolve(profile)
            adapter = services.remote_resources[1]
            for role in (
                EmbeddingRequestRole.QUERY,
                EmbeddingRequestRole.DOCUMENT,
            ):
                adapter.embed(
                    EmbeddingRequest(
                        slot_id="standby",
                        role=role,
                        texts=("公开合成文本",),
                    )
                )
        assert [body["parameters"].get("instruct") for body in bodies] == [
            "检索设备维护资料",
            None,
            "检索采购合同资料",
            None,
        ]
    finally:
        harness.close()


def test_evidence_compatibility_input_drives_resolved_policy(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        profile = harness.runtime.control.create_profile(
            _draft(kb, jina, aliyun).model_copy(
                update={
                    "evidence_policy": {
                        "minimum_units": 2,
                        "max_evidence_items": 3,
                        "evidence_token_budget": 64,
                    }
                }
            ),
        )
        policy = harness.runtime.profiles._resolve(profile).retrieval._policy
        assert policy.minimum_support_items == 2
        assert policy.max_evidence_items == 3
        assert policy.evidence_token_budget == 64
        assert dict(profile.retrieval_policy)["minimum_support_items"] == 2
    finally:
        harness.close()


def test_failover_changes_serving_but_preserves_index(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        draft = _draft(kb, jina, aliyun)
        enabled = harness.runtime.control.create_profile(draft)
        disabled = harness.runtime.control.create_profile(
            draft.model_copy(update={"failover_enabled": False}),
        )
        assert (
            enabled.index_semantic_fingerprint
            == disabled.index_semantic_fingerprint
        )
        assert enabled.serving_fingerprint != disabled.serving_fingerprint
    finally:
        harness.close()


def test_schema_name_is_not_offline_evaluation_evidence(tmp_path: Path) -> None:
    harness = build_product_harness(tmp_path)
    try:
        assert harness.runtime.sdk.health().offline_evaluation_v3_ready is False
    finally:
        harness.close()


def test_semantic_apply_keeps_old_profile_and_index_until_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        project, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        validate_five_operations(harness, jina, aliyun)
        old_profile = activate_hot_standby_profile(harness, kb, jina, aliyun)
        job = harness.runtime.sdk.create_document(
            project,
            kb,
            display_name="合成.docx",
            content=build_package(
                "<w:p><w:r><w:t>采购合同执行要求。</w:t></w:r></w:p>"
            ),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            idempotency_key="r2-initial",
        )
        deadline = monotonic() + 10
        while (
            job.state.value in {"queued", "running"} and monotonic() < deadline
        ):
            sleep(0.01)
            job = harness.runtime.sdk.get_job(job.job_id)
        assert job.state.value == "succeeded"
        proposed = harness.runtime.control.create_profile(
            _draft(kb, jina, aliyun).model_copy(
                update={
                    "standby_query_policy": {
                        "text_type": "query",
                        "query_instruct": "新的合同检索指令",
                    },
                }
            ),
        )
        assert harness.runtime.control.profile_validation_issues(
            proposed.profile_revision_id
        ) == (f"{aliyun}:embedding.query",)
        harness.runtime.providers.validate(
            aliyun,
            operation="embedding.query",
            model="qwen3.7-text-embedding",
            expected_dimension=1024,
            request_policy=dict(proposed.standby_query_policy),
        )
        harness.runtime.control.activate_profile(
            proposed.profile_revision_id,
            confirmed_impact=ImpactKind.NEW_INDEX_REVISION_REQUIRED,
        )
        assert (
            harness.runtime.control.active_profile(kb).profile_revision_id
            == old_profile
        )
        assert (
            harness.runtime.retrieval_runtime.persistence.control.active_revision_id(
                kb
            )
            == job.revision_id
        )
        assert harness.runtime.sdk.search(project, kb, "采购合同").evidence
        pending = harness.runtime.control.get_profile(
            proposed.profile_revision_id
        )
        assert pending.activation_job_id is not None
        harness.runtime.profiles.job_lifecycle(
            pending.activation_job_id, harness.runtime.p09.lifecycle
        ).run_ingestion(pending.activation_job_id)
        completed = harness.runtime.sdk.get_job(pending.activation_job_id)
        assert completed.state.value == "succeeded"
        assert (
            harness.runtime.control.active_profile(kb).profile_revision_id
            == proposed.profile_revision_id
        )
        assert (
            harness.runtime.retrieval_runtime.persistence.control.active_revision_id(
                kb
            )
            == completed.revision_id
        )
    finally:
        harness.close()
