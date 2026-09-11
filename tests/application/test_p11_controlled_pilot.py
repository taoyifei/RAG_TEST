"""不读取既有 holdout 的独立合成校准准入回归。"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from evaluation import p11_pilot_runtime
from evaluation.p11_pilot_data import PilotContent, PilotDataset, PilotDocument
from evaluation.p11_pilot_runtime import (
    _PilotInventory,
    _PilotRuntime,
    _prepare_corpus,
)
from evaluation.v2.models import (
    DatasetDocument,
    DatasetManifest,
    FixtureVersion,
)
from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.application.answering.service import ExtractiveAnsweringService
from rag_app.application.retrieval import QueryAnalyzer
from rag_app.application.retrieval.confidence import ConfidenceEvaluator
from rag_app.application.retrieval.evidence import EvidenceAssembler
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    EvidenceSelectionContext,
    KnowledgeBaseScope,
    QueryKind,
    RankedChunk,
    RetrievalPolicy,
    SearchRequest,
)
from rag_app.product.live_acceptance import AcceptanceState
from rag_app.product.models import RetrievalProfileRevision
from rag_app.product.provider_runtime import ProviderRuntimeRegistry
from tests.application.retrieval.helpers import make_ranked_chunk
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


@dataclass(frozen=True)
class _Scenario:
    harness: ProductHarness
    context: _PilotRuntime
    project_id: str
    profile: RetrievalProfileRevision


def _new_dataset() -> PilotDataset:
    """只用新合成文档做入口组合验证，不伪造质量样本或通过报告。"""
    document = DatasetDocument(
        document_id=deterministic_id("doc", "calibration-regression"),
        project_id=deterministic_id("prj", "calibration-regression"),
        knowledge_base_id=deterministic_id("kb", "calibration-regression"),
        family_group_id="grp_calibration_regression",
        coverage_tags=("calibration_regression",),
        versions=(
            FixtureVersion(
                fixture_id="calibration-regression",
                display_name="实验设备.docx",
            ),
        ),
    )
    manifest = DatasetManifest(
        schema_version="3",
        dataset_id="calibration-regression",
        dataset_version="1.0.0",
        description="独立入口回归，非质量集",
        split_algorithm="no-evaluation-cases",
        content_classification="synthetic_public",
        documents=(document,),
    )
    return PilotDataset(
        manifest=manifest,
        cases=(),
        documents=(
            PilotDocument(
                document,
                (
                    PilotContent(
                        paragraphs=("环境温度低于零度时应先预热实验设备。",),
                        tables=(),
                    ),
                ),
            ),
        ),
        dataset_sha256=canonical_sha256("calibration-regression-v1"),
    )


@pytest.fixture
def scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Scenario]:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-only")
    harness = build_product_harness(tmp_path)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        validate_five_operations(harness, jina, aliyun)
        source_id = activate_hot_standby_profile(harness, kb, jina, aliyun)
        context = _PilotRuntime(
            harness.runtime,
            {
                "source_profile_revision_id": source_id,
                "ledger_path": str(tmp_path / "ledger.sqlite3"),
            },
            AcceptanceState(tmp_path / "acceptance.sqlite3", "synthetic-only"),
            _new_dataset(),
        )
        documents, profiles = _prepare_corpus(context)
        yield _Scenario(
            harness,
            context,
            documents[0].project_id,
            harness.runtime.control.get_profile(profiles[0]),
        )
    finally:
        harness.close()


def _scope(scenario: _Scenario) -> AbstractContextManager[None]:
    return scenario.harness.runtime.profiles._controlled_pilot(
        source_profile_id=str(
            scenario.context.config["source_profile_revision_id"]
        ),
        project_id=scenario.project_id,
        profile_ids=(scenario.profile.profile_revision_id,),
    )


def _candidate() -> RankedChunk:
    return make_ranked_chunk(
        1,
        "环境温度低于零度时应先预热实验设备。",
        channel="dense:primary",
    ).model_copy(update={"rerank_rank": 1, "rerank_score": 0.95})


def _answer(
    scenario: _Scenario,
    policy: RetrievalPolicy,
    vector: str | None,
    *,
    candidates: tuple[RankedChunk, ...] | None = None,
) -> str | None:
    scope = KnowledgeBaseScope(
        project_id=scenario.project_id,
        knowledge_base_id=scenario.profile.knowledge_base_id,
    )
    analysis = QueryAnalyzer().analyze(
        SearchRequest(scope=scope, text="低温时如何保护设备")
    )
    candidates = (_candidate(),) if candidates is None else candidates
    evidence = EvidenceAssembler().assemble(
        candidates,
        policy,
        context=EvidenceSelectionContext(
            analysis=analysis,
            query_kind=QueryKind.SIMPLE_FACT,
            rerank_mode="provider",
            selected_slot="primary",
        ),
    )
    decision = ConfidenceEvaluator().evaluate(
        analysis,
        QueryKind.SIMPLE_FACT,
        candidates,
        evidence,
        (),
        policy=policy,
        rerank_mode="provider",
        selected_vector_space=vector,
    )
    persistence = scenario.harness.runtime.p09.retrieval_runtime.persistence
    generator = persistence.components.generator
    return ExtractiveAnsweringService(generator).answer(
        "低温时如何保护设备",
        evidence,
        decision,
    )


def test_scoped_calibration_allows_citation_without_marking_quality_pass(
    scenario: _Scenario,
) -> None:
    runtime = scenario.harness.runtime
    resolver = runtime.profiles
    ordinary = resolver._resolve(scenario.profile)
    assert _answer(scenario, ordinary.retrieval._policy, None) is None
    with _scope(scenario):
        controlled = resolver._resolve(scenario.profile)
        policy = controlled.retrieval._policy
        assert policy.dense_semantic_calibration_state == "CONTROLLED_TEST_ONLY"
        assert len(policy.dense_calibrated_vector_spaces) == 2
        assert _answer(
            scenario, policy, policy.dense_calibrated_vector_spaces[0]
        )
        assert (
            _answer(
                scenario, policy, "primary:another-provider:model:1024:l2-v1:1"
            )
            is None
        )
        assert (
            runtime.control.quality.states(scenario.profile.profile_revision_id)
            == {}
        )
        assert runtime.sdk.health().remote_dense_confidence_calibrated is False
    assert resolver._resolve(scenario.profile) is ordinary
    assert ordinary.retrieval._policy.dense_semantic_enabled is False
    assert (
        runtime.control.quality.calibrated_spaces(
            scenario.profile.profile_revision_id
        )
        == ()
    )


def test_scope_does_not_leak_to_source_profile_or_other_thread(
    scenario: _Scenario,
) -> None:
    resolver = scenario.harness.runtime.profiles
    source = scenario.harness.runtime.control.get_profile(
        str(scenario.context.config["source_profile_revision_id"]),
    )
    with _scope(scenario):
        assert resolver.serving_contract(scenario.profile)[
            0
        ].dense_semantic_enabled
        assert not resolver.serving_contract(source)[0].dense_semantic_enabled
        with ThreadPoolExecutor(max_workers=1) as pool:
            other = pool.submit(
                resolver.serving_contract, scenario.profile
            ).result()
        assert not other[0].dense_semantic_enabled


def test_exception_and_connection_drift_remove_controlled_admission(
    scenario: _Scenario,
) -> None:
    runtime = scenario.harness.runtime
    with (
        pytest.raises(RuntimeError, match="SYNTHETIC_QUERY_STOP"),
        _scope(scenario),
    ):
        assert runtime.profiles.serving_contract(scenario.profile)[
            0
        ].dense_semantic_enabled
        raise RuntimeError("SYNTHETIC_QUERY_STOP")
    assert not runtime.profiles.serving_contract(scenario.profile)[
        0
    ].dense_semantic_enabled
    with _scope(scenario):
        connection = runtime.control.get_connection(
            scenario.profile.primary_connection_id
        )
        runtime.credentials.rotate(
            connection.credential_id, "synthetic-new-credential"
        )
        with pytest.raises(ValueError, match="PILOT_SCOPE_CHANGED"):
            runtime.profiles.serving_contract(scenario.profile)
    assert not runtime.profiles.serving_contract(scenario.profile)[
        0
    ].dense_semantic_enabled


def test_pilot_query_entry_receives_scoped_policy_and_restores_on_failure(
    scenario: _Scenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只探测入口策略便停止，不运行样本查询或把 Mock 当成 Live 质量。"""
    runtime = scenario.harness.runtime
    monkeypatch.setattr(
        p11_pilot_runtime,
        "load_pilot_dataset",
        lambda: scenario.context.dataset,
    )
    monkeypatch.setattr(
        ProviderRuntimeRegistry,
        "test_only_transport",
        property(lambda _: False),
    )

    def inspect_query_policy(*args: object) -> None:
        inventory = cast(_PilotInventory, args[2])
        target = runtime.control.active_profile(
            inventory.documents[0].knowledge_base_id
        )
        assert target is not None
        policy = runtime.profiles.serving_contract(target)[0]
        assert policy.dense_semantic_calibration_state == "CONTROLLED_TEST_ONLY"
        raise RuntimeError("SYNTHETIC_QUERY_STOP")

    monkeypatch.setattr(p11_pilot_runtime, "_query_cases", inspect_query_policy)
    with runtime.connections.transaction() as connection:
        session_id = str(
            connection.execute(
                "SELECT session_id FROM console_sessions "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        )
    with pytest.raises(RuntimeError, match="SYNTHETIC_QUERY_STOP"):
        p11_pilot_runtime.run_pilot(
            runtime,
            {
                **scenario.context.config,
                "pilot_retrieval_authorization": {
                    "expires_at": (
                        datetime.now(UTC) + timedelta(hours=1)
                    ).isoformat(),
                    "request_limit": 500,
                    "estimated_token_limit": 1_000_000,
                    "operation_request_limits": {
                        "embedding.document": 100,
                        "embedding.query": 200,
                        "reranking": 200,
                    },
                },
                "pilot_admin_session_id": session_id,
            },
            AcceptanceState(
                tmp_path / "scoped-policy.sqlite3", "scoped-policy"
            ),
            lambda *_: pytest.fail("本回归不能执行真实样本查询"),
            {
                "request_limit": 1,
                "reserved": 0,
                "estimated_token_limit": 1,
                "estimated_input_tokens": 0,
            },
        )
    assert not runtime.profiles.serving_contract(scenario.profile)[
        0
    ].dense_semantic_enabled
    assert (
        runtime.control.quality.states(scenario.profile.profile_revision_id)
        == {}
    )


def test_pilot_preflight_includes_locally_blocked_standby_retries(
    scenario: _Scenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query operation 预算不足时必须在建 Pilot 语料前阻断。"""
    runtime = scenario.harness.runtime
    projects_before = runtime.sdk.list_projects()
    monkeypatch.setattr(
        ProviderRuntimeRegistry,
        "test_only_transport",
        property(lambda _: False),
    )
    result = p11_pilot_runtime.run_pilot(
        runtime,
        {
            **scenario.context.config,
            "max_forwarded_attempts": 3,
            "pilot_retrieval_authorization": {
                "expires_at": (
                    datetime.now(UTC) + timedelta(hours=1)
                ).isoformat(),
                "request_limit": 500,
                "estimated_token_limit": 1_000_000,
                "operation_request_limits": {
                    "embedding.document": 100,
                    "embedding.query": 149,
                    "reranking": 60,
                },
            },
        },
        AcceptanceState(tmp_path / "budget-preflight.sqlite3", "preflight"),
        lambda *_: pytest.fail("预算预检后不能执行查询"),
        {
            "request_limit": 1,
            "reserved": 0,
            "estimated_token_limit": 1,
            "estimated_input_tokens": 0,
        },
    )

    assert result.status == "BLOCKED"
    assert result.reason == "BLOCKED_BUDGET"
    assert result.evidence["minimum_additional"] == {
        "requests": 1,
        "estimated_input_tokens": 0,
        "reranking_requests": 0,
    }
    assert result.evidence["operation_request_lower_bound"] == {
        "embedding.query": 150,
        "reranking": 60,
    }
    assert runtime.sdk.list_projects() == projects_before


def test_pilot_build_uses_explicit_scoped_retrieval_authorization(
    scenario: _Scenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产组合分支只能用独立 KB 清单构建，不复用全局验收活动。"""
    runtime = scenario.harness.runtime
    with runtime.connections.transaction() as connection:
        session_id = str(
            connection.execute(
                "SELECT session_id FROM console_sessions "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        )
    context = _PilotRuntime(
        runtime,
        {
            "source_profile_revision_id": scenario.context.config[
                "source_profile_revision_id"
            ],
            "pilot_admin_session_id": session_id,
            "pilot_retrieval_authorization": {
                "expires_at": (
                    datetime.now(UTC) + timedelta(hours=1)
                ).isoformat(),
                "request_limit": 30,
                "estimated_token_limit": 100_000,
                "operation_request_limits": {
                    "embedding.document": 10,
                    "embedding.query": 10,
                    "reranking": 10,
                },
            },
        },
        AcceptanceState(tmp_path / "authorized.sqlite3", "authorized-pilot"),
        _new_dataset(),
    )
    monkeypatch.setattr(
        ProviderRuntimeRegistry,
        "test_only_transport",
        property(lambda _: False),
    )

    documents, profile_ids = _prepare_corpus(context)

    assert len(documents) == len(profile_ids) == 1
    profile = runtime.control.get_profile(profile_ids[0])
    assert profile.status == "active"
    status = runtime.retrieval_authorizations.status(
        profile.profile_revision_id
    )
    assert status.authorization_state == "APPROVED"
    assert status.manifest is not None
    with runtime.profiles._controlled_pilot(
        source_profile_id=str(context.config["source_profile_revision_id"]),
        project_id=documents[0].project_id,
        profile_ids=(profile.profile_revision_id,),
    ):
        result = runtime.sdk.search(
            documents[0].project_id,
            documents[0].knowledge_base_id,
            "低温时如何保护设备",
            limit=5,
        )
    assert result.selected_embedding_slot == "primary"
    assert result.selected_vector_name == "dense_primary"
    ledger = ProviderBudgetLedger(
        runtime.data_dir / "provider-budget.sqlite3", read_only=True
    )
    attempts = ledger.attempts(status.manifest.budget_campaign_id)
    assert attempts
    assert {item["operation"] for item in attempts} == {
        "embedding.document",
        "embedding.query",
        "reranking",
    }
    assert all(
        item["forwarded"] and item["http_status"] == 200 for item in attempts
    )


def test_controlled_admission_keeps_evidence_rank_and_span_requirements(
    scenario: _Scenario,
) -> None:
    with _scope(scenario):
        policy = scenario.harness.runtime.profiles.serving_contract(
            scenario.profile
        )[0]
        vector = policy.dense_calibrated_vector_spaces[0]
        assert _answer(scenario, policy, vector, candidates=()) is None
        irrelevant = make_ranked_chunk(
            1,
            "前台午间领取快递。",
            channel="dense:primary",
        ).model_copy(update={"rerank_rank": 50, "rerank_score": 0.01})
        assert (
            _answer(scenario, policy, vector, candidates=(irrelevant,)) is None
        )
        candidate = _candidate()
        chunk = candidate.hydrated.chunk
        invalid = chunk.model_copy(
            update={
                "source_spans": tuple(
                    span.model_copy(update={"is_citable": False})
                    for span in chunk.source_spans
                )
            }
        )
        candidate = candidate.model_copy(
            update={
                "hydrated": candidate.hydrated.model_copy(
                    update={"chunk": invalid}
                )
            }
        )
        assert (
            _answer(scenario, policy, vector, candidates=(candidate,)) is None
        )


def test_source_profile_cannot_be_used_as_pilot_target(
    scenario: _Scenario,
) -> None:
    resolver = scenario.harness.runtime.profiles
    source_id = str(scenario.context.config["source_profile_revision_id"])
    with (
        pytest.raises(ValueError, match="PILOT_SCOPE_PROFILE_MISMATCH"),
        resolver._controlled_pilot(
            source_profile_id=source_id,
            project_id=scenario.project_id,
            profile_ids=(source_id,),
        ),
    ):
        pytest.fail("源产品方案不能成为待校准的独立pilot目标")
