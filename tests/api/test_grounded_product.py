"""完整产品资料生成链的离线 HTTP、真实预算边界和重启历史回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from rag_app.composition.product_runtime import build_product_runtime
from tests.api.test_query_history import _upload
from tests.product_support import (
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)


def test_configured_generation_history_cache_failure_and_scope(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    requests: list[httpx.Request] = []
    failing = False

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failing:
            return httpx.Response(503, json={"error": {"code": "unavailable"}})
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
                    "prompt_tokens": 100,
                    "completion_tokens": 30,
                    "total_tokens": 130,
                },
            },
        )

    harness = build_product_harness(
        tmp_path, transport_factory=lambda _: httpx.MockTransport(respond)
    )
    project, kb = create_project_and_knowledge_base(harness)
    _upload(harness, project, kb)
    _, _, _, connection_id = create_provider_connections(harness)
    settings_path = f"/api/v1/knowledge-bases/{kb}/model-settings"
    response = harness.client.put(
        settings_path,
        headers=harness.write_headers,
        json={
            "generation_connection_id": connection_id,
            "generation_model": "qwen3.7-flash",
        },
    )
    assert response.status_code == 200, response.text
    approval = harness.client.post(
        f"/api/v1/knowledge-bases/{kb}/corpus-authorization:approve",
        headers=harness.write_headers,
        json={
            "operations": ["generation"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "request_limit": 8,
            "estimated_token_limit": 80_000,
            "operation_request_limits": {"generation": 8},
        },
    )
    assert approval.status_code == 200, approval.text
    assert not requests
    endpoint = f"/api/v1/projects/{project}/knowledge-bases/{kb}:answer"

    def query(text: str) -> dict:
        response = harness.client.post(
            endpoint, headers=harness.write_headers, json={"query": text}
        )
        assert response.status_code == 200, response.text
        return response.json()

    first = query("MX-41")
    assert first["generation_mode"] == "llm", first
    assert first["cache_hit"] is False
    assert first["result_origin"] == "fresh"
    assert first["generation_called_this_request"] is True
    assert first["rewrite_called_this_request"] is False
    assert len(requests) == 1
    first_payload = json.loads(requests[0].content)
    assert first_payload["model"] == "qwen3.7-flash"
    assert [message["role"] for message in first_payload["messages"]] == [
        "system",
        "user",
    ]
    grounded_input = json.loads(first_payload["messages"][1]["content"])
    assert grounded_input["question"] == "MX-41"
    assert [item["support_id"] for item in grounded_input["evidence"]] == [
        item["evidence_id"] for item in first["evidence"]
    ]
    assert [item["text"] for item in grounded_input["evidence"]] == [
        item["citation_text"] for item in first["evidence"]
    ]
    cached = query("MX-41")
    assert cached["cache_hit"] is True
    assert cached["result_origin"] == "cache"
    assert cached["generation_mode"] == "llm"
    assert cached["generation_called_this_request"] is False
    assert cached["rewrite_called_this_request"] is False
    assert cached["cache_key"] == first["cache_key"]
    assert cached["answer"] == first["answer"]
    assert len(requests) == 1
    refused = query("MX-41 负责人的手机号是什么")
    assert refused["answer"] is None, refused
    assert refused["generation_mode"] == "none"
    assert refused["generation_called_this_request"] is False
    failing = True
    failure = query("设备 MX-41 的维护周期是多少？")
    assert failure["generation_mode"] == "extractive_fallback", failure
    assert failure["generation_called_this_request"] is True
    assert len(requests) == 2
    page = harness.runtime.history.list_history()
    assert {item["status"] for item in page["items"]} == {
        "ANSWERED",
        "REFUSED",
    }
    failed_detail = harness.runtime.history.detail(failure["trace_id"])
    assert failed_detail["status"] == "ANSWERED"
    assert failed_detail["fallback_answer_available"] is True
    assert failed_detail["generation_reason_code"] == "PROVIDER_UNAVAILABLE"
    assert (
        failed_detail["requested_answer_type"]
        == failure["requested_answer_type"]
    )
    assert (
        failed_detail["query_semantic_source"]
        == failure["query_semantic_source"]
    )
    assert any(
        item["call_count"] == 1 and item["operation"] == "generation"
        for item in failed_detail["provider_usage"]
    )
    assert any(
        item["usage"] == 130
        for item in harness.runtime.history.detail(first["trace_id"])[
            "provider_usage"
        ]
    )
    saved_settings = harness.runtime.settings
    harness.close()
    with build_product_runtime(
        saved_settings, transport_factory=lambda _: httpx.MockTransport(respond)
    ) as runtime:
        page = runtime.history.list_history()
        assert page["total"] == 4
        assert runtime.history.detail(first["trace_id"])["answer"]
        assert runtime.history.detail(cached["trace_id"])["cache_hit"] is True
