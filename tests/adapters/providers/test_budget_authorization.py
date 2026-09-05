from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from rag_app.adapters.providers.budget_authorization import (
    bind_existing_product_campaign,
    budget_initialization,
    provider_request_lease,
)
from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import BudgetedTransport
from rag_app.core.identifiers import canonical_sha256


def _campaign() -> BudgetCampaign:
    return BudgetCampaign(
        campaign_id="bind-campaign",
        authorization_id="bind-authorization",
        scope="synthetic-only",
        request_limit=25,
        estimated_token_limit=1000,
        provider_token_limits={"jina": 600, "aliyun": 600},
        approved_payload_hashes=(canonical_sha256({"input": ["synthetic"]}),),
    )


def _history(data_dir: Path) -> None:
    connection = sqlite3.connect(data_dir / "universal-rag.sqlite3")
    try:
        connection.executescript(
            """
            CREATE TABLE provider_connections (
                connection_id TEXT, provider_type TEXT
            );
            CREATE TABLE provider_operation_events (
                event_id TEXT, connection_id TEXT, operation TEXT,
                status_category TEXT, estimated_tokens INTEGER,
                observed_tokens INTEGER, retry_count INTEGER,
                cache_hit INTEGER, occurred_at TEXT
            );
            INSERT INTO provider_connections VALUES ('jina-test', 'jina');
            INSERT INTO provider_connections
            VALUES ('aliyun-test', 'aliyun-model-studio');
            """
        )
        connection.executemany(
            "INSERT INTO provider_operation_events "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "synthetic-1",
                    "jina-test",
                    "embedding.document",
                    "SUCCESS",
                    6,
                    9,
                    0,
                    0,
                    "1",
                ),
                (
                    "synthetic-2",
                    "aliyun-test",
                    "embedding.query",
                    "http_5xx",
                    4,
                    None,
                    1,
                    0,
                    "2",
                ),
                (
                    "synthetic-3",
                    "aliyun-test",
                    "embedding.document",
                    "invalid_configuration",
                    3,
                    None,
                    0,
                    0,
                    "3",
                ),
                (
                    "synthetic-cache",
                    "jina-test",
                    "embedding.query",
                    "SUCCESS",
                    100,
                    None,
                    0,
                    1,
                    "4",
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def test_binding_requires_maintenance_and_imports_actual_rows_once(
    tmp_path: Path,
) -> None:
    _history(tmp_path)
    source = tmp_path / "universal-rag.sqlite3"
    before = source.read_bytes()
    with pytest.raises(
        BudgetBlockedError, match="BLOCKED_MAINTENANCE_REQUIRED"
    ):
        bind_existing_product_campaign(tmp_path, _campaign())
    assert not (tmp_path / "provider-budget.sqlite3").exists()
    summary = bind_existing_product_campaign(
        tmp_path, _campaign(), maintenance_confirmed=True
    )
    assert summary["forwarded"] == 3
    assert summary["estimated_input_tokens"] == 14
    assert summary["observed_tokens"] == 9
    assert summary["unknown_usage_attempts"] == 2
    assert summary["locally_blocked"] == 1
    assert summary["locally_blocked_estimated_tokens"] == 3
    assert source.read_bytes() == before
    repeated = bind_existing_product_campaign(
        tmp_path, _campaign(), maintenance_confirmed=True
    )
    assert repeated == summary
    assert not (tmp_path / "provider-budget.initializing").exists()
    assert (
        ProviderBudgetLedger(
            tmp_path / "provider-budget.sqlite3", read_only=True
        ).active_campaign()
        is not None
    )


def test_initialization_refuses_inflight_and_leaves_fail_closed_marker(
    tmp_path: Path,
) -> None:
    _history(tmp_path)
    with (
        provider_request_lease(tmp_path),
        pytest.raises(BudgetBlockedError, match="BLOCKED_INFLIGHT"),
    ):
        bind_existing_product_campaign(
            tmp_path, _campaign(), maintenance_confirmed=True
        )
    assert (tmp_path / "provider-budget.initializing").exists()
    assert not (tmp_path / "provider-budget.sqlite3").exists()


def test_initialization_stops_new_http_before_any_ledger_exists(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    with (
        budget_initialization(tmp_path),
        httpx.Client(
            transport=BudgetedTransport(
                httpx.MockTransport(handler),
                ledger_path=tmp_path / "provider-budget.sqlite3",
            )
        ) as client,
        pytest.raises(BudgetBlockedError, match="BLOCKED_INFLIGHT"),
    ):
        client.post(
            "https://api.jina.ai/v1/embeddings", json={"input": ["synthetic"]}
        )
    assert requests == []
