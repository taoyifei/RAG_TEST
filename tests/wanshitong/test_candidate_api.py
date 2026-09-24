"""候选问答入口沿用管理员认证，并始终输出有限 SSE 终态。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rag_app.core.errors import ProviderInvalidResponse
from rag_app.core.models import ProviderCall
from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.candidate_api import _safe_error_diagnostics
from tests.wanshitong.support import PublicHarness


def test_candidate_chat_requires_admin_session_and_csrf(
    public_harness: PublicHarness,
) -> None:
    endpoint = ADMIN_BASE_PATH + "/candidate/chat"
    body = {"query": "资料中有何说明？"}
    with TestClient(public_harness.app) as anonymous:
        assert anonymous.post(endpoint, json=body).status_code == 401
    assert public_harness.client.post(endpoint, json=body).status_code == 403


def test_candidate_chat_empty_index_emits_terminal_error(
    public_harness: PublicHarness,
) -> None:
    response = public_harness.client.post(
        ADMIN_BASE_PATH + "/candidate/chat",
        json={"query": "资料中有何说明？"},
        headers=public_harness.product.write_headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: error\n" in response.text
    assert response.text.endswith("\n\n")


def test_candidate_error_exposes_only_safe_provider_reason_codes() -> None:
    error = ProviderInvalidResponse(
        "Provider 响应无效。",
        stage="provider.openai_compatible.generation",
        details={"reason_code": "INVALID_STREAM_SCHEMA"},
    )
    error.provider_call = ProviderCall(
        provider_id="fixture",
        operation="generation",
        call_count=1,
        retry_count=0,
        elapsed_ms=1,
        transport_diagnostics={
            "contract_detail": "CHAT_OUTPUT_TRUNCATED",
            "request_id": "private-request-id",
        },
    )

    assert _safe_error_diagnostics(error) == {
        "provider_reason_code": "INVALID_STREAM_SCHEMA",
        "contract_detail": "CHAT_OUTPUT_TRUNCATED",
    }
