"""候选问答入口沿用管理员认证，并始终输出有限 SSE 终态。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
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
