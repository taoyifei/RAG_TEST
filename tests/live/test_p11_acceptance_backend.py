"""使用公开合成 Credential 与 Mock 产品验证 Runner 的实际边界。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from time import monotonic
from typing import cast

import pytest

from rag_app.adapters.providers.budget_ledger import (
    BudgetBlockedError,
    BudgetCampaign,
    ProviderBudgetLedger,
)
from rag_app.adapters.providers.budget_transport import (
    estimated_input_tokens,
    payload_contract,
    provider_budget_fault,
    provider_budget_scope,
)
from rag_app.composition.product_runtime import build_product_runtime
from rag_app.core.models import RetrievalPolicy
from rag_app.core.tokenization import estimate_tokens
from rag_app.product.live_acceptance import AcceptanceState, run_acceptance
from rag_app.product.live_acceptance_backend import (
    ProductAcceptanceBackend,
    _p11_limits,
)
from rag_app.product.live_acceptance_payloads import approved_payload_contracts
from rag_app.product.models import ProviderConnection
from rag_app.product.provider_runtime import build_offline_mock_transport
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


@pytest.fixture
def configured_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ProductHarness, dict[str, object]]]:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-only")
    harness = build_product_harness(tmp_path)
    _, kb_id = create_project_and_knowledge_base(harness)
    _, _, jina_id, aliyun_id = create_provider_connections(harness)
    validate_five_operations(harness, jina_id, aliyun_id)
    profile_id = activate_hot_standby_profile(
        harness, kb_id, jina_id, aliyun_id
    )
    ledger_path = harness.runtime.data_dir / "provider-budget.sqlite3"
    ledger = ProviderBudgetLedger(ledger_path)
    ledger.create_campaign(
        BudgetCampaign(
            campaign_id="synthetic-campaign",
            authorization_id="synthetic-auth",
            scope="p11-public-synthetic-v1",
            request_limit=1,
            estimated_token_limit=1000,
            step_request_limits={
                "aliyun_document_canary": 1,
                "aliyun_query_canary": 1,
            },
        )
    )
    config = {
        "data_dir": str(harness.runtime.data_dir),
        "state_path": str(tmp_path / "state.sqlite3"),
        "ledger_path": str(ledger_path),
        "campaign_id": "synthetic-campaign",
        "authorization_id": "synthetic-auth",
        "jina_connection_id": jina_id,
        "aliyun_connection_id": aliyun_id,
        "source_profile_revision_id": profile_id,
        "candidate_identity": "synthetic-candidate",
        "runtime_settings": {"master_key_file": str(tmp_path / "master-key")},
    }
    try:
        yield harness, config
    finally:
        harness.close()


def test_config_check_does_not_open_master_key_or_start_runtime(
    configured_product: tuple[ProductHarness, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
):
    _, config = configured_product

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("本地配置检查不允许启动 Runtime 或读取 Master Key")

    monkeypatch.setattr(
        "rag_app.product.live_acceptance_backend.load_master_key", forbidden
    )
    monkeypatch.setattr(
        "rag_app.product.live_acceptance_backend.build_product_runtime",
        forbidden,
    )
    report = run_acceptance(config, steps=("config_check",))
    diagnosis = report["steps"]["config_check"]
    assert diagnosis["status"] == "BLOCKED"
    assert diagnosis["evidence"]["endpoint_contract"] == "PASS"
    assert diagnosis["evidence"]["connection_configuration"] == "PASS"
    assert diagnosis["evidence"]["campaign_binding"] == "BLOCKED"
    assert report["budget"]["this_run"]["forwarded"] == 0


def test_aliyun_config_does_not_require_jina_profile_or_qdrant(
    configured_product: tuple[ProductHarness, dict[str, object]],
):
    _, config = configured_product
    config.pop("jina_connection_id")
    config.pop("source_profile_revision_id")
    report = run_acceptance(config, steps=("config_check",))
    assert (
        report["steps"]["config_check"]["evidence"]["endpoint_contract"]
        == "PASS"
    )
    assert (
        report["steps"]["config_check"]["reason"] == "CAMPAIGN_BINDING_REQUIRED"
    )


def test_mock_connection_validation_never_becomes_live_success(
    configured_product: tuple[ProductHarness, dict[str, object]],
):
    harness, config = configured_product
    backend = ProductAcceptanceBackend(
        config,
        AcceptanceState(Path(str(config["state_path"])), "synthetic-campaign"),
    )
    backend.provider_registry = harness.runtime.providers
    result = backend._validate("aliyun_connection_id", "embedding.document")
    assert result.status == "FAIL"
    assert result.evidence["validation_mode"] == "mock"


def test_dual_index_uses_its_own_kb_and_actual_inventory(
    configured_product: tuple[ProductHarness, dict[str, object]],
):
    harness, config = configured_product
    state = AcceptanceState(
        Path(str(config["state_path"])), "synthetic-campaign"
    )
    backend = ProductAcceptanceBackend(config, state)
    backend.runtime = harness.runtime
    result = backend._dual_index()
    assert result.status == "PASS", result.evidence
    assert result.evidence["fts_count"] == result.evidence["actual_chunk_count"]
    assert {
        slot["vector_name"] for slot in result.evidence["slot_coverages"]
    } == {"dense_primary", "dense_standby"}
    assert (
        state.resource("knowledge_base_id")
        != harness.runtime.control.get_profile(
            str(config["source_profile_revision_id"])
        ).knowledge_base_id
    )
    rerun = backend._dual_index()
    assert rerun.evidence["revision_id"] == result.evidence["revision_id"]


def test_token_estimator_preserves_chinese_and_rerank_fields():
    payload = {"query": "公开合成查询", "documents": ["甲文档", "乙文档"]}
    assert estimated_input_tokens(payload) == sum(
        estimate_tokens(text)
        for text in [payload["query"], *payload["documents"]]
    )


def test_token_estimator_counts_instruct_once_per_input():
    texts = ["公开文本一", "公开文本二", "公开文本三"]
    instruct = "生成检索向量"
    payload = {"input": {"texts": texts}, "parameters": {"instruct": instruct}}
    assert estimated_input_tokens(payload) == sum(
        estimate_tokens(text) for text in texts
    ) + len(texts) * estimate_tokens(instruct)


def test_bind_campaign_is_offline_and_idempotent(
    configured_product: tuple[ProductHarness, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
):
    _, config = configured_product
    config.update(
        {
            "campaign_id": "bound-synthetic-campaign",
            "authorization_id": "bound-synthetic-authorization",
            "campaign_limits": {
                "request_limit": 25,
                "estimated_token_limit": 1000,
            },
            "maintenance_confirmed": True,
            "bind_campaign": True,
        }
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("本地预算绑定不能解密或初始化产品 Runtime")

    monkeypatch.setattr(
        "rag_app.product.live_acceptance_backend.load_master_key", forbidden
    )
    monkeypatch.setattr(
        "rag_app.product.live_acceptance_backend.build_product_runtime",
        forbidden,
    )
    first = run_acceptance(config, steps=("config_check",))
    assert first["campaign_binding"]["status"] == "PASS", first[
        "campaign_binding"
    ]
    assert first["budget"]["this_run"]["forwarded"] == 0
    second = run_acceptance(config, steps=("config_check",))
    assert second["campaign_binding"]["status"] == "PASS"
    assert second["budget"]["cumulative"] == first["budget"]["cumulative"]


def test_bind_campaign_requires_real_maintenance_confirmation(
    configured_product: tuple[ProductHarness, dict[str, object]],
):
    _, config = configured_product
    config.update(
        {
            "campaign_limits": {
                "request_limit": 25,
                "estimated_token_limit": 1000,
            },
            "bind_campaign": True,
        }
    )
    report = run_acceptance(config, steps=("config_check",))
    assert (
        report["campaign_binding"]["reason"] == "BLOCKED_MAINTENANCE_REQUIRED"
    )
    assert report["steps"]["config_check"]["status"] == "BLOCKED"


def test_real_product_transport_fault_and_half_open_with_mock_provider(
    configured_product: tuple[ProductHarness, dict[str, object]],
):
    harness, config = configured_product
    config.update(
        {
            "campaign_id": "transport-campaign",
            "authorization_id": "transport-authorization",
            "campaign_limits": {
                "request_limit": 25,
                "estimated_token_limit": 1000,
            },
            "maintenance_confirmed": True,
        }
    )
    state = AcceptanceState(
        Path(str(config["state_path"])), "transport-campaign"
    )
    backend = ProductAcceptanceBackend(config, state)
    assert backend.bind_campaign().status == "PASS"
    backend.runtime = build_product_runtime(
        harness.runtime.settings,
        transport_factory=build_offline_mock_transport,
        circuit_factory=backend._circuit,
        recover_jobs=False,
    )
    ledger = ProviderBudgetLedger(Path(str(config["ledger_path"])))
    try:
        for step in (
            "dual_index",
            "primary_query",
            "standby_failover",
            "recovery",
        ):
            with (
                backend._background_budget(step),
                provider_budget_scope(
                    ledger,
                    campaign_id="transport-campaign",
                    authorization_id="transport-authorization",
                    scope="p11-public-synthetic-v1",
                    step_id=step,
                ),
                provider_budget_fault(backend._local_blocker),
            ):
                result = (
                    backend._dual_index()
                    if step == "dual_index"
                    else backend._query(step)
                )
            assert result.status == "PASS", (
                step,
                result.reason,
                result.evidence,
            )
        assert backend.observed_half_open
        attempts = ledger.attempts("transport-campaign")
        assert any(item["locally_blocked"] for item in attempts)
        assert all(
            not item["forwarded"]
            for item in attempts
            if item["locally_blocked"]
        )
    finally:
        backend.close()


@pytest.mark.parametrize(
    "limits",
    [
        {"request_limit": 26, "estimated_token_limit": 1000},
        {"request_limit": 25, "estimated_token_limit": 1001},
        {
            "request_limit": 25,
            "estimated_token_limit": 1000,
            "provider_token_limits": {"jina": 601},
        },
    ],
)
def test_p11_config_cannot_increase_accepted_authorization(
    limits: dict[str, object],
):
    with pytest.raises(
        BudgetBlockedError, match="P11_AUTHORIZATION_LIMIT_EXCEEDED"
    ):
        _p11_limits(limits)


def test_p11_default_provider_caps_remain_six_hundred():
    assert _p11_limits(
        {"request_limit": 25, "estimated_token_limit": 1000}
    ) == {"jina": 600, "aliyun": 600}


def test_queued_acceptance_job_is_resubmitted_on_resume(
    configured_product: tuple[ProductHarness, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
):
    harness, config = configured_product
    state = AcceptanceState(
        Path(str(config["state_path"])), "synthetic-campaign"
    )
    backend = ProductAcceptanceBackend(config, state)
    backend.runtime = harness.runtime
    clock = iter((0.0, 121.0))
    with monkeypatch.context() as paused:
        paused.setattr(harness.runtime.sdk, "_submit_job", lambda _job: None)
        paused.setattr(
            "rag_app.product.live_acceptance_backend.monotonic",
            lambda: next(clock),
        )
        first = backend._dual_index()
    assert first.status == "BLOCKED"
    assert (
        harness.runtime.sdk.get_job(
            str(state.resource("index_job_id"))
        ).state.value
        == "queued"
    )
    monkeypatch.setattr(
        "rag_app.product.live_acceptance_backend.monotonic", monotonic
    )
    second = backend._dual_index()
    assert second.status == "PASS", second.evidence


def test_probe_budget_denial_is_blocked_and_retains_minimum_additional(
    configured_product: tuple[ProductHarness, dict[str, object]],
):
    harness, config = configured_product
    config.update(
        {
            "campaign_id": "probe-budget-campaign",
            "authorization_id": "probe-budget-authorization",
            "campaign_limits": {
                "request_limit": 25,
                "estimated_token_limit": 1000,
            },
            "maintenance_confirmed": True,
        }
    )
    backend = ProductAcceptanceBackend(
        config,
        AcceptanceState(
            Path(str(config["state_path"])), "probe-budget-campaign"
        ),
    )
    assert backend.bind_campaign().status == "PASS"
    backend.provider_registry = harness.runtime.providers
    first = backend.execute("aliyun_document_canary")
    assert first.status == "FAIL", first  # Mock 的成功响应不能充当 Live。
    second = backend.execute("aliyun_document_canary")
    assert second.status == "BLOCKED"
    assert second.reason == "BLOCKED_BUDGET"
    assert second.evidence["minimum_additional"]["step_requests"] == 1
    assert second.evidence["new_forwarded_http"] == 0
    assert first.evidence["new_forwarded_http"] == 1
    assert (
        second.evidence["budget"]["forwarded"]
        == first.evidence["budget"]["forwarded"]
    )


def test_midnight_does_not_change_identity_and_expired_validation_is_not_reused(
    configured_product: tuple[ProductHarness, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
):
    _, config = configured_product
    ledger = ProviderBudgetLedger(Path(str(config["ledger_path"])))
    config.update(campaign_id="ttl-campaign", authorization_id="ttl-auth")
    state = AcceptanceState(Path(str(config["state_path"])), "ttl-campaign")
    backend = ProductAcceptanceBackend(config, state)
    finished = datetime(2026, 9, 5, 23, 55, tzinfo=UTC)

    class Clock(datetime):
        current = finished

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            """固定时钟只用于合成证据 TTL 回归。"""
            return cls.current.astimezone(tz)

    monkeypatch.setattr(
        "rag_app.product.live_acceptance_backend.datetime", Clock
    )
    monkeypatch.setattr("rag_app.product.verification.datetime", Clock)
    connection_id = str(config["aliyun_connection_id"])
    control = backend.control
    assert control is not None
    original = next(
        item
        for item in control.list_validations(connection_id)
        if item.operation == "embedding.document"
    )
    ledger.create_campaign(
        BudgetCampaign(
            campaign_id="ttl-campaign",
            authorization_id="ttl-auth",
            scope="p11-public-synthetic-v1",
            request_limit=1,
            estimated_token_limit=1000,
            approved_payload_hashes=(original.synthetic_payload_hash,),
        )
    )
    ledger.activate_campaign("ttl-campaign")
    simulated = original.model_copy(
        update={
            "finished_at": finished.isoformat(),
            "status": "succeeded",
            "validation_mode": "live",
            "http_category": "live_200",
            "request_dispatched": True,
            "http_status": 200,
        }
    )
    monkeypatch.setattr(control, "list_validations", lambda _key: (simulated,))
    record: dict[str, object] = {
        "status": "PASS",
        "timestamp": finished.isoformat(),
        "evidence": {
            "validation_id": original.validation_id,
            "authorization": {
                "campaign_id": config["campaign_id"],
                "authorization_id": config["authorization_id"],
                "scope": "p11-public-synthetic-v1",
            },
        },
    }
    before = backend.identity("aliyun_document_canary")
    Clock.current = finished + timedelta(minutes=10)
    assert backend.identity("aliyun_document_canary") == before
    assert backend.evidence_is_current("aliyun_document_canary", record)
    Clock.current = finished + timedelta(hours=24, seconds=1)
    assert not backend.evidence_is_current("aliyun_document_canary", record)
    # 刷新调度时间也不能使实际已经过期的 Provider 验证变新。
    record["timestamp"] = Clock.current.isoformat()
    assert not backend.evidence_is_current("aliyun_document_canary", record)
    Clock.current = finished + timedelta(minutes=10)
    cast(dict[str, object], record["evidence"])["authorization"] = {
        "campaign_id": "another-campaign"
    }
    assert not backend.evidence_is_current("aliyun_document_canary", record)


def test_another_provider_configuration_does_not_change_jina_identity(
    configured_product: tuple[ProductHarness, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
):
    _, config = configured_product
    backend = ProductAcceptanceBackend(
        config,
        AcceptanceState(Path(str(config["state_path"])), "synthetic-campaign"),
    )
    before = backend.identity("jina_connection")
    control = backend.control
    assert control is not None
    get_connection = control.get_connection

    def changed(key: str) -> ProviderConnection:
        connection = get_connection(key)
        return (
            connection.model_copy(update={"configuration_version": 99})
            if (key == config["aliyun_connection_id"])
            else connection
        )

    monkeypatch.setattr(control, "get_connection", changed)
    assert backend.identity("jina_connection") == before


def test_approved_rerank_shapes_cover_actual_policy_candidate_limit(
    configured_product: tuple[ProductHarness, dict[str, object]],
):
    _, config = configured_product
    backend = ProductAcceptanceBackend(
        config,
        AcceptanceState(Path(str(config["state_path"])), "synthetic-campaign"),
    )
    assert backend.control is not None
    connection = backend.control.get_connection(
        str(config["jina_connection_id"])
    )
    policy = RetrievalPolicy()
    assert policy.rerank_candidate_limit > 5
    contracts = approved_payload_contracts(
        (connection,),
        (backend._spec("jina_connection_id"),),
        policy,
    )
    _, _, shape = payload_contract(
        {
            "model": "jina-reranker-v3.5",
            "query": "公开合成查询",
            "documents": ["公开合成候选"] * policy.rerank_candidate_limit,
            "top_n": policy.rerank_candidate_limit,
            "return_documents": False,
        }
    )
    assert shape in contracts["approved_request_shape_hashes"]


def test_unscoped_jina_record_is_not_relabelled_as_current_authorization(
    configured_product: tuple[ProductHarness, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
):
    harness, config = configured_product
    backend = ProductAcceptanceBackend(
        config,
        AcceptanceState(Path(str(config["state_path"])), "synthetic-campaign"),
    )
    backend.provider_registry = harness.runtime.providers
    control = backend.control
    assert control is not None
    runs = control.list_validations(str(config["jina_connection_id"]))
    simulated = tuple(
        item.model_copy(
            update={
                "validation_mode": "live",
                "http_category": "live_200",
                "dimension": 64,
                "synthetic_payload_hash": "0" * 64,
            }
        )
        for item in runs
    )
    monkeypatch.setattr(control, "list_validations", lambda _key: simulated)
    result = backend._validate("jina_connection_id", "embedding.document")
    assert result.status == "FAIL"
    assert result.reason != "CURRENT_LIVE_VALIDATION_REUSED"
    assert result.evidence["validation_mode"] == "mock"
    assert "endpoint_host" not in result.evidence
