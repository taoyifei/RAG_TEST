"""Provider Client 缓存、轮换失效与安全错误归类回归。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rag_app.product.models import ProviderConnectionDraft
from tests.product_support import (
    build_product_harness,
    create_provider_connections,
)


def test_provider_client_is_cached_and_rotation_closes_old_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        credential_id, _, connection_id, _ = create_provider_connections(
            harness
        )
        for operation in ("embedding.document", "embedding.query"):
            result = harness.runtime.providers.validate(
                connection_id,
                operation=operation,
                model="jina-embeddings-v5-text-small",
                expected_dimension=1024,
            )
            assert result.status == "succeeded"
        assert harness.runtime.providers.client_count == 1

        harness.runtime.credentials.rotate(
            credential_id, "rotated-synthetic-jina-value"
        )
        harness.runtime.providers.invalidate_credential(credential_id)
        assert harness.runtime.providers.client_count == 0
        harness.runtime.providers.validate(
            connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )
        assert harness.runtime.providers.client_count == 1
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (httpx.Response(400), "REGION_OR_WORKSPACE_INVALID"),
        (httpx.Response(401), "PROVIDER_AUTHENTICATION_FAILED"),
        (httpx.Response(403), "PROVIDER_AUTHORIZATION_DENIED"),
        (
            httpx.Response(429, headers={"Retry-After": "1"}),
            "PROVIDER_RATE_LIMITED",
        ),
        (httpx.Response(503), "PROVIDER_UPSTREAM_ERROR"),
        (httpx.Response(200, content=b"not-json"), "INVALID_JSON"),
        (
            httpx.Response(200, json={"data": [{"embedding": [0.1]}]}),
            "EMBEDDING_DIMENSION_MISMATCH",
        ),
    ],
)
def test_provider_validation_persists_safe_failures(
    tmp_path: Path,
    response: httpx.Response,
    error_code: str,
) -> None:
    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            del request
            return response

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "safe-synthetic-value"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="失败分类",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.status == "failed"
        assert result.safe_error_code == error_code
        assert "safe-synthetic-value" not in json.dumps(
            result.model_dump(mode="json")
        )
    finally:
        harness.close()


def test_provider_validation_classifies_timeout_without_leaking_secret(
    tmp_path: Path,
) -> None:
    credential_value = "synthetic-timeout-value"

    def _transport(_connection: object) -> httpx.MockTransport:
        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        return httpx.MockTransport(_handler)

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", credential_value
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="超时分类",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="embedding.query",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )

        assert result.http_category == "timeout"
        assert result.safe_error_code == "PROVIDER_TIMEOUT"
        assert credential_value not in json.dumps(
            result.model_dump(mode="json")
        )
    finally:
        harness.close()


def test_reranker_validation_requires_the_synthetic_candidate(
    tmp_path: Path,
) -> None:
    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"results": []})
        )

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        created = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-reranker-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="候选校验",
                provider_type="jina",
                credential_id=created.credential_id,
            )
        )
        result = harness.runtime.providers.validate(
            connection.connection_id,
            operation="reranking",
            model="jina-reranker-v3.5",
        )

        assert result.http_category == "mock_200"
        assert result.safe_error_code == "RERANK_CANDIDATE_MISSING"
    finally:
        harness.close()
