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
        if "candidates" in data:
            assert data["original_query"] == "MX-41有哪些要求"
            assert len(data["candidates"]) == 1
            candidate = data["candidates"][0]
            assert candidate["claim"] == "设备 MX-41 的维护周期为 14 天。"
            assert candidate["refs"] == [data["read_units"][0]["unit_id"]]
            content = {
                "results": [
                    {
                        "claim_id": candidate["claim_id"],
                        "status": "supported",
                    }
                ],
            }
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
                                    content, ensure_ascii=False
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
        candidates = data["read_units"]
        atom_ids = [atom["atom_id"] for atom in data["atoms"]]
        claims = []
        if "手机号" not in json.dumps(data, ensure_ascii=False):
            evidence = candidates[0]
            claims = [
                {
                    "atom_id": atom_ids[0],
                    "text": evidence["text"],
                    "refs": [evidence["unit_id"]],
                }
            ]
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
                                {"claims": claims},
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

    # 章节要求由生成链处理；单一设备标识可直接返回已认证标量事实。
    first = query("MX-41有哪些要求")
    assert first["generation_mode"] == "llm", first
    assert first["cache_hit"] is False
    assert first["result_origin"] == "fresh"
    assert first["generation_called_this_request"] is True
    assert first["rewrite_called_this_request"] is False
    assert len(requests) == 2
    first_payload = json.loads(requests[0].content)
    assert first_payload["model"] == "qwen3.7-flash"
    assert [message["role"] for message in first_payload["messages"]] == [
        "system",
        "user",
    ]
    grounded_input = json.loads(first_payload["messages"][1]["content"])
    assert grounded_input["question"] == "MX-41有哪些要求"
    assert [item["text"] for item in grounded_input["read_units"]] == [
        item["citation_text"] for item in first["evidence"]
    ]
    review_input = json.loads(
        json.loads(requests[1].content)["messages"][1]["content"]
    )
    assert "candidates" in review_input
    assert "repair_only" not in review_input
    assert (
        review_input["candidates"][0]["claim"]
        == grounded_input["read_units"][0]["text"]
    )
    cached = query("MX-41有哪些要求")
    assert cached["cache_hit"] is True
    assert cached["result_origin"] == "cache"
    assert cached["generation_mode"] == "llm"
    assert cached["generation_called_this_request"] is False
    assert cached["rewrite_called_this_request"] is False
    assert cached["cache_key"] == first["cache_key"]
    assert cached["answer"] == first["answer"]
    assert len(requests) == 2
    refused = query("MX-41 负责人的手机号是什么")
    assert refused["answer"] is None, refused
    assert refused["generation_mode"] == "none"
    assert refused["generation_called_this_request"] is True
    refused_payload = json.loads(requests[2].content)
    refused_input = json.loads(refused_payload["messages"][1]["content"])
    assert refused_input["read_units"]
    assert "answer_support_set" not in refused_input
    assert "model_evidence_candidates" not in refused_input
    assert len(requests) == 3
    failing = True
    # 泛问需要资料生成；标量维护周期题已有直接证书，不应调用 Provider。
    failure = query("MX-41具体有哪些要求")
    assert failure["status"] == "PROVIDER_UNAVAILABLE", failure
    assert failure["answer"] is None
    assert failure["generation_mode"] == "none"
    assert failure["generation_called_this_request"] is True
    assert len(requests) == 4
    page = harness.runtime.history.list_history()
    assert {item["status"] for item in page["items"]} == {
        "ANSWERED",
        "REFUSED",
    }
    failed_detail = harness.runtime.history.detail(failure["trace_id"])
    assert failed_detail["status"] == "REFUSED"
    assert "fallback_answer_available" not in failed_detail
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
        item["usage"] == 260 and item["call_count"] == 2
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
