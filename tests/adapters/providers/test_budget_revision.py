from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_revision import (
    BudgetAuthorizationRevision,
    budget_payload_set_identity,
)
from tests.adapters.providers.test_budget_ledger import _campaign, _reserve


def approved_revision(
    campaign: BudgetCampaign, **changes: object
) -> BudgetAuthorizationRevision:
    """为隔离测试构造明确标记为虚构人的审批。"""
    return replace(
        BudgetAuthorizationRevision(
            revision_id="revision-synthetic-1",
            campaign_id=campaign.campaign_id,
            previous_revision_id=None,
            authorization_id=campaign.authorization_id,
            approval_reference="approval-synthetic-1",
            approver="测试用虚构管理员",
            approved_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            scope=campaign.scope,
            payload_set_identity=budget_payload_set_identity(campaign),
            request_limit=3,
            estimated_token_limit=35,
            provider_token_limits={"jina": 35, "aliyun": 35},
            reason="仅用于隔离回归的虚构批准",
            status="APPROVED",
        ),
        **changes,
    )


def _setup(tmp_path: Path) -> tuple[ProviderBudgetLedger, BudgetCampaign]:
    campaign = _campaign(request_limit=1, estimated_token_limit=20)
    ledger = ProviderBudgetLedger(tmp_path / "provider-budget.sqlite3")
    ledger.create_campaign(campaign)
    ledger.activate_campaign(campaign.campaign_id)
    attempt = _reserve(ledger)
    ledger.mark_forwarded(attempt)
    ledger.finish(attempt, status="TRANSPORT_ERROR")
    return ledger, campaign


def test_revision_retains_unknown_usage_and_replay_does_not_reset(
    tmp_path: Path,
) -> None:
    ledger, campaign = _setup(tmp_path)
    revision = approved_revision(campaign)
    before = ledger.attempts(campaign.campaign_id)
    with pytest.raises(BudgetBlockedError, match="BLOCKED_BUDGET"):
        _reserve(ledger)
    ledger.apply_revision(revision, admin_session_id="sess_synthetic")
    restarted = ProviderBudgetLedger(ledger.path)
    restarted.apply_revision(revision, admin_session_id="sess_synthetic")
    assert restarted.base_campaign(campaign.campaign_id) == campaign
    assert restarted.campaign(campaign.campaign_id).request_limit == 3
    assert restarted.attempts(campaign.campaign_id)[0] == before[0]
    summary = restarted.summary(campaign.campaign_id)
    assert summary["reserved"] == 1
    assert summary["estimated_input_tokens"] == 10
    assert summary["unknown_usage_attempts"] == 1
    assert summary["observed_tokens"] is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": "PROPOSED"}, "NOT_APPROVED"),
        ({"approver": ""}, "METADATA_REQUIRED"),
        ({"approval_reference": ""}, "ID_INVALID"),
        ({"expires_at": "2020-01-01T00:00:00+00:00"}, "TIME_INVALID"),
        ({"approved_at": "2999-01-01T00:00:00+00:00"}, "TIME_INVALID"),
        ({"scope": "other"}, "SCOPE_MISMATCH"),
        ({"payload_set_identity": "wrong"}, "SCOPE_MISMATCH"),
        ({"campaign_id": "other"}, "BINDING_REQUIRED"),
        ({"previous_revision_id": "missing"}, "CHAIN_MISMATCH"),
        ({"estimated_token_limit": 9}, "BELOW_CONSUMPTION"),
        (
            {"provider_token_limits": {"jina": 9, "aliyun": 35}},
            "BELOW_CONSUMPTION",
        ),
        ({"provider_token_limits": {}}, "SUBLIMITS_REQUIRED"),
    ],
)
def test_unapproved_or_inapplicable_revision_is_rejected(
    tmp_path: Path, changes: dict[str, object], reason: str
) -> None:
    ledger, campaign = _setup(tmp_path)
    with pytest.raises(BudgetBlockedError, match=reason):
        ledger.apply_revision(
            approved_revision(campaign, **changes),
            admin_session_id="sess_synthetic",
        )
    assert ledger.campaign(campaign.campaign_id) == campaign


def test_concurrent_revision_and_reservation_preserve_cumulative_cap(
    tmp_path: Path,
) -> None:
    ledger, campaign = _setup(tmp_path)
    revision = approved_revision(campaign)

    def run(index: int) -> bool:
        instance = ProviderBudgetLedger(ledger.path)
        if index % 2 == 0:
            instance.apply_revision(revision, admin_session_id="sess_synthetic")
            return False
        try:
            _reserve(instance)
        except BudgetBlockedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(run, range(24)))
    assert sum(outcomes) == 2
    assert ledger.summary(campaign.campaign_id)["reserved"] == 3


def test_expired_revision_blocks_dispatch_but_can_be_renewed(
    tmp_path: Path,
) -> None:
    ledger, campaign = _setup(tmp_path)
    revision = approved_revision(campaign)
    ledger.apply_revision(revision, admin_session_id="sess_synthetic")
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE provider_budget_revisions SET configuration="
            "json_set(configuration,'$.approved_at',?, '$.expires_at',?)",
            ("2020-01-01T00:00:00+00:00", "2021-01-01T00:00:00+00:00"),
        )
    with pytest.raises(BudgetBlockedError, match="TIME_INVALID"):
        _reserve(ledger)
    snapshot = ledger.authorization_snapshot(campaign.campaign_id)
    assert snapshot["authorization_valid"] is False
    assert snapshot["previous_revision_id_for_update"] == revision.revision_id
    renewal = approved_revision(
        campaign,
        revision_id="revision-synthetic-2",
        approval_reference="approval-synthetic-2",
        previous_revision_id=revision.revision_id,
    )
    ledger.apply_revision(renewal, admin_session_id="sess_synthetic")
    _reserve(ledger)
    (tmp_path / "provider-budget.restore-blocked").write_text(
        "RECONCILE_REQUIRED", encoding="utf-8"
    )
    with pytest.raises(BudgetBlockedError, match="RECONCILIATION_REQUIRED"):
        ledger.apply_revision(renewal, admin_session_id="sess_synthetic")
    with pytest.raises(BudgetBlockedError, match="RECONCILIATION_REQUIRED"):
        _reserve(ProviderBudgetLedger(ledger.path))


def test_conflicting_replay_and_reused_approval_reference_are_rejected(
    tmp_path: Path,
) -> None:
    ledger, campaign = _setup(tmp_path)
    revision = approved_revision(campaign)
    ledger.apply_revision(revision, admin_session_id="sess_synthetic")
    with pytest.raises(BudgetBlockedError, match="REVISION_CONFLICT"):
        ledger.apply_revision(
            replace(revision, request_limit=4),
            admin_session_id="sess_synthetic",
        )
    with pytest.raises(BudgetBlockedError, match="REFERENCE_ALREADY_USED"):
        ledger.apply_revision(
            replace(
                revision,
                revision_id="revision-synthetic-2",
                previous_revision_id=revision.revision_id,
            ),
            admin_session_id="sess_synthetic",
        )
    assert ledger.campaign(campaign.campaign_id).request_limit == 3
