"""活动语料清单批准、失效和查询数据面合同回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest

from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.core.models import Job
from tests.adapters.parsers.docx.fixtures import build_package
from tests.api.test_query_history import _upload
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _wait(harness: ProductHarness, job: Job) -> Job:
    """等待公开合成文档版本完成并成为活动 Revision。"""
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            assert current.state.value == "succeeded", current
            return current
        sleep(0.01)
    raise AssertionError("公开合成文档版本处理超时。")


def _grounded_response(request: httpx.Request) -> httpx.Response:
    """用请求自身的第一条 Evidence 构造可通过校验的公开 Mock。"""
    payload = json.loads(request.content)
    data = json.loads(payload["messages"][1]["content"])
    evidence = data["evidence"][0]
    return httpx.Response(
        200,
        json={
            "model": payload["model"],
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "claims": [
                                    {
                                        "text": evidence["text"],
                                        "supports": [
                                            {
                                                "support_id": evidence[
                                                    "support_id"
                                                ],
                                                "quote": evidence["text"],
                                            }
                                        ],
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 10,
                "total_tokens": 50,
            },
        },
    )


def test_manifest_approval_enables_generation_and_revision_change_blocks_it(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """批准前零出网，批准后可调用，新 Revision 后再次零出网。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _grounded_response(request)

    harness = build_product_harness(
        tmp_path,
        transport_factory=lambda _: httpx.MockTransport(respond),
    )
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        document_id = _upload(harness, project_id, knowledge_base_id)
        _, _, _, aliyun_connection_id = create_provider_connections(harness)
        settings_path = (
            f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings"
        )
        saved = harness.client.put(
            settings_path,
            headers=harness.write_headers,
            json={
                "generation_connection_id": aliyun_connection_id,
                "generation_model": "qwen3.7-flash",
            },
        )
        assert saved.status_code == 200, saved.text
        assert (
            saved.json()["corpus_authorization"]["corpus_authorization_state"]
            == "MISSING"
        )
        assert (
            saved.json()["retrieval_data_plane"]["retrieval_data_plane"]
            == "default_local_fallback"
        )

        answer_path = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer"
        )
        before = harness.client.post(
            answer_path,
            headers=harness.write_headers,
            json={"query": "MX-41"},
        )
        assert before.status_code == 200, before.text
        before_payload = before.json()
        assert before_payload["answer"]
        assert before_payload["generation_called_this_request"] is False
        assert (
            before_payload["data_plane"]["retrieval_data_plane"]
            == "default_local_fallback"
        )
        assert (
            before_payload["data_plane"]["model_authorization_state"]
            == "MISSING"
        )
        assert not requests

        authorization_path = (
            f"/api/v1/knowledge-bases/{knowledge_base_id}/"
            "corpus-authorization:approve"
        )
        approval = {
            "operations": ["generation"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "request_limit": 4,
            "estimated_token_limit": 20_000,
            "operation_request_limits": {"generation": 4},
        }
        missing_csrf = harness.client.post(authorization_path, json=approval)
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["error"]["code"] == "CSRF_REQUIRED"

        approved = harness.client.post(
            authorization_path,
            headers=harness.write_headers,
            json=approval,
        )
        assert approved.status_code == 200, approved.text
        approved_payload = approved.json()
        assert approved_payload["corpus_authorization_state"] == "APPROVED"
        assert approved_payload["model_authorization_state"] == "APPROVED"
        assert approved_payload["budget_state"] == "AVAILABLE"
        assert approved_payload["manifest"]["active_document_count"] == 1
        assert approved_payload["manifest"]["operations"] == ["generation"]

        document = harness.runtime.sdk.get_document(
            project_id, knowledge_base_id, document_id
        )
        assert document.current_version_id is not None
        version = harness.runtime.sdk.get_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            document.current_version_id,
        )
        assert version.content_sha256 not in approved.text
        campaign = ProviderBudgetLedger(
            harness.runtime.data_dir / "provider-budget.sqlite3",
            read_only=True,
        ).campaign(approved_payload["manifest"]["budget_campaign_id"])
        assert campaign.approved_source_hashes == (version.content_sha256,)
        with harness.runtime.connections.transaction() as connection:
            stored_session = connection.execute(
                "SELECT approved_by_session_id FROM "
                "corpus_authorization_manifests"
            ).fetchone()[0]
            active_session = connection.execute(
                "SELECT session_id FROM console_sessions "
                "WHERE revoked_at IS NULL"
            ).fetchone()[0]
        assert stored_session == active_session

        generated = harness.client.post(
            answer_path,
            headers=harness.write_headers,
            json={"query": "设备 MX-41 的维护周期是多少？"},
        )
        assert generated.status_code == 200, generated.text
        generated_payload = generated.json()
        assert generated_payload["generation_mode"] == "llm"
        assert generated_payload["generation_called_this_request"] is True
        assert (
            generated_payload["data_plane"]["corpus_authorization_state"]
            == "APPROVED"
        )
        assert len(requests) == 1

        next_version = harness.runtime.sdk.create_document_version(
            project_id,
            knowledge_base_id,
            document_id,
            content=build_package(
                "<w:p><w:r><w:t>设备 MX-41 的维护周期调整为 21 天。</w:t>"
                "</w:r></w:p>"
            ),
            media_type=_MEDIA_TYPE,
            idempotency_key="corpus-authorization-stale",
        )
        _wait(harness, next_version)
        stale = harness.client.get(authorization_path.removesuffix(":approve"))
        assert stale.status_code == 200, stale.text
        assert stale.json()["corpus_authorization_state"] == "STALE_REVISION"

        after = harness.client.post(
            answer_path,
            headers=harness.write_headers,
            json={"query": "MX-41 调整后的维护周期是多少？"},
        )
        assert after.status_code == 200, after.text
        after_payload = after.json()
        assert after_payload["answer"]
        assert after_payload["generation_called_this_request"] is False
        assert (
            after_payload["data_plane"]["corpus_authorization_state"]
            == "STALE_REVISION"
        )
        assert (
            "CORPUS_AUTHORIZATION_STALE_REVISION"
            in after_payload["data_plane"]["fallback_reason_codes"]
        )
        assert len(requests) == 1
    finally:
        harness.close()


def test_all_corpus_operations_keep_a_stable_binding_identity(
    tmp_path: Path,
) -> None:
    """批准顺序与配置推导顺序不同时仍应立即可用。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        _, _, _, aliyun_connection_id = create_provider_connections(harness)
        saved = harness.client.put(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings",
            headers=harness.write_headers,
            json={
                "generation_connection_id": aliyun_connection_id,
                "generation_model": "qwen3.7-flash",
                "rewrite_enabled": True,
                "ocr_connection_id": aliyun_connection_id,
                "ocr_model": "qwen3.5-ocr",
                "ocr_enabled": True,
                "ocr_media_hashes": ["a" * 64],
            },
        )
        assert saved.status_code == 200, saved.text
        required = saved.json()["corpus_authorization"]["required_operations"]

        approved = harness.client.post(
            f"/api/v1/knowledge-bases/{knowledge_base_id}/"
            "corpus-authorization:approve",
            headers=harness.write_headers,
            json={
                "operations": required,
                "expires_at": (
                    datetime.now(UTC) + timedelta(hours=1)
                ).isoformat(),
                "request_limit": 4,
                "estimated_token_limit": 20_000,
                "operation_request_limits": dict.fromkeys(required, 1),
            },
        )

        assert approved.status_code == 200, approved.text
        status = approved.json()
        assert status["corpus_authorization_state"] == "APPROVED"
        assert status["model_authorization_state"] == "APPROVED"
        assert status["budget_state"] == "AVAILABLE"
        assert status["fallback_reason_codes"] == []
    finally:
        harness.close()


def test_local_data_plane_is_persisted_in_history_and_safe_trace(
    tmp_path: Path,
) -> None:
    """响应、History 和 SAFE Trace 共享同一份非敏感数据面真相。"""
    harness = build_product_harness(tmp_path)
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload(harness, project_id, knowledge_base_id)
        response = harness.client.post(
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer",
            headers=harness.write_headers,
            json={"query": "MX-41"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        data_plane = payload["data_plane"]
        assert data_plane["retrieval_data_plane"] == "default_local_fallback"
        assert data_plane["embedding_provider_id"] == "deterministic"
        assert "DETERMINISTIC_EMBEDDING" in data_plane["fallback_reason_codes"]

        history = harness.runtime.history.detail(payload["trace_id"])
        assert history["data_plane"] == data_plane
        trace = harness.runtime.traces.detail(payload["trace_id"])
        spans = [
            item for item in trace.spans if item.name == "query.data_plane"
        ]
        assert len(spans) == 1
        assert dict(spans[0].attributes)["data_plane"] == data_plane
        assert trace.candidate_decisions
        assert all(
            decision.rank is None
            and decision.score_type is None
            and decision.score is None
            and decision.contribution is None
            and decision.details == {}
            for decision in trace.candidate_decisions
        )
        span_names = {item.name for item in trace.spans}
        assert {"query.semantics", "query.final_status"} <= span_names
        semantics = next(
            item for item in trace.spans if item.name == "query.semantics"
        )
        assert (
            dict(semantics.attributes)["requested_answer_type"]
            == payload["requested_answer_type"]
        )
        assert (
            dict(semantics.attributes)["query_semantic_source"]
            == payload["query_semantic_source"]
        )
        final = next(
            item for item in trace.spans if item.name == "query.final_status"
        )
        assert dict(final.attributes)["answer_published"] is True
    finally:
        harness.close()
