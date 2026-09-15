"""湾事通模式模型配置写锁与 Universal 兼容回归。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rag_app.wanshitong.admin_api import ADMIN_BASE_PATH
from rag_app.wanshitong.admin_policy import MODEL_CONFIGURATION_LOCKED
from tests.product_support import build_product_harness
from tests.wanshitong.support import PublicHarness


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/provider-credentials"),
        ("POST", "/api/v1/provider-credentials/cred_test:rotate"),
        ("POST", "/api/v1/provider-connections"),
        ("PATCH", "/api/v1/provider-connections/conn_test"),
        ("POST", "/api/v1/provider-connections/conn_test:validate"),
        ("POST", "/api/v1/knowledge-bases/kb_test/retrieval-profiles"),
        (
            "POST",
            "/api/v1/retrieval-profiles/pfr_test/authorization:approve",
        ),
        ("POST", "/api/v1/jobs/job_test/retrieval-authorization:approve"),
        ("POST", "/api/v1/retrieval-profiles/pfr_test:activate"),
        ("PUT", "/api/v1/knowledge-bases/kb_test/model-settings"),
        (
            "POST",
            "/api/v1/knowledge-bases/kb_test/corpus-authorization:approve",
        ),
        ("POST", "/api/v1/provider-budget/revisions"),
    ],
)
def test_model_configuration_writes_are_locked_after_authentication(
    public_harness: PublicHarness, method: str, path: str
) -> None:
    response = public_harness.client.request(
        method,
        path,
        headers=public_harness.product.write_headers,
        json={},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == MODEL_CONFIGURATION_LOCKED
    assert public_harness.product.runtime.control.list_connections() == ()


def test_authentication_and_csrf_run_before_configuration_lock(
    public_harness: PublicHarness,
) -> None:
    path = "/api/v1/provider-credentials"
    missing_csrf = public_harness.client.post(path, json={})
    with TestClient(public_harness.app) as anonymous:
        unauthenticated = anonymous.post(path, json={})

    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "CSRF_REQUIRED"
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == (
        "AUTHENTICATION_REQUIRED"
    )


def test_read_only_model_projection_is_available(
    public_harness: PublicHarness,
) -> None:
    response = public_harness.client.get(ADMIN_BASE_PATH + "/models")

    assert response.status_code == 200
    assert set(response.json()) == {
        "embedding",
        "reranker",
        "llm",
        "retrieval_profile",
        "kb_model_settings",
        "image_ocr",
        "pdf_parser",
    }
    assert response.json()["embedding"]["configured"] is False
    assert response.json()["llm"]["configured"] is False


def test_universal_mode_keeps_provider_configuration_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RAG_PRODUCT_MODE", raising=False)
    harness = build_product_harness(tmp_path)
    try:
        response = harness.client.post(
            "/api/v1/provider-credentials",
            headers=harness.write_headers,
            json={
                "provider_type": "jina",
                "source": "database_encrypted",
                "secret_value": "synthetic-universal-secret",
            },
        )

        assert response.status_code == 201
        assert response.json()["configured"] is True
    finally:
        harness.close()
