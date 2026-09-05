from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    BudgetRequest,
    ProviderBudgetLedger,
)
from rag_app.core.identifiers import canonical_sha256

_PAYLOAD = canonical_sha256({"input": ["公开合成示例"]})
_REQUEST = BudgetRequest(
    provider="jina",
    operation="embedding.document",
    request_identity=canonical_sha256("synthetic-connection-v1"),
    payload_identity=_PAYLOAD,
    estimated_input_tokens=10,
)


def _campaign(**changes: object) -> BudgetCampaign:
    return replace(
        BudgetCampaign(
            campaign_id="campaign-test",
            authorization_id="authorization-test",
            scope="synthetic-only",
            request_limit=25,
            estimated_token_limit=1000,
            approved_payload_hashes=(_PAYLOAD,),
            provider_token_limits={"jina": 600, "aliyun": 600},
        ),
        **changes,
    )


def _reserve(
    ledger: ProviderBudgetLedger, request: BudgetRequest = _REQUEST
) -> str:
    return ledger.reserve(
        "campaign-test",
        authorization_id="authorization-test",
        scope="synthetic-only",
        step_id="document",
        request=request,
    )


def test_restart_retains_historical_estimate_and_observed_usage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = ProviderBudgetLedger(path)
    campaign = _campaign()
    ledger.create_campaign(campaign)
    events = [
        {
            "event_id": "event-1",
            "provider": "jina",
            "operation": "embedding.document",
            "forwarded": True,
            "estimated_input_tokens": 119,
            "observed_tokens": 242,
        },
        {
            "event_id": "event-2",
            "provider": "aliyun",
            "operation": "embedding.document",
            "forwarded": True,
            "estimated_input_tokens": 38,
            "observed_tokens": None,
        },
        {
            "event_id": "event-local",
            "provider": "aliyun",
            "operation": "embedding.document",
            "forwarded": False,
            "estimated_input_tokens": 19,
        },
    ]
    source = canonical_sha256("synthetic-source")
    ledger.import_history(
        "campaign-test", source_identity=source, events=events
    )
    restarted = ProviderBudgetLedger(path)
    restarted.create_campaign(campaign)
    restarted.import_history(
        "campaign-test", source_identity=source, events=events
    )
    summary = restarted.summary("campaign-test")
    assert summary["forwarded"] == 2
    assert summary["locally_blocked"] == 1
    assert summary["estimated_input_tokens"] == 157
    assert summary["observed_tokens"] == 242
    assert summary["observed_usage_status"] == "unknown"
    assert summary["unknown_usage_attempts"] == 1
    assert summary["locally_blocked_estimated_tokens"] == 19
    with pytest.raises(BudgetBlockedError, match="HISTORICAL_EVENT_CONFLICT"):
        restarted.import_history(
            "campaign-test",
            source_identity=source,
            events=[{**events[0], "estimated_input_tokens": 0}],
        )


def test_campaign_cannot_reset_or_rebind_authorization(tmp_path: Path) -> None:
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    campaign = _campaign(request_limit=1)
    ledger.create_campaign(campaign)
    _reserve(ledger)
    ledger.create_campaign(campaign)
    with pytest.raises(BudgetBlockedError) as exhausted:
        _reserve(ledger)
    assert exhausted.value.reason == "BLOCKED_BUDGET"
    assert exhausted.value.minimum_additional["requests"] == 1
    with pytest.raises(BudgetBlockedError, match="IMMUTABLE"):
        ledger.create_campaign(replace(campaign, request_limit=25))
    with pytest.raises(BudgetBlockedError, match="AUTHORIZATION_ALREADY_BOUND"):
        ledger.create_campaign(replace(campaign, campaign_id="new-process"))
    assert ledger.summary("campaign-test")["reserved"] == 1


def test_concurrent_connections_cannot_over_reserve(tmp_path: Path) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = ProviderBudgetLedger(path)
    ledger.create_campaign(_campaign(request_limit=3))

    def reserve_one(_: int) -> bool:
        try:
            _reserve(ProviderBudgetLedger(path))
        except BudgetBlockedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(reserve_one, range(20)))
    assert sum(outcomes) == 3
    summary = ledger.summary("campaign-test")
    assert summary["reserved"] == 3
    assert summary["locally_blocked"] == 17
    assert summary["forwarded"] == 0
    assert summary["estimated_input_tokens"] == 30
    assert summary["observed_tokens"] is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"estimated_token_limit": 9}, "estimated_input_tokens"),
        (
            {"provider_token_limits": {"jina": 9}},
            "provider_estimated_input_tokens",
        ),
        ({"provider_request_limits": {"jina": 1}}, "provider_requests"),
        ({"step_request_limits": {"document": 1}}, "step_requests"),
    ],
)
def test_all_cumulative_limits_are_enforced(
    tmp_path: Path, change: dict[str, object], expected: str
) -> None:
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    ledger.create_campaign(_campaign(**change))
    if expected.endswith("requests"):
        _reserve(ledger)
    with pytest.raises(BudgetBlockedError) as error:
        _reserve(ledger)
    assert error.value.minimum_additional[expected] == 1


@pytest.mark.parametrize(
    ("authorization_id", "scope", "payload", "expected"),
    [
        ("wrong", "synthetic-only", _PAYLOAD, "AUTHORIZATION_ID_MISMATCH"),
        (
            "authorization-test",
            "wrong",
            _PAYLOAD,
            "AUTHORIZATION_SCOPE_MISMATCH",
        ),
        (
            "authorization-test",
            "synthetic-only",
            canonical_sha256("private"),
            "PAYLOAD_NOT_APPROVED",
        ),
    ],
)
def test_authorization_and_payload_denied_before_forward(
    tmp_path: Path,
    authorization_id: str,
    scope: str,
    payload: str,
    expected: str,
) -> None:
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    ledger.create_campaign(_campaign())
    with pytest.raises(BudgetBlockedError, match=expected):
        ledger.reserve(
            "campaign-test",
            authorization_id=authorization_id,
            scope=scope,
            step_id="document",
            request=replace(_REQUEST, payload_identity=payload),
        )
    assert ledger.summary("campaign-test")["forwarded"] == 0
    assert ledger.summary("campaign-test")["reserved"] == 0


def test_diagnostics_do_not_persist_free_text_or_secret(tmp_path: Path) -> None:
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    ledger.create_campaign(_campaign())
    attempt = _reserve(ledger)
    ledger.mark_forwarded(attempt)
    ledger.finish(
        attempt,
        status="HTTP_ERROR",
        request_id="Bearer synthetic-private-key not allowed",
        http_status=400,
    )
    record = ledger.attempts("campaign-test")[0]
    assert record["request_id"] is None
    assert record["safe_code"] == "HTTP_400"
    assert "private-key" not in str(record)
    assert record["observed_tokens"] is None


def test_read_only_reports_do_not_create_or_mutate_ledger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = ProviderBudgetLedger(path)
    ledger.create_campaign(_campaign())
    _reserve(ledger)
    before = path.read_bytes()
    reader = ProviderBudgetLedger(path, read_only=True)
    assert reader.summary("campaign-test")["reserved"] == 1
    assert path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        _reserve(reader)
    missing = tmp_path / "missing" / "budget.sqlite3"
    absent = ProviderBudgetLedger(missing, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        absent.campaign("campaign-test")
    assert not missing.parent.exists()


def test_active_campaign_cannot_be_replaced_and_restore_requires_reconciliation(
    tmp_path: Path,
) -> None:
    ledger = ProviderBudgetLedger(tmp_path / "provider-budget.sqlite3")
    campaign = _campaign()
    ledger.create_campaign(campaign)
    ledger.activate_campaign(campaign.campaign_id)
    ledger.activate_campaign(campaign.campaign_id)
    ledger.create_campaign(
        replace(
            campaign,
            campaign_id="other-campaign",
            authorization_id="other-auth",
        )
    )
    with pytest.raises(BudgetBlockedError, match="ACTIVE_CAMPAIGN_IMMUTABLE"):
        ledger.activate_campaign("other-campaign")
    reserved = _reserve(ledger)
    (tmp_path / "provider-budget.restore-blocked").write_text(
        "reconcile", encoding="utf-8"
    )
    with pytest.raises(
        BudgetBlockedError, match="BOUNDARY_RECONCILIATION_REQUIRED"
    ):
        ledger.active_campaign()
    with pytest.raises(
        BudgetBlockedError, match="BOUNDARY_RECONCILIATION_REQUIRED"
    ):
        _reserve(ledger)
    assert ledger.attempts(campaign.campaign_id)[0]["attempt_id"] == reserved
    assert ledger.summary(campaign.campaign_id)["reserved"] == 1
