"""P11 Provider 用量与持久预算回归。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.errors import PolicyDenied, ProviderInvalidResponse
from rag_app.core.models import (
    EmbeddingRequest,
    EmbeddingRequestRole,
    ProviderCall,
)
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.product_support import (
    build_product_harness,
    create_provider_connections,
)


def test_standby_daily_budget_survives_runtime_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """备用预算不得因 Product Runtime 重启而清零。"""
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    _, _, _, aliyun_connection = create_provider_connections(harness)
    settings = harness.runtime.settings
    harness.runtime.control.reserve_daily_provider_budget(
        aliyun_connection,
        "embedding",
        8,
        request_limit=1,
        token_limit=16,
    )
    harness.close()

    runtime = build_product_runtime(
        settings,
        transport_factory=build_offline_mock_transport,
    )
    try:
        with pytest.raises(PolicyDenied, match="预算已耗尽"):
            runtime.control.reserve_daily_provider_budget(
                aliyun_connection,
                "embedding",
                1,
                request_limit=1,
                token_limit=16,
            )
    finally:
        runtime.close()


def test_provider_call_persists_observed_tokens_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        _, _, jina_connection, _ = create_provider_connections(harness)
        harness.runtime.control.record_provider_call(
            jina_connection,
            ProviderCall(
                provider_id="jina",
                operation="embedding.query",
                call_count=1,
                retry_count=1,
                elapsed_ms=12,
                reason_code="OK",
                model="jina-embeddings-v5-text-small",
                endpoint="api.jina.ai/v1/embeddings",
                attempt_count=2,
                status_category="SUCCESS",
                input_count=1,
                estimated_tokens=4,
                observed_tokens=3,
            ),
        )

        with harness.runtime.connections.transaction() as connection:
            rows = tuple(
                connection.execute(
                    "SELECT observed_tokens, retry_count, status_category "
                    "FROM provider_operation_events WHERE connection_id=?",
                    (jina_connection,),
                ).fetchall()
            )
        usage = harness.runtime.control.list_daily_provider_usage()

        assert [tuple(row) for row in rows] == [(3, 1, "SUCCESS")]
        assert len(usage) == 1
        assert usage[0].request_count == 1
        assert usage[0].observed_tokens == 3
        assert usage[0].retry_count == 1
    finally:
        harness.close()


def test_semantic_failure_persists_one_final_provider_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")

    def _transport(_connection: object) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "model": "jina-embeddings-v5-text-small",
                    "data": [{"index": 0, "embedding": [0.1]}],
                    "usage": {"total_tokens": 4},
                },
            )
        )

    harness = build_product_harness(tmp_path, transport_factory=_transport)
    try:
        _, _, jina_connection, _ = create_provider_connections(harness)
        adapter = harness.runtime.providers.embedding_adapter(
            jina_connection,
            slot_id="primary",
            model="jina-embeddings-v5-text-small",
            dimension=1024,
            document_policy_identity="document-policy-v1",
            query_policy_identity="query-policy-v1",
        )
        with pytest.raises(ProviderInvalidResponse) as captured:
            adapter.embed(
                EmbeddingRequest(
                    slot_id="primary",
                    role=EmbeddingRequestRole.QUERY,
                    texts=("公开合成查询",),
                )
            )
        adapter.close()

        with harness.runtime.connections.transaction() as connection:
            rows = tuple(
                connection.execute(
                    "SELECT status_category, observed_tokens, retry_count, "
                    "safe_error_code FROM provider_operation_events "
                    "WHERE connection_id=?",
                    (jina_connection,),
                ).fetchall()
            )

        assert captured.value.provider_call is not None
        assert (
            captured.value.provider_call.status_category == "RESPONSE_CONTRACT"
        )
        assert [tuple(row) for row in rows] == [
            ("RESPONSE_CONTRACT", 4, 0, "INVALID_RESPONSE_CONTRACT")
        ]
    finally:
        harness.close()
