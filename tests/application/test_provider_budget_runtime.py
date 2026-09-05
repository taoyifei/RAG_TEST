from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import provider_budget_scope
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    EmbeddingRequest,
    EmbeddingRequestRole,
    RerankRequest,
)
from rag_app.product.models import ProviderConnectionDraft
from rag_app.product.provider_runtime import (
    ProviderRuntimeRegistry,
    build_offline_mock_transport,
)
from tests.product_support import build_product_harness


def test_probe_sdk_index_embedding_and_reranker_share_persistent_limit(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    text = "验收示例：审批完成后归档。"
    embedding_payload = {
        "dimensions": 1024,
        "embedding_type": "float",
        "input": [text],
        "model": "jina-embeddings-v5-text-small",
        "normalized": True,
        "task": "retrieval.passage",
        "truncate": False,
    }
    rerank_payload = {
        "model": "jina-reranker-v3.5",
        "query": text,
        "documents": [text],
        "return_documents": False,
        "top_n": 1,
    }
    ledger = ProviderBudgetLedger(tmp_path / "ledger.sqlite3")
    ledger.create_campaign(
        BudgetCampaign(
            campaign_id="runtime-campaign",
            authorization_id="runtime-auth",
            scope="synthetic-only",
            request_limit=1,
            estimated_token_limit=100,
            approved_payload_hashes=(
                canonical_sha256(embedding_payload),
                canonical_sha256(rerank_payload),
            ),
        )
    )
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-budget-runtime-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="预算隔离测试",
                provider_type="jina",
                credential_id=credential.credential_id,
            )
        )
        providers = harness.runtime.providers
        with provider_budget_scope(
            ledger,
            campaign_id="runtime-campaign",
            authorization_id="runtime-auth",
            scope="synthetic-only",
            step_id="shared",
        ):
            first = providers.validate(
                connection.connection_id,
                operation="embedding.document",
                model="jina-embeddings-v5-text-small",
                expected_dimension=1024,
            )
            assert first.status == "succeeded"
            second = providers.validate(
                connection.connection_id,
                operation="embedding.document",
                model="jina-embeddings-v5-text-small",
                expected_dimension=1024,
            )
            assert second.status == "failed"
            assert second.safe_error_code == "BLOCKED_BUDGET"
            assert second.request_dispatched is False
            adapter = providers.embedding_adapter(
                connection.connection_id,
                slot_id="primary",
                model="jina-embeddings-v5-text-small",
                dimension=1024,
                document_policy_identity="synthetic-document-policy",
                query_policy_identity="synthetic-query-policy",
            )
            with pytest.raises(BudgetBlockedError, match="BLOCKED_BUDGET"):
                adapter.embed(
                    EmbeddingRequest(
                        slot_id="primary",
                        role=EmbeddingRequestRole.DOCUMENT,
                        texts=(text,),
                    )
                )
            adapter.close()
            reranker = providers.reranker_adapter(
                connection.connection_id, model="jina-reranker-v3.5"
            )
            with pytest.raises(BudgetBlockedError, match="BLOCKED_BUDGET"):
                reranker.rerank(
                    RerankRequest(
                        query=text, candidates=(("chunk-test", text),), limit=1
                    )
                )
            reranker.close()
        summary = ledger.summary("runtime-campaign")
        assert summary["forwarded"] == 1
        assert summary["locally_blocked"] == 3
        identities = [
            row["request_identity"]
            for row in ledger.attempts("runtime-campaign")
        ]
        assert identities[0] == identities[1] == identities[2]
        assert identities[2] != identities[3]
    finally:
        harness.close()


def test_already_cached_registry_and_independent_registry_share_active_campaign(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    ledger_path = harness.runtime.data_dir / "provider-budget.sqlite3"
    other = ProviderRuntimeRegistry(
        harness.runtime.credentials,
        harness.runtime.control,
        transport_factory=build_offline_mock_transport,
        budget_ledger_path=ledger_path,
    )
    try:
        credential = harness.runtime.credentials.create_encrypted(
            "jina", "synthetic-shared-budget-secret"
        )
        connection = harness.runtime.control.create_connection(
            ProviderConnectionDraft(
                display_name="共享账本测试",
                provider_type="jina",
                credential_id=credential.credential_id,
            )
        )
        providers = harness.runtime.providers
        before_activation = providers.validate(
            connection.connection_id,
            operation="embedding.document",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )
        assert before_activation.status == "succeeded"
        assert providers.client_count == 1
        ledger = ProviderBudgetLedger(ledger_path)
        ledger.create_campaign(
            BudgetCampaign(
                campaign_id="shared-campaign",
                authorization_id="shared-auth",
                scope="synthetic-only",
                request_limit=1,
                estimated_token_limit=100,
                approved_payload_hashes=(
                    before_activation.synthetic_payload_hash,
                ),
            )
        )
        ledger.activate_campaign("shared-campaign")
        first = providers.validate(
            connection.connection_id,
            operation="embedding.document",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )
        second = other.validate(
            connection.connection_id,
            operation="embedding.document",
            model="jina-embeddings-v5-text-small",
            expected_dimension=1024,
        )
        assert first.status == "succeeded"
        assert providers.client_count == 1
        assert second.status == "failed"
        assert second.safe_error_code == "BLOCKED_BUDGET"
        assert second.request_dispatched is False
        assert ledger.summary("shared-campaign")["forwarded"] == 1
        assert ledger.summary("shared-campaign")["locally_blocked"] == 1
    finally:
        other.close()
        harness.close()
