"""模型目录、连接与持久验证记录 API 回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.product_support import (
    build_product_harness,
    create_provider_connections,
    validate_five_operations,
)


def test_catalog_connections_and_five_validation_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        _, _, jina_connection, aliyun_connection = create_provider_connections(
            harness
        )
        validate_five_operations(harness, jina_connection, aliyun_connection)
        catalog = harness.client.get("/api/v1/provider-catalog")
        history = harness.client.get(
            f"/api/v1/provider-connections/{jina_connection}/validations"
        )
        usage = harness.client.get("/api/v1/provider-usage/daily")

        assert catalog.status_code == 200
        assert catalog.json()["catalog_version"]
        assert len(history.json()["items"]) == 3
        assert usage.status_code == 200
        assert len(usage.json()["items"]) == 5
        assert all(
            item["successful_requests"] == 1
            for item in usage.json()["items"]
        )
        assert "synthetic-jina-value" not in history.text
        assert "synthetic-jina-value" not in usage.text
        assert harness.runtime.sdk.health().primary_live_evaluation_status == (
            "mock_validated"
        )
        assert harness.runtime.sdk.health().standby_live_evaluation_status == (
            "mock_validated"
        )
        assert harness.runtime.sdk.health().reranker_live_evaluation_status == (
            "mock_validated"
        )
    finally:
        harness.close()
