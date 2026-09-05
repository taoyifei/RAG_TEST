from dataclasses import asdict
from pathlib import Path

from fastapi.testclient import TestClient

from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.api.product import create_product_app
from tests.adapters.providers.test_budget_ledger import _campaign, _reserve
from tests.adapters.providers.test_budget_revision import approved_revision
from tests.product_support import build_product_harness

_ROUTE = "/api/v1/provider-budget/revisions"


def test_only_existing_admin_session_with_csrf_can_apply_revision(
    tmp_path: Path,
) -> None:
    harness = build_product_harness(tmp_path)
    try:
        assert (
            harness.client.get("/api/v1/provider-budget/campaign").status_code
            == 409
        )
        campaign = _campaign(request_limit=1)
        path = harness.runtime.settings.data_dir / "provider-budget.sqlite3"
        ledger = ProviderBudgetLedger(path)
        ledger.create_campaign(campaign)
        ledger.activate_campaign(campaign.campaign_id)
        _reserve(ledger)
        payload = asdict(approved_revision(campaign))
        snapshot = harness.client.get("/api/v1/provider-budget/campaign")
        assert snapshot.status_code == 200
        assert (
            snapshot.json()["payload_set_identity"]
            == payload["payload_set_identity"]
        )
        assert snapshot.json()["previous_revision_id_for_update"] is None
        for scope in ("system:read", "knowledge:write", "query:read"):
            token = harness.runtime.auth.create_access_token(
                name="测试受限 Token", scopes=(scope,)
            )
            with TestClient(create_product_app(harness.runtime)) as client:
                response = client.post(
                    _ROUTE,
                    json=payload,
                    headers={"Authorization": f"Bearer {token.token}"},
                )
                assert response.status_code == 403
                assert (
                    client.get(
                        "/api/v1/provider-budget/campaign",
                        headers={"Authorization": f"Bearer {token.token}"},
                    ).status_code
                    == 403
                )
        assert ledger.campaign(campaign.campaign_id) == campaign
        assert harness.client.post(_ROUTE, json=payload).status_code == 403
        rejected = harness.client.post(
            _ROUTE,
            headers=harness.write_headers,
            json={**payload, "status": "PROPOSED"},
        )
        assert rejected.status_code == 409
        for _ in range(2):
            response = harness.client.post(
                _ROUTE, headers=harness.write_headers, json=payload
            )
            assert response.status_code == 200, response.text
            assert response.json()["budget"]["reserved"] == 1
        assert ledger.campaign(campaign.campaign_id).request_limit == 3
        snapshot = harness.client.get("/api/v1/provider-budget/campaign")
        assert (
            snapshot.json()["previous_revision_id_for_update"]
            == payload["revision_id"]
        )
        schema = create_product_app(harness.runtime).openapi()
        body_schema = schema["paths"][_ROUTE]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        assert body_schema["additionalProperties"] is False
        assert body_schema["properties"]["status"]["enum"] == ["APPROVED"]
        with harness.runtime.connections.transaction() as connection:
            session = connection.execute(
                "SELECT session_id FROM console_sessions"
            ).fetchone()
        with ledger._transaction() as connection:
            audit = connection.execute(
                "SELECT admin_session_id FROM provider_budget_revisions"
            ).fetchall()
        assert len(audit) == 1
        assert audit[0]["admin_session_id"] == session["session_id"]
    finally:
        harness.close()
