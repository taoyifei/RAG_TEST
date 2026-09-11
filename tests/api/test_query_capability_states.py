"""回答模型配置、授权、预算与运行失败的 Product API 状态回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

import httpx
import pytest

from tests.adapters.parsers.docx.fixtures import build_package
from tests.product_support import (
    ProductHarness,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _upload_capability_fixture(
    harness: ProductHarness,
    project_id: str,
    knowledge_base_id: str,
) -> None:
    """上传同时含直接事实与待模型核验候选的公开合成文档。

    Args:
        harness: 已登录的 Product 测试环境。
        project_id: 当前测试项目。
        knowledge_base_id: 当前测试知识库。

    Returns:
        文档入库并激活新 Revision 后无返回值。

    """
    job = harness.runtime.sdk.create_document(
        project_id,
        knowledge_base_id,
        display_name="公开能力状态手册.docx",
        content=build_package(
            "<w:p><w:r><w:t>设备 MX-41 的维护周期为 14 天。"
            "</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>合成设备维护安排尚未确定，相关团队正在评估。"
            "</w:t></w:r></w:p>"
        ),
        media_type=_MEDIA_TYPE,
        idempotency_key="query-capability-fixture",
    )
    deadline = monotonic() + 10
    while monotonic() < deadline:
        current = harness.runtime.sdk.get_job(job.job_id)
        if current.state.value not in {"queued", "running"}:
            assert current.state.value == "succeeded", current
            return
        sleep(0.01)
    raise AssertionError("公开能力状态 Fixture 上传超时")


def _configure_generation(
    harness: ProductHarness,
    knowledge_base_id: str,
    connection_id: str,
) -> None:
    """保存公开测试使用的回答模型设置。

    Args:
        harness: 已登录的 Product 测试环境。
        knowledge_base_id: 当前测试知识库。
        connection_id: 环境托管的合成百炼连接。

    Returns:
        设置成功保存后无返回值。

    """
    response = harness.client.put(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/model-settings",
        headers=harness.write_headers,
        json={
            "generation_connection_id": connection_id,
            "generation_model": "qwen3.7-flash",
        },
    )
    assert response.status_code == 200, response.text


def _approve_generation(
    harness: ProductHarness,
    knowledge_base_id: str,
    *,
    request_limit: int,
) -> None:
    """批准当前公开语料的一次有限 generation 活动。

    Args:
        harness: 已登录的 Product 测试环境。
        knowledge_base_id: 当前测试知识库。
        request_limit: generation 和累计请求上限。

    Returns:
        授权清单与预算成功建立后无返回值。

    """
    response = harness.client.post(
        f"/api/v1/knowledge-bases/{knowledge_base_id}/"
        "corpus-authorization:approve",
        headers=harness.write_headers,
        json={
            "operations": ["generation"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "request_limit": request_limit,
            "estimated_token_limit": 80_000,
            "operation_request_limits": {"generation": request_limit},
        },
    )
    assert response.status_code == 200, response.text


def _answer(
    harness: ProductHarness,
    project_id: str,
    knowledge_base_id: str,
    query: str,
) -> dict[str, object]:
    """调用同步 Product Answer API 并返回 JSON。

    Args:
        harness: 已登录的 Product 测试环境。
        project_id: 当前测试项目。
        knowledge_base_id: 当前测试知识库。
        query: 公开合成问题。

    Returns:
        通过响应模型校验后的 JSON object。

    """
    response = harness.client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}:answer",
        headers=harness.write_headers,
        json={"query": query},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _assert_refused_state(
    harness: ProductHarness,
    result: dict[str, object],
    expected_reason: str,
) -> None:
    """核对 API、History 与 Operational Trace 的拒答原因一致。

    Args:
        harness: 已登录的 Product 测试环境。
        result: Answer API 返回的公开响应。
        expected_reason: 三个投影面都应保存的能力阻断状态。

    Returns:
        三个状态面一致时无返回值。

    """
    assert result["status"] == expected_reason
    trace_id = str(result["trace_id"])
    history = harness.runtime.history.detail(trace_id)
    trace = harness.runtime.traces.detail(trace_id)
    assert history["status"] == "REFUSED"
    assert history["reason_code"] == expected_reason
    assert trace.trace.status.value == "REFUSED"
    assert trace.trace.refusal_code == expected_reason


def _grounded_response(request: httpx.Request) -> httpx.Response:
    """把最小支持集第一项原样返回为合法 claim。"""
    request_payload = json.loads(request.content)
    grounded = json.loads(request_payload["messages"][1]["content"])
    available = (
        grounded["answer_support_set"] or grounded["model_evidence_candidates"]
    )
    evidence = available[0]
    return httpx.Response(
        200,
        json={
            "model": request_payload["model"],
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
            "usage": {"total_tokens": 20},
        },
    )


def test_llm_can_validate_candidate_separate_from_empty_support_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已授权模型可核验候选，但 Prompt 不把候选伪装成直接支持集。"""
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
        _upload_capability_fixture(harness, project_id, knowledge_base_id)
        _, _, _, connection_id = create_provider_connections(harness)
        _configure_generation(harness, knowledge_base_id, connection_id)
        _approve_generation(harness, knowledge_base_id, request_limit=2)

        result = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "合成设备维护安排是什么？",
        )
        assert result["status"] == "ANSWERABLE"
        assert result["answer"]
        assert result["generation_mode"] == "llm"
        assert result["generation_called_this_request"] is True
        assert len(requests) == 1
        provider_payload = json.loads(requests[0].content)
        grounded = json.loads(provider_payload["messages"][1]["content"])
        assert grounded["answer_support_set"] == []
        assert grounded["evidence"] == []
        assert grounded["model_evidence_candidates"]
        assert (
            result["evidence"][0]["evidence_id"]
            == (grounded["model_evidence_candidates"][0]["support_id"])
        )
    finally:
        harness.close()


def test_queries_without_available_model_report_configuration_and_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有真实回答模型时拒答，并区分未配置和资料未批准。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    requests: list[httpx.Request] = []

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _grounded_response(request)

    harness = build_product_harness(
        tmp_path,
        transport_factory=lambda _: httpx.MockTransport(unexpected_request),
    )
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload_capability_fixture(harness, project_id, knowledge_base_id)

        not_configured = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "合成设备维护安排是什么？",
        )
        assert not_configured["answer"] is None
        assert not_configured["status"] == "CONFIGURATION_REQUIRED", (
            harness.runtime.history.detail(not_configured["trace_id"])[
                "diagnostics"
            ]["model_evidence_candidates"]
        )
        _assert_refused_state(
            harness,
            not_configured,
            "CONFIGURATION_REQUIRED",
        )
        assert not_configured["generation_reason_code"] == (
            "GENERATOR_NOT_CONFIGURED"
        )
        support_decisions = [
            item
            for item in harness.runtime.traces.detail(
                not_configured["trace_id"]
            ).candidate_decisions
            if item.stage == "answer_support_selection"
        ]
        assert support_decisions
        assert all(
            item.selected is False
            and item.reason_code.value == "VALIDATION_FAILED"
            and item.details == {}
            for item in support_decisions
        )

        _, _, _, connection_id = create_provider_connections(harness)
        _configure_generation(harness, knowledge_base_id, connection_id)
        policy_denied = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "合成设备维护安排是什么？",
        )
        assert policy_denied["answer"] is None
        _assert_refused_state(harness, policy_denied, "POLICY_DENIED")
        assert policy_denied["generation_reason_code"] == (
            "CORPUS_AUTHORIZATION_MISSING"
        )

        policy_blocked = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "设备 MX-41 的维护周期是多少？",
        )
        assert policy_blocked["answer"] is None
        assert policy_blocked["generation_mode"] == "none"
        assert policy_blocked["generation_called_this_request"] is False
        assert policy_blocked["generation_reason_code"] == (
            "CORPUS_AUTHORIZATION_MISSING"
        )
        assert (
            "CORPUS_AUTHORIZATION_MISSING"
            in policy_blocked["degraded_reason_codes"]
        )
        _assert_refused_state(harness, policy_blocked, "POLICY_DENIED")
        assert requests == []
    finally:
        harness.close()


def test_exhausted_budget_refuses_regardless_of_local_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算耗尽时完整与不完整支持都不得绕过真实生成。"""
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
        _upload_capability_fixture(harness, project_id, knowledge_base_id)
        _, _, _, connection_id = create_provider_connections(harness)
        _configure_generation(harness, knowledge_base_id, connection_id)
        _approve_generation(harness, knowledge_base_id, request_limit=1)

        generated = _answer(harness, project_id, knowledge_base_id, "MX-41")
        assert generated["generation_mode"] == "llm"
        assert generated["generation_called_this_request"] is True
        assert len(requests) == 1

        direct_blocked = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "设备 MX-41 的维护周期是多少？",
        )
        assert direct_blocked["answer"] is None
        assert direct_blocked["generation_mode"] == "none"
        assert direct_blocked["generation_reason_code"] == "BLOCKED_BUDGET"
        assert direct_blocked["generation_called_this_request"] is False
        _assert_refused_state(harness, direct_blocked, "BUDGET_BLOCKED")

        blocked = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "合成设备维护安排是什么？",
        )
        assert blocked["answer"] is None
        _assert_refused_state(harness, blocked, "BUDGET_BLOCKED")
        assert blocked["generation_reason_code"] == "BLOCKED_BUDGET"
        assert blocked["generation_called_this_request"] is False
        assert len(requests) == 1
    finally:
        harness.close()


@pytest.mark.parametrize("failure", ("rate_limit", "timeout"))
def test_provider_failure_refuses_regardless_of_local_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """429 与超时都必须保留调用事实并拒答。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    requests: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(429, json={"error": {"code": "Throttled"}})

    harness = build_product_harness(
        tmp_path,
        transport_factory=lambda _: httpx.MockTransport(fail),
    )
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload_capability_fixture(harness, project_id, knowledge_base_id)
        _, _, _, connection_id = create_provider_connections(harness)
        _configure_generation(harness, knowledge_base_id, connection_id)
        _approve_generation(harness, knowledge_base_id, request_limit=8)

        direct_failure = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "设备 MX-41 的维护周期是多少？",
        )
        assert direct_failure["answer"] is None
        assert direct_failure["generation_mode"] == "none"
        assert direct_failure["generation_called_this_request"] is True
        _assert_refused_state(
            harness,
            direct_failure,
            "PROVIDER_UNAVAILABLE",
        )

        unavailable = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "合成设备维护安排是什么？",
        )
        assert unavailable["answer"] is None
        _assert_refused_state(
            harness,
            unavailable,
            "PROVIDER_UNAVAILABLE",
        )
        assert unavailable["generation_called_this_request"] is True
        assert unavailable["generation_reason_code"] in {
            "PROVIDER_RATE_LIMITED",
            "PROVIDER_UNAVAILABLE",
        }
        assert len(requests) == 2
    finally:
        harness.close()


def test_invalid_model_json_with_complete_support_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无效 JSON 只修复一次，仍失败时必须拒答。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "public-synthetic-key")
    requests: list[httpx.Request] = []

    def invalid(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "qwen3.7-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "not-json",
                        },
                    }
                ],
            },
        )

    harness = build_product_harness(
        tmp_path,
        transport_factory=lambda _: httpx.MockTransport(invalid),
    )
    try:
        project_id, knowledge_base_id = create_project_and_knowledge_base(
            harness
        )
        _upload_capability_fixture(harness, project_id, knowledge_base_id)
        _, _, _, connection_id = create_provider_connections(harness)
        _configure_generation(harness, knowledge_base_id, connection_id)
        _approve_generation(harness, knowledge_base_id, request_limit=4)

        result = _answer(
            harness,
            project_id,
            knowledge_base_id,
            "设备 MX-41 的维护周期是多少？",
        )
        assert result["answer"] is None
        assert result["generation_mode"] == "none"
        assert result["generation_called_this_request"] is True
        assert result["generation_reason_code"] == "PROVIDER_INVALID_RESPONSE"
        assert len(requests) == 2
        _assert_refused_state(
            harness,
            result,
            "PROVIDER_UNAVAILABLE",
        )
    finally:
        harness.close()


__all__: tuple[str, ...] = ()
