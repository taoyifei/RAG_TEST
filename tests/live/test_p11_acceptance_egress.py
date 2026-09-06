"""验收累计授权与普通产品日预算保持各自边界。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.core.errors import PolicyDenied
from rag_app.product.live_acceptance import AcceptanceState
from rag_app.product.live_acceptance_backend import ProductAcceptanceBackend
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


@pytest.fixture
def acceptance_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[
    tuple[ProductHarness, ProductAcceptanceBackend, ProviderBudgetLedger]
]:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-only")
    harness = build_product_harness(tmp_path)
    _, kb_id = create_project_and_knowledge_base(harness)
    _, _, jina_id, aliyun_id = create_provider_connections(harness)
    validate_five_operations(harness, jina_id, aliyun_id)
    profile_id = activate_hot_standby_profile(
        harness, kb_id, jina_id, aliyun_id
    )
    ledger = ProviderBudgetLedger(
        harness.runtime.data_dir / "provider-budget.sqlite3"
    )
    ledger.create_campaign(
        BudgetCampaign(
            campaign_id="scoped-acceptance",
            authorization_id="scoped-approval",
            scope="p11-public-synthetic-v1",
            request_limit=80,
            estimated_token_limit=12000,
            provider_request_limits={"aliyun": 40, "jina": 40},
            provider_token_limits={"aliyun": 6000, "jina": 6000},
        )
    )
    ledger.activate_campaign("scoped-acceptance")
    config = {
        "data_dir": str(harness.runtime.data_dir),
        "ledger_path": str(ledger.path),
        "campaign_id": "scoped-acceptance",
        "authorization_id": "scoped-approval",
        "scope": "p11-public-synthetic-v1",
        "candidate_identity": "new-independent-fixture",
        "jina_connection_id": jina_id,
        "aliyun_connection_id": aliyun_id,
        "source_profile_revision_id": profile_id,
        "campaign_limits": {
            "request_limit": 999999,
            "estimated_token_limit": 999999,
        },
    }
    backend = ProductAcceptanceBackend(
        config, AcceptanceState(tmp_path / "state.sqlite3", "scoped-acceptance")
    )
    try:
        yield harness, backend, ledger
    finally:
        backend.close()
        harness.close()


def test_acceptance_uses_bound_ledger_without_resetting_daily_usage(
    acceptance_budget: tuple[
        ProductHarness, ProductAcceptanceBackend, ProviderBudgetLedger
    ],
) -> None:
    harness, backend, ledger = acceptance_budget
    control = harness.runtime.control
    profile = control.get_profile(
        str(backend.config["source_profile_revision_id"])
    )
    connection_id = str(profile.standby_connection_id)
    _, normal, _ = harness.runtime.profiles.serving_contract(profile)
    before_profile = profile.model_dump()
    before_connection = control.get_connection(connection_id).model_dump()
    assert normal.aliyun_daily_request_budget == 2

    for _ in range(2):
        control.reserve_daily_provider_budget(
            connection_id,
            "embedding.query",
            7,
            request_limit=2,
            token_limit=4096,
        )
    with pytest.raises(PolicyDenied):
        control.reserve_daily_provider_budget(
            connection_id,
            "embedding.query",
            7,
            request_limit=2,
            token_limit=4096,
        )

    scoped = backend._acceptance_egress(profile, normal)
    assert scoped.aliyun_daily_request_budget == 40
    assert scoped.aliyun_daily_token_budget == 6000
    control.reserve_daily_provider_budget(
        connection_id,
        "embedding.query",
        7,
        request_limit=scoped.aliyun_daily_request_budget,
        token_limit=scoped.aliyun_daily_token_budget,
    )
    with sqlite3.connect(
        harness.runtime.data_dir / "universal-rag.sqlite3"
    ) as db:
        row = db.execute(
            "SELECT requests,estimated_tokens FROM provider_daily_budgets "
            "WHERE connection_id=? AND operation='embedding.query'",
            (connection_id,),
        ).fetchone()
    assert row == (3, 21)
    assert (
        control.get_profile(profile.profile_revision_id).model_dump()
        == before_profile
    )
    assert (
        control.get_connection(connection_id).model_dump() == before_connection
    )
    assert harness.runtime.profiles.serving_contract(profile)[1] == normal
    assert ledger.summary("scoped-acceptance")["forwarded"] == 0


@pytest.mark.parametrize(
    "changed", ["campaign_id", "authorization_id", "scope"]
)
def test_acceptance_denies_mismatched_authorization(
    acceptance_budget: tuple[
        ProductHarness, ProductAcceptanceBackend, ProviderBudgetLedger
    ],
    changed: str,
) -> None:
    harness, backend, _ = acceptance_budget
    profile = harness.runtime.control.get_profile(
        str(backend.config["source_profile_revision_id"])
    )
    normal = harness.runtime.profiles.serving_contract(profile)[1]
    backend.config[changed] = "wrong-identity"
    with pytest.raises(BudgetBlockedError):
        backend._acceptance_egress(profile, normal)


def test_acceptance_denies_different_profile_contract(
    acceptance_budget: tuple[
        ProductHarness, ProductAcceptanceBackend, ProviderBudgetLedger
    ],
) -> None:
    harness, backend, _ = acceptance_budget
    profile = harness.runtime.control.get_profile(
        str(backend.config["source_profile_revision_id"])
    )
    normal = harness.runtime.profiles.serving_contract(profile)[1]
    changed = profile.model_copy(
        update={"serving_fingerprint": "sha256:" + "9" * 64}
    )
    with pytest.raises(BudgetBlockedError):
        backend._acceptance_egress(changed, normal)
