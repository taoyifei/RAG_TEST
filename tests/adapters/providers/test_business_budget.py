"""业务知识库授权的发送边界回归，全部使用隔离账本和离线响应。"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from rag_app.adapters.providers.aliyun_ocr import synthetic_ocr_payload
from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    BudgetRequest,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_models import campaign_configuration
from rag_app.adapters.providers.budget_revision import (
    budget_payload_set_identity,
)
from rag_app.adapters.providers.budget_transport import (
    BudgetedTransport,
    _response_observation,
    provider_budget_scope,
    provider_data_scope,
    provider_request_identity,
)
from rag_app.core.identifiers import canonical_sha256

_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_SOURCE = "1" * 64
_IDENTITY = {
    "connection_id": "conn_test",
    "configuration_version": 3,
    "credential_key_version": 1,
}
_REQUEST = provider_request_identity(_URL, "qwen3.7-flash", _IDENTITY)


def _campaign(**changes: object) -> BudgetCampaign:
    return replace(
        BudgetCampaign(
            campaign_id="kb-test",
            authorization_id="approved-test",
            scope="kb-scope",
            request_limit=3,
            estimated_token_limit=9000,
            scope_mode="knowledge_base",
            project_id="project-1",
            knowledge_base_id="kb-1",
            approved_source_hashes=(_SOURCE,),
            approved_media_hashes=("2" * 64,),
            approved_request_identities=(_REQUEST,),
            allowed_models=("qwen3.7-flash", "qwen3.5-ocr"),
            allowed_operations=("generation", "query.rewrite", "image.ocr"),
            operation_request_limits={
                "generation": 2,
                "query.rewrite": 1,
                "image.ocr": 1,
            },
            expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        ),
        **changes,
    )


def _request(**changes: object) -> BudgetRequest:
    return replace(
        BudgetRequest(
            provider="aliyun",
            operation="generation",
            request_identity=_REQUEST,
            payload_identity="3" * 64,
            estimated_input_tokens=50,
            estimated_output_tokens=100,
            model="qwen3.7-flash",
            project_id="project-1",
            knowledge_base_id="kb-1",
            source_hashes=(_SOURCE,),
            provenance_verified=True,
        ),
        **changes,
    )


def _reserve(
    ledger: ProviderBudgetLedger, request: BudgetRequest, step: str = "test"
) -> str:
    return ledger.reserve(
        "kb-test",
        authorization_id="approved-test",
        scope="kb-scope",
        step_id=step,
        request=request,
    )


def test_old_campaign_serialization_and_approval_checksum_are_unchanged():
    old = BudgetCampaign(
        campaign_id="p11-test",
        authorization_id="p11-approved",
        scope="public-only",
        request_limit=1,
        estimated_token_limit=10,
        approved_payload_hashes=("4" * 64,),
    )
    original = {
        k: v
        for k, v in asdict(old).items()
        if k
        in {
            "campaign_id",
            "authorization_id",
            "scope",
            "request_limit",
            "estimated_token_limit",
            "approved_payload_hashes",
            "approved_text_hashes",
            "approved_request_shape_hashes",
            "approved_request_identities",
            "provider_request_limits",
            "provider_token_limits",
            "step_request_limits",
        }
    }
    assert campaign_configuration(old) == original
    assert budget_payload_set_identity(old) == canonical_sha256(
        {k: sorted(v) for k, v in original.items() if k.startswith("approved_")}
    )
    business = _campaign()
    assert budget_payload_set_identity(business) != budget_payload_set_identity(
        replace(business, knowledge_base_id="kb-2")
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"knowledge_base_id": "kb-2"}, "DATA_SCOPE_MISMATCH"),
        ({"provenance_verified": False}, "DATA_SCOPE_MISMATCH"),
        ({"source_hashes": ()}, "SOURCE_NOT_APPROVED"),
        ({"source_hashes": ("9" * 64,)}, "SOURCE_NOT_APPROVED"),
        ({"model": "other-model"}, "MODEL_OPERATION_NOT_APPROVED"),
        ({"policy_valid": False}, "POLICY_MISMATCH"),
        ({"request_identity": "9" * 64}, "IDENTITY_NOT_APPROVED"),
        ({"media_hashes": ("2" * 64,)}, "UNEXPECTED_IMAGE"),
        (
            {"operation": "image.ocr", "media_hashes": ("9" * 64,)},
            "MEDIA_NOT_APPROVED",
        ),
    ],
)
def test_business_scope_rejects_unapproved_data_before_reservation(
    tmp_path: Path, changes: dict[str, object], reason: str
):
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    ledger.create_campaign(_campaign())
    with pytest.raises(BudgetBlockedError, match=reason):
        _reserve(ledger, _request(**changes))
    assert ledger.summary("kb-test")["reserved"] == 0


def test_rewrite_allows_question_without_source_and_expiry_blocks(
    tmp_path: Path,
):
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    campaign = _campaign()
    ledger.create_campaign(campaign)
    _reserve(ledger, _request(operation="query.rewrite", source_hashes=()))
    expired = replace(
        campaign,
        campaign_id="expired",
        authorization_id="expired-auth",
        expires_at="2020-01-01T00:00:00+00:00",
    )
    ledger.create_campaign(expired)
    with pytest.raises(BudgetBlockedError, match="EXPIRED"):
        ledger.reserve(
            "expired",
            authorization_id="expired-auth",
            scope="kb-scope",
            step_id="test",
            request=_request(),
        )


def test_unknown_usage_combined_reservation_survives_restart_and_concurrency(
    tmp_path: Path,
):
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    ledger.create_campaign(
        _campaign(request_limit=1, estimated_token_limit=150)
    )

    def reserve_once(_: int) -> str | None:
        try:
            return _reserve(ProviderBudgetLedger(ledger.path), _request())
        except BudgetBlockedError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        attempts = list(pool.map(reserve_once, range(4)))
    assert sum(item is not None for item in attempts) == 1
    attempt = next(item for item in attempts if item is not None)
    ledger.mark_forwarded(attempt)
    ledger.finish(attempt, status="TRANSPORT_ERROR")
    summary = ProviderBudgetLedger(ledger.path).summary("kb-test")
    assert summary["reserved_total_tokens"] == 150
    assert summary["estimated_output_tokens"] == 100
    assert summary["unknown_usage_attempts"] == 1
    assert summary["observed_tokens"] is None


def test_output_and_operation_limits_cannot_be_bypassed_by_new_step(
    tmp_path: Path,
):
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    ledger.create_campaign(_campaign(estimated_token_limit=299))
    _reserve(ledger, _request())
    with pytest.raises(BudgetBlockedError, match="BLOCKED_BUDGET"):
        _reserve(ledger, _request(), "second")
    second = ProviderBudgetLedger(tmp_path / "second.sqlite3")
    second.create_campaign(_campaign())
    _reserve(second, _request(), "first")
    _reserve(second, _request(), "second")
    with pytest.raises(BudgetBlockedError, match="BLOCKED_BUDGET"):
        _reserve(second, _request(), "third")


def test_scoped_new_questions_do_not_replace_old_active_campaign(
    tmp_path: Path,
):
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    old = BudgetCampaign(
        campaign_id="p11",
        authorization_id="p11-auth",
        scope="public-only",
        request_limit=1,
        estimated_token_limit=1,
    )
    ledger.create_campaign(old)
    ledger.activate_campaign("p11")
    ledger.create_campaign(_campaign())
    before = ledger.authorization_snapshot("p11")
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    with (
        httpx.Client(
            transport=BudgetedTransport(
                httpx.MockTransport(handler),
                ledger_path=ledger.path,
                identity=_IDENTITY,
            )
        ) as client,
        provider_budget_scope(
            ledger,
            campaign_id="kb-test",
            authorization_id="approved-test",
            scope="kb-scope",
            step_id="generation",
        ),
    ):
        for question in ("允许的新问题一", "允许的新问题二"):
            with provider_data_scope(
                project_id="project-1",
                knowledge_base_id="kb-1",
                source_hashes=(_SOURCE,),
            ):
                client.post(
                    _URL,
                    json={
                        "model": "qwen3.7-flash",
                        "messages": [{"role": "user", "content": question}],
                        "stream": False,
                        "enable_thinking": False,
                        "max_tokens": 1536,
                    },
                    extensions={"rag_chat_operation": "generation"},
                )
    assert len(sent) == 2
    assert ProviderBudgetLedger(ledger.path).active_campaign() == old
    assert ledger.authorization_snapshot("p11") == before
    with sqlite3.connect(ledger.path) as connection:
        configurations = connection.execute(
            "SELECT configuration FROM provider_budget_campaigns"
        ).fetchall()
        assert all("允许的新问题" not in item[0] for item in configurations)


def test_real_transport_without_authorization_is_blocked_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def no_network(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("不应进入真实网络")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    with (
        httpx.Client(
            transport=BudgetedTransport(
                httpx.HTTPTransport(), ledger_path=tmp_path / "missing.sqlite3"
            )
        ) as client,
        pytest.raises(BudgetBlockedError, match="CHAT_AUTHORIZATION_REQUIRED"),
    ):
        client.post(_URL, json={"model": "qwen3.7-flash"})


@pytest.mark.parametrize(
    "mutation", [None, "tools", "image", "pixels", "thinking"]
)
def test_ocr_transport_hashes_media_and_reserves_image_and_output(
    tmp_path: Path,
    mutation: str | None,
):
    payload = synthetic_ocr_payload("qwen3.5-ocr")
    image = payload["messages"][0]["content"][0]
    digest = hashlib.sha256(
        base64.b64decode(image["image_url"]["url"].split(",")[1])
    ).hexdigest()
    ledger = ProviderBudgetLedger(tmp_path / "budget.sqlite3")
    ledger.create_campaign(
        _campaign(
            approved_media_hashes=(digest,),
            approved_request_identities=(
                provider_request_identity(_URL, "qwen3.5-ocr", _IDENTITY),
            ),
        )
    )
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    body = deepcopy(payload)
    if mutation == "tools":
        body["tools"] = []
    elif mutation == "thinking":
        body["enable_thinking"] = True
    elif mutation == "pixels":
        body["messages"][0]["content"][0]["max_pixels"] = 20_000_000
    elif mutation == "image":
        body["messages"][0]["content"][0]["image_url"]["url"] = (
            "https://example.com/private.png"
        )
    with (
        httpx.Client(
            transport=BudgetedTransport(
                httpx.MockTransport(handler),
                ledger_path=ledger.path,
                identity=_IDENTITY,
            )
        ) as client,
        provider_budget_scope(
            ledger,
            campaign_id="kb-test",
            authorization_id="approved-test",
            scope="kb-scope",
            step_id="ocr",
        ),
        provider_data_scope(
            project_id="project-1",
            knowledge_base_id="kb-1",
            source_hashes=(_SOURCE,),
            media_hashes=(digest,),
        ),
    ):
        if mutation:
            with pytest.raises(BudgetBlockedError):
                client.post(
                    _URL,
                    json=body,
                    extensions={"rag_chat_operation": "image.ocr"},
                )
            assert not sent
        else:
            client.post(
                _URL, json=body, extensions={"rag_chat_operation": "image.ocr"}
            )
            summary = ledger.summary("kb-test")
            assert summary["estimated_image_tokens"] == 2048
            assert summary["estimated_output_tokens"] == 256
            assert summary["reserved_total_tokens"] > 2048 + 256
            assert summary["observed_tokens"] is None


def test_chat_partial_input_usage_does_not_claim_known_total():
    response = httpx.Response(200, json={"usage": {"prompt_tokens": 37}})
    assert _response_observation(response, chat=True)[0] is None
    assert _response_observation(response, chat=False)[0] == 37
