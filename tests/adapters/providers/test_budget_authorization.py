from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from rag_app.adapters.providers.budget_authorization import (
    bind_existing_product_campaign,
    budget_initialization,
    provider_request_lease,
    read_product_budget_history,
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
            CREATE TABLE provider_validation_runs (
                validation_id TEXT,connection_id TEXT,operation TEXT,
                started_at TEXT,finished_at TEXT,status TEXT,http_category TEXT,
                estimated_tokens INTEGER,observed_tokens INTEGER,
                validation_mode TEXT,diagnostics_json TEXT
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
                    "2026-01-01T00:00:01+00:00",
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
                    "2026-01-01T00:00:02+00:00",
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
                    "2026-01-01T00:00:03+00:00",
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
                    "2026-01-01T00:00:04+00:00",
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


def _validation(
    database: sqlite3.Connection, identifier: str, *, matched: bool
) -> None:
    database.execute(
        "INSERT INTO provider_validation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            identifier,
            "jina-test",
            "embedding.document",
            "2026-01-01T00:00:00+00:00"
            if matched
            else "2026-02-01T00:00:00+00:00",
            "2026-01-01T00:00:00.5+00:00"
            if matched
            else "2026-02-01T00:00:00+00:00",
            "succeeded",
            "SUCCESS",
            6,
            9,
            "live",
            '{"request_dispatched":true}',
        ),
    )


def test_history_reconciles_validation_once_and_reserves_missing_event_unknown(
    tmp_path: Path,
) -> None:
    _history(tmp_path)
    source = tmp_path / "universal-rag.sqlite3"
    with sqlite3.connect(source) as database:
        _validation(database, "validation-covered", matched=True)
        _validation(database, "validation-missing-event", matched=False)
    before = source.read_bytes()
    summary = read_product_budget_history(tmp_path)
    assert summary["forwarded"] == 4
    assert summary["estimated_input_tokens"] == 20
    assert summary["unmatched_validation_attempts"] == 1
    assert summary["unknown_forwarding_attempts"] == 2
    assert source.read_bytes() == before
    assert not (tmp_path / "provider-budget.sqlite3").exists()
    bound = bind_existing_product_campaign(
        tmp_path, _campaign(), maintenance_confirmed=True
    )
    assert bound["forwarded"] == summary["forwarded"]
    replay = bind_existing_product_campaign(
        tmp_path, _campaign(), maintenance_confirmed=True
    )
    assert replay == bound


def test_history_ambiguous_correlations_and_unknown_provider_fail_closed(
    tmp_path: Path,
) -> None:
    _history(tmp_path)
    source = tmp_path / "universal-rag.sqlite3"
    with sqlite3.connect(source) as database:
        _validation(database, "validation-ambiguous", matched=True)
        database.execute(
            "INSERT INTO provider_operation_events SELECT 'duplicate',"
            "connection_id,operation,status_category,estimated_tokens,"
            "observed_tokens,retry_count,cache_hit,occurred_at "
            "FROM provider_operation_events WHERE event_id='synthetic-1'"
        )
    with pytest.raises(BudgetBlockedError, match="CORRELATION_AMBIGUOUS"):
        read_product_budget_history(tmp_path)
    with sqlite3.connect(source) as database:
        database.execute("DELETE FROM provider_validation_runs")
        database.execute(
            "DELETE FROM provider_connections WHERE provider_type='jina'"
        )
    with pytest.raises(BudgetBlockedError, match="PROVIDER_IDENTITY_UNKNOWN"):
        read_product_budget_history(tmp_path)


def test_rebinding_does_not_reimport_events_already_protected_by_transport(
    tmp_path: Path,
) -> None:
    _history(tmp_path)
    before = bind_existing_product_campaign(
        tmp_path, _campaign(), maintenance_confirmed=True
    )
    with sqlite3.connect(tmp_path / "universal-rag.sqlite3") as database:
        database.execute(
            "INSERT INTO provider_operation_events VALUES "
            "('already-in-ledger','jina-test','embedding.query','SUCCESS',"
            "7,8,0,0,?)",
            (datetime.now(UTC).isoformat(),),
        )
    replay = bind_existing_product_campaign(
        tmp_path, _campaign(), maintenance_confirmed=True
    )
    assert replay == before
