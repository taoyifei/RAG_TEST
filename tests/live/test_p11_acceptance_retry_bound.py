"""验收专用真实出站次数限制保留首失败和独立本地阻断记录。"""

from pathlib import Path

import httpx
import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import BudgetedTransport
from rag_app.adapters.providers.http_common import (
    ProviderHttpClient,
    ProviderHttpError,
)
from rag_app.core.identifiers import canonical_sha256
from rag_app.product.live_acceptance import AcceptanceState, StepResult
from rag_app.product.live_acceptance_backend import ProductAcceptanceBackend
from tests.live.test_p11_acceptance_backend import (
    configured_product,  # noqa: F401
)
from tests.live.test_p11_acceptance_backend import (
    test_real_product_transport_fault_and_half_open_with_mock_provider as _check_fault_recovery,  # noqa: E501
)
from tests.product_support import ProductHarness


def _backend(tmp_path: Path, limit: object = 1) -> ProductAcceptanceBackend:
    config: dict[str, object] = {
        "data_dir": str(tmp_path),
        "ledger_path": str(tmp_path / "provider-budget.sqlite3"),
        "campaign_id": "synthetic-retry-campaign",
        "authorization_id": "synthetic-retry-authorization",
        "scope": "p11-public-synthetic-v1",
        "candidate_identity": "synthetic-candidate",
    }
    if limit is not None:
        config["max_forwarded_attempts"] = limit
    return ProductAcceptanceBackend(
        config,
        AcceptanceState(tmp_path / "state.sqlite3", str(config["campaign_id"])),
    )


@pytest.mark.parametrize("limit", [0, 4, -1, True, False, "1", 1.0, [], {}])
def test_invalid_forward_limit_is_rejected(tmp_path: Path, limit: object):
    with pytest.raises(ValueError, match="MAX_FORWARDED_ATTEMPTS_INVALID"):
        _backend(tmp_path, limit)


@pytest.mark.parametrize("limit", [1, 2, 3, None])
@pytest.mark.parametrize("failure", ["http_503", "timeout"])
def test_forward_limit_preserves_first_failure_and_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int | None,
    failure: str,
):
    backend = _backend(tmp_path, limit)
    payload = {"model": "jina-embeddings-v5-text-small", "input": ["公开合成"]}
    payload_hash = canonical_sha256(payload)
    ledger = ProviderBudgetLedger(Path(str(backend.config["ledger_path"])))
    campaign = BudgetCampaign(
        campaign_id=str(backend.config["campaign_id"]),
        authorization_id=str(backend.config["authorization_id"]),
        scope="p11-public-synthetic-v1",
        request_limit=12,
        estimated_token_limit=1000,
        approved_payload_hashes=(payload_hash,),
    )
    ledger.create_campaign(campaign)
    ledger.activate_campaign(campaign.campaign_id)
    forwarded_payloads: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        forwarded_payloads.append(request.content)
        if failure == "timeout":
            raise httpx.ConnectTimeout("synthetic-timeout", request=request)
        return httpx.Response(503, json={"error": "synthetic-unavailable"})

    client = ProviderHttpClient(
        "https://api.jina.ai/v1",
        client=httpx.Client(
            transport=BudgetedTransport(
                httpx.MockTransport(handle), ledger_path=ledger.path
            )
        ),
        sleeper=lambda _delay: None,
    )

    def execute(_step: str) -> StepResult:
        with pytest.raises(ProviderHttpError):
            client.request_json(
                "POST",
                "/embeddings",
                payload=payload,
                headers={},
                provider_id="jina-embedding",
                operation="embedding.document",
                model="jina-embeddings-v5-text-small",
                input_count=1,
                estimated_tokens=4,
            )
        return StepResult("FAIL", "SYNTHETIC_PROVIDER_FAILURE")

    monkeypatch.setattr(backend, "_execute_online", execute)
    try:
        result = backend._execute_bound("citation_quality")
        effective_limit = 3 if limit is None else limit
        assert len(forwarded_payloads) == effective_limit
        assert len(set(forwarded_payloads)) == 1
        attempts = ledger.attempts(campaign.campaign_id)
        assert len(attempts) == 3
        assert attempts[0]["forwarded"] == 1
        assert attempts[0]["status"] == (
            "TRANSPORT_ERROR" if failure == "timeout" else "HTTP_ERROR"
        )
        if failure == "http_503":
            assert attempts[0]["http_status"] == 503
        assert {item["payload_identity"] for item in attempts} == {payload_hash}
        assert all(
            item["locally_blocked"] == 1
            and item["forwarded"] == 0
            and item["reserved"] == 0
            for item in attempts[effective_limit:]
        )
        assert result.status == "FAIL"
        assert result.evidence["max_forwarded_attempts"] == effective_limit
        assert result.evidence["new_forwarded_http"] == effective_limit
        assert result.evidence["new_locally_blocked"] == 3 - effective_limit
        assert (
            result.evidence["new_locally_blocked_at_retry_limit"]
            == 3 - effective_limit
        )
        assert (
            ledger.summary(campaign.campaign_id)["reserved"] == effective_limit
        )
        execute("outside_acceptance")
        assert len(forwarded_payloads) == effective_limit + 3
    finally:
        client.close()
        backend.close()


def test_forward_limit_preserves_canary_identities(tmp_path: Path):
    default = _backend(tmp_path / "default", None)
    bounded = _backend(tmp_path / "bounded", 1)
    for step in (
        "config_check",
        "aliyun_document_canary",
        "aliyun_query_canary",
        "jina_connection",
    ):
        assert bounded.identity(step) == default.identity(step)
    assert bounded.identity("citation_quality") != default.identity(
        "citation_quality"
    )


def test_explicit_null_forward_limit_is_rejected(tmp_path: Path):
    backend = _backend(tmp_path)
    config = {**backend.config, "max_forwarded_attempts": None}
    with pytest.raises(ValueError, match="MAX_FORWARDED_ATTEMPTS_INVALID"):
        ProductAcceptanceBackend(config, backend.state)


def test_bounded_fault_and_half_open_keep_original_behavior(
    configured_product: tuple[ProductHarness, dict[str, object]],  # noqa: F811
):
    _, config = configured_product
    config["max_forwarded_attempts"] = 1
    _check_fault_recovery(configured_product)
