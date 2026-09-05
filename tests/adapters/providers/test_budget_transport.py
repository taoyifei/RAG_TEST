from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import (
    BudgetedTransport,
    payload_contract,
    provider_budget_fault,
    provider_budget_scope,
    provider_request_identity,
)
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.core.identifiers import canonical_sha256

_PAYLOAD = {"model": "synthetic-model", "input": ["公开合成文本"]}


def _ledger(
    tmp_path: Path,
    *,
    request_limit: int = 2,
    approved_request_identities: tuple[str, ...] = (),
) -> ProviderBudgetLedger:
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    payload_hash, texts, shape = payload_contract(_PAYLOAD)
    assert shape is not None
    ledger.create_campaign(
        BudgetCampaign(
            campaign_id="transport-test",
            authorization_id="authorization-test",
            scope="public-only",
            request_limit=request_limit,
            estimated_token_limit=100,
            approved_payload_hashes=(payload_hash,),
            approved_text_hashes=texts,
            approved_request_shape_hashes=(shape,),
            approved_request_identities=approved_request_identities,
        )
    )
    return ledger


def _request(client: ProviderHttpClient) -> None:
    client.request_json(
        "POST",
        "/embeddings",
        payload=_PAYLOAD,
        headers={"Authorization": "Bearer synthetic-secret"},
        provider_id="jina",
        operation="embedding.document",
        model="synthetic-model",
        input_count=1,
        estimated_tokens=6,
    )


def test_retry_reserves_each_http_and_stops_at_persistent_limit(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={})

    client = ProviderHttpClient(
        "https://api.jina.ai/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda _: None,
    )
    try:
        with (
            provider_budget_scope(
                ledger,
                campaign_id="transport-test",
                authorization_id="authorization-test",
                scope="public-only",
                step_id="document",
            ),
            pytest.raises(BudgetBlockedError, match="BLOCKED_BUDGET"),
        ):
            _request(client)
    finally:
        client.close()
    assert len(requests) == 2
    summary = ledger.summary("transport-test")
    assert summary["forwarded"] == 2
    assert summary["locally_blocked"] == 1
    assert summary["unknown_usage_attempts"] == 2
    assert summary["observed_tokens"] is None
    assert [
        row["retry_index"] for row in ledger.attempts("transport-test")
    ] == [0, 1, 2]


def test_acceptance_fault_is_local_and_releases_reservation(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path, request_limit=1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"usage": {"total_tokens": 9}})

    with httpx.Client(
        transport=BudgetedTransport(httpx.MockTransport(handler))
    ) as client:
        with (
            provider_budget_scope(
                ledger,
                campaign_id="transport-test",
                authorization_id="authorization-test",
                scope="public-only",
                step_id="fault",
            ),
            provider_budget_fault(lambda _: True),
            pytest.raises(httpx.ConnectTimeout),
        ):
            client.post("https://api.jina.ai/v1/embeddings", json=_PAYLOAD)
        with provider_budget_scope(
            ledger,
            campaign_id="transport-test",
            authorization_id="authorization-test",
            scope="public-only",
            step_id="recovery",
        ):
            response = client.post(
                "https://api.jina.ai/v1/embeddings", json=_PAYLOAD
            )
    assert response.status_code == 200
    assert len(requests) == 1
    summary = ledger.summary("transport-test")
    assert summary["reserved"] == 1
    assert summary["forwarded"] == 1
    assert summary["locally_blocked"] == 1
    assert summary["observed_tokens"] == 9


def test_approved_text_cannot_change_model_or_disclose_private_text(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    with (
        httpx.Client(
            transport=BudgetedTransport(httpx.MockTransport(handler))
        ) as client,
        provider_budget_scope(
            ledger,
            campaign_id="transport-test",
            authorization_id="authorization-test",
            scope="public-only",
            step_id="document",
        ),
    ):
        client.post(
            "https://api.jina.ai/v1/embeddings",
            json={**_PAYLOAD, "input": ["公开合成文本", "公开合成文本"]},
        )
        for payload in (
            {**_PAYLOAD, "model": "different-model"},
            {**_PAYLOAD, "input": ["private-document-synthetic"]},
        ):
            with pytest.raises(
                BudgetBlockedError, match="PAYLOAD_NOT_APPROVED"
            ):
                client.post("https://api.jina.ai/v1/embeddings", json=payload)
    assert len(requests) == 1
    records = json.dumps(ledger.attempts("transport-test"))
    assert "private-document-synthetic" not in records
    assert "公开合成文本" not in records


def test_background_environment_uses_same_ledger_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path, request_limit=1)
    for name, value in {
        "LEDGER": str(ledger.path),
        "CAMPAIGN_ID": "transport-test",
        "AUTHORIZATION_ID": "authorization-test",
        "SCOPE": "public-only",
    }.items():
        monkeypatch.setenv("RAG_PROVIDER_BUDGET_" + name, value)
    with httpx.Client(
        transport=BudgetedTransport(
            httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        )
    ) as client:
        client.post("https://api.jina.ai/v1/embeddings", json=_PAYLOAD)
        with pytest.raises(BudgetBlockedError, match="BLOCKED_BUDGET"):
            client.post("https://api.jina.ai/v1/embeddings", json=_PAYLOAD)
        monkeypatch.delenv("RAG_PROVIDER_BUDGET_SCOPE")
        with pytest.raises(
            BudgetBlockedError, match="CONFIGURATION_INCOMPLETE"
        ):
            client.post("https://api.jina.ai/v1/embeddings", json=_PAYLOAD)
    assert ledger.summary("transport-test")["forwarded"] == 1


def test_request_shape_binds_instruct_to_approved_policy() -> None:
    original = {
        "model": "synthetic-model",
        "input": {"texts": ["公开合成文本"]},
        "parameters": {
            "instruct": "approved instruction",
            "text_type": "query",
        },
    }
    _, text_hashes, shape = payload_contract(original)
    assert text_hashes == (canonical_sha256("公开合成文本"),)
    changed = {**original, "parameters": {"instruct": "different instruction"}}
    assert payload_contract(changed)[2] != shape


@pytest.mark.parametrize("usage", [True, -1, 2**64, "17"])
def test_invalid_usage_remains_unknown(tmp_path: Path, usage: object) -> None:
    ledger = _ledger(tmp_path)
    with (
        httpx.Client(
            transport=BudgetedTransport(
                httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, json={"usage": {"total_tokens": usage}}
                    )
                )
            )
        ) as client,
        provider_budget_scope(
            ledger,
            campaign_id="transport-test",
            authorization_id="authorization-test",
            scope="public-only",
            step_id="document",
        ),
    ):
        client.post("https://api.jina.ai/v1/embeddings", json=_PAYLOAD)
    assert ledger.summary("transport-test")["observed_tokens"] is None
    assert ledger.summary("transport-test")["unknown_usage_attempts"] == 1


def test_connection_or_credential_rotation_cannot_reuse_old_approval(
    tmp_path: Path,
) -> None:
    endpoint = "https://api.jina.ai/v1/embeddings"
    identity: dict[str, object] = {
        "connection_id": "connection-synthetic",
        "configuration_version": 1,
        "credential_key_version": 1,
    }
    ledger = _ledger(
        tmp_path,
        approved_request_identities=(
            provider_request_identity(endpoint, "synthetic-model", identity),
        ),
    )
    with (
        httpx.Client(
            transport=BudgetedTransport(
                httpx.MockTransport(lambda _: httpx.Response(200, json={})),
                identity=lambda: identity,
            )
        ) as client,
        provider_budget_scope(
            ledger,
            campaign_id="transport-test",
            authorization_id="authorization-test",
            scope="public-only",
            step_id="document",
        ),
    ):
        client.post(endpoint, json=_PAYLOAD)
        identity["credential_key_version"] = 2
        with pytest.raises(
            BudgetBlockedError, match="REQUEST_IDENTITY_NOT_APPROVED"
        ):
            client.post(endpoint, json=_PAYLOAD)
    assert ledger.summary("transport-test")["forwarded"] == 1
    assert ledger.summary("transport-test")["locally_blocked"] == 1
