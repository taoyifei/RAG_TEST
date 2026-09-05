"""独立 pilot 的预算前置、真实排名观测与质量状态回归。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from evaluation import p11_pilot_runtime
from evaluation.p11_pilot import PilotLiveEvidence, evaluate_pilot
from evaluation.p11_pilot_data import approved_pilot_texts, load_pilot_dataset
from evaluation.p11_pilot_runtime import (
    _active_inventory,
    _pilot_prefix,
    _PilotInventory,
    _PilotRuntime,
    _prepare_corpus,
    _query_cases,
    _remap_cases,
    query_budget_lower_bound,
    run_pilot,
)
from evaluation.v2.observations import ObservationContext, observe_case_result
from evaluation.v2.runtime import _effective_cases
from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.composition.product_runtime import (
    _product_status,
    build_product_runtime,
)
from rag_app.core.errors import Conflict
from rag_app.core.models import SearchAnswerResult
from rag_app.product.live_acceptance import AcceptanceState
from rag_app.product.models import RetrievalProfileDraft
from rag_app.product.provider_runtime import build_offline_mock_transport
from rag_app.product.quality import QualityValidationRecord
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


@pytest.fixture
def configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ProductHarness, str]]:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        _, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        validate_five_operations(harness, jina, aliyun)
        profile_id = activate_hot_standby_profile(harness, kb, jina, aliyun)
        yield harness, profile_id
    finally:
        harness.close()


def test_pilot_labels_are_independent_and_keep_accepted_sample_sizes() -> None:
    dataset = load_pilot_dataset()
    assert len(dataset.cases) == 30
    assert sum(case.expected.answerable for case in dataset.cases) == 20
    assert all(case.split == "holdout" for case in dataset.cases)
    assert all("青岛啤酒" not in case.query for case in dataset.cases)
    assert all(not case.expected.required_identifiers for case in dataset.cases)
    assert {case.category for case in dataset.cases} >= {
        "identifier_free_fact",
        "semantic_paraphrase",
        "cjk_noise",
        "table_structure",
        "negative_refusal",
        "revision_isolation",
        "scope_isolation",
    }
    assert any(len(item.versions) == 2 for item in dataset.documents)
    assert all(item.content() == item.content() for item in dataset.documents)
    lower = query_budget_lower_bound(dataset, "查询指令")
    assert lower["requests"] == 60
    assert lower["estimated_input_tokens"] > 0


def test_pilot_budget_block_precedes_indexing_and_provider_calls(
    configured: tuple[ProductHarness, str],
    tmp_path: Path,
) -> None:
    harness, profile_id = configured
    called = False

    def forbidden_search(
        project: str,
        kb: str,
        query: str,
        lane: str,
    ) -> SearchAnswerResult:
        nonlocal called
        called = True
        raise AssertionError((project, kb, query, lane))

    state = AcceptanceState(tmp_path / "acceptance.sqlite3", "synthetic-pilot")
    projects_before = harness.runtime.sdk.list_projects()
    result = run_pilot(
        harness.runtime,
        {"source_profile_revision_id": profile_id},
        state,
        forbidden_search,
        {
            "request_limit": 25,
            "reserved": 6,
            "estimated_token_limit": 1000,
            "estimated_input_tokens": 157,
        },
    )
    assert result.status == "BLOCKED"
    assert result.reason == "BLOCKED_BUDGET"
    additional = cast(dict[str, int], result.evidence["minimum_additional"])
    assert additional["requests"] == 41
    assert called is False
    assert harness.runtime.sdk.list_projects() == projects_before
    assert harness.runtime.control.quality.states(profile_id) == {}
    assert (
        harness.runtime.sdk.health().remote_dense_confidence_calibrated is False
    )


def test_pilot_canonical_labels_and_actual_v3_diagnostics(
    configured: tuple[ProductHarness, str],
    tmp_path: Path,
) -> None:
    harness, profile_id = configured
    dataset = load_pilot_dataset()
    context = _PilotRuntime(
        harness.runtime,
        {"source_profile_revision_id": profile_id},
        AcceptanceState(tmp_path / "pilot.sqlite3", "synthetic-offline-pilot"),
        dataset,
    )
    documents, _ = _prepare_corpus(context)
    chunks, revisions = _active_inventory(harness.runtime, documents)
    approved_texts = approved_pilot_texts()
    assert {chunk.embedding_text for chunk in chunks.values()} <= set(
        approved_texts
    )
    cases = _effective_cases(
        _remap_cases(dataset, documents), chunks, require_fixed_labels=False
    )
    assert all(
        item.node_id and item.source_start_char is not None
        for case in cases
        for item in case.expected.required_source_ranges
    )
    assert not any(
        "每份八元" in chunk.citation_text for chunk in chunks.values()
    )
    observations = []
    checked_evidence_separation = False
    for case in cases:
        result = harness.runtime.sdk.search(
            case.project_id, case.knowledge_base_id, case.query, limit=10
        )
        observation_context = ObservationContext(
            chunks=chunks,
            documents=documents,
            expected_revision=revisions[case.knowledge_base_id],
            expected_vectors=(
                ("primary", "dense_primary"),
                ("standby", "dense_standby"),
            ),
            variant_id="p11-fixed-profile",
            lane="offline-structural",
            evidence_token_budget=4096,
        )
        observed = observe_case_result(
            case,
            result,
            observation_context,
            latency_ms=0.0,
        )
        assert result.diagnostics is not None
        assert observed.fused_chunk_ids == result.diagnostics.fused_chunk_ids
        assert observed.retrieved_chunk_ids == tuple(
            item.chunk_id for item in result.diagnostics.reranked
        )
        assert observed.evidence_chunk_ids == tuple(
            item.chunk_id for item in result.evidence
        )
        if result.evidence and not checked_evidence_separation:
            without_ranks = result.model_copy(
                update={
                    "diagnostics": result.diagnostics.model_copy(
                        update={
                            "fused_chunk_ids": (),
                            "reranked": (),
                        }
                    )
                }
            )
            separated = observe_case_result(
                case, without_ranks, observation_context, latency_ms=0.0
            )
            assert separated.retrieved_chunk_ids == ()
            assert separated.evidence_chunk_ids
            checked_evidence_separation = True
        observations.append(observed)
    assert checked_evidence_separation
    report = evaluate_pilot(cases, {"primary": tuple(observations)})
    assert report.status == "NOT_RUN"
    assert report.sample_count == 30
    assert report.positive_samples == 20
    assert report.negative_samples == 10
    assert "fusion_recall_at_5" in report.metrics["primary"].metrics
    assert "source_range_precision" in report.metrics["primary"].metrics
    thresholds = {
        item.gate_id: item.expected for item in report.gates["primary"].outcomes
    }
    assert thresholds["p11_citation_source_precision"] == {
        "operator": "ge",
        "value": 0.9,
    }
    selected = (cases[0], cases[-1])

    def mock_query(
        project_id: str,
        kb_id: str,
        text: str,
        lane: str,
    ) -> SearchAnswerResult:
        del lane
        return harness.runtime.sdk.search(project_id, kb_id, text)

    _, attempts, ranges = _query_cases(
        context,
        selected,
        _PilotInventory(documents, chunks, revisions),
        mock_query,
        ProviderBudgetLedger(tmp_path / "pilot-budget.sqlite3"),
    )
    assert len(ranges) == 4
    assert not any(attempts.values())
    positive_ranges = cast(
        dict[str, object], ranges[f"primary:{cases[0].case_id}"]
    )
    assert positive_ranges["expected"]
    assert "exact_text" not in str(positive_ranges["expected"])
    assert harness.runtime.sdk.health().remote_production_profile_ready is False


def test_live_label_alone_cannot_replace_case_bound_attempts() -> None:
    dataset = load_pilot_dataset()
    identity = PilotLiveEvidence(
        validation_mode="live",
        profile_revision_id="synthetic-profile",
        binding_identity="synthetic-binding",
        campaign_id="synthetic-campaign",
        dataset_sha256=dataset.dataset_sha256,
        case_attempts={},
        provider_models=("jina:synthetic-model", "qwen:synthetic-model"),
    )
    report = evaluate_pilot(dataset.cases, {}, identity)
    assert report.status == "BLOCKED"
    assert report.reason == "INCOMPLETE_DUAL_LANE_LABEL_EVALUATION"


@pytest.mark.parametrize("interrupted", (False, True))
def test_pilot_resume_recovers_only_state_referenced_job_after_restart(
    configured: tuple[ProductHarness, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted: bool,
) -> None:
    harness, profile_id = configured
    source_project_id = harness.runtime.sdk.list_projects()[0].project_id
    dataset = load_pilot_dataset()
    item = next(item for item in dataset.documents if len(item.versions) == 1)
    dataset = replace(
        dataset,
        documents=(item,),
        manifest=dataset.manifest.model_copy(
            update={"documents": (item.document,)}
        ),
    )
    context = _PilotRuntime(
        harness.runtime,
        {"source_profile_revision_id": profile_id},
        AcceptanceState(tmp_path / "resume.sqlite3", "synthetic-resume"),
        dataset,
    )
    key = _pilot_prefix(context) + item.document.versions[0].fixture_id

    def stop_after_persist(*_args: object) -> None:
        raise KeyboardInterrupt("TEST_ONLY 持久引用写入后停机")

    with monkeypatch.context() as patch:
        patch.setattr(harness.runtime.sdk, "_submit_job", lambda _: None)
        patch.setattr(p11_pilot_runtime, "_wait_job", stop_after_persist)
        with pytest.raises(KeyboardInterrupt):
            _prepare_corpus(context)
        pilot_id = context.state.resource(key)
        assert pilot_id is not None
        pilot_job = harness.runtime.sdk.get_job(pilot_id)
        source = harness.runtime.control.get_profile(profile_id)
        source_kb = harness.runtime.sdk.get_knowledge_base(
            source_project_id,
            source.knowledge_base_id,
        )
        unrelated = harness.runtime.sdk.create_document(
            source_kb.project_id,
            source_kb.knowledge_base_id,
            display_name="无关公开作业.docx",
            content=item.content(),
            media_type=p11_pilot_runtime._MEDIA_TYPE,
            idempotency_key="unrelated-job",
        )
    if interrupted:
        assert harness.runtime.p09.store.claim_ingestion(pilot_id) is not None
        with harness.runtime.connections.transaction(write=True) as db:
            db.execute(
                "UPDATE ingestion_jobs SET heartbeat_at=? WHERE job_id=?",
                ("2000-01-01T00:00:00+00:00", pilot_id),
            )
    settings = harness.runtime.settings
    harness.runtime.close()
    with build_product_runtime(
        settings,
        transport_factory=build_offline_mock_transport,
        recover_jobs=False,
    ) as restarted:
        assert restarted.sdk.get_job(unrelated.job_id).state.value == "queued"
        assert restarted.sdk.get_job(pilot_id).state.value in {
            "queued",
            "running",
        }
        resumed = replace(context, runtime=restarted)
        documents, _ = _prepare_corpus(resumed)
        assert documents[0].document_id == pilot_job.document_id
        assert restarted.sdk.get_job(pilot_id).state.value == "succeeded"
        assert restarted.sdk.get_job(unrelated.job_id).state.value == "queued"
        with pytest.raises(Conflict):
            restarted.p09.store.resume_ingestion_job(
                unrelated.job_id,
                project_id=pilot_job.project_id,
                knowledge_base_id=pilot_job.knowledge_base_id,
                idempotency_key=key,
            )
        assert restarted.sdk.get_job(unrelated.job_id).state.value == "queued"


def test_pilot_profile_change_gets_new_resources_before_query(
    configured: tuple[ProductHarness, str],
    tmp_path: Path,
) -> None:
    harness, profile_id = configured
    dataset = load_pilot_dataset()
    item = next(item for item in dataset.documents if len(item.versions) == 1)
    dataset = replace(
        dataset,
        documents=(item,),
        manifest=dataset.manifest.model_copy(
            update={"documents": (item.document,)}
        ),
    )
    context = _PilotRuntime(
        harness.runtime,
        {"source_profile_revision_id": profile_id},
        AcceptanceState(tmp_path / "binding.sqlite3", "synthetic-binding"),
        dataset,
    )
    original_prefix = _pilot_prefix(context)
    original_documents, _ = _prepare_corpus(context)
    source = harness.runtime.control.get_profile(profile_id)
    draft = RetrievalProfileDraft.model_validate(
        source.model_dump(include=set(RetrievalProfileDraft.model_fields))
    )
    draft = draft.model_copy(
        update={
            "retrieval_policy": {
                **dict(draft.retrieval_policy),
                "max_evidence_items": 1,
            },
            "evidence_policy": {},
        }
    )
    changed = harness.runtime.control.create_profile(draft)
    changed_context = replace(
        context,
        config={"source_profile_revision_id": changed.profile_revision_id},
    )
    assert _pilot_prefix(changed_context) != original_prefix
    changed_documents, profiles = _prepare_corpus(changed_context)
    assert changed_documents[0].knowledge_base_id != (
        original_documents[0].knowledge_base_id
    )
    for actual_id in profiles:
        actual = harness.runtime.control.get_profile(actual_id)
        assert actual.serving_fingerprint == changed.serving_fingerprint


def test_scoped_resume_keeps_inflight_job_running(
    configured: tuple[ProductHarness, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, profile_id = configured
    source = harness.runtime.control.get_profile(profile_id)
    project = harness.runtime.sdk.list_projects()[0]
    with monkeypatch.context() as patch:
        patch.setattr(harness.runtime.sdk, "_submit_job", lambda _: None)
        job = harness.runtime.sdk.create_document(
            project.project_id,
            source.knowledge_base_id,
            display_name="仍活跃的公开作业.docx",
            content=load_pilot_dataset().documents[0].content(),
            media_type=p11_pilot_runtime._MEDIA_TYPE,
            idempotency_key="inflight-job",
        )
    assert harness.runtime.p09.store.claim_ingestion(job.job_id) is not None
    current = harness.runtime.p09.store.resume_ingestion_job(
        job.job_id,
        project_id=job.project_id,
        knowledge_base_id=job.knowledge_base_id,
        idempotency_key="inflight-job",
    )
    assert current.state.value == "running"
    with harness.runtime.connections.transaction() as db:
        assert (
            db.execute(
                "SELECT state FROM ingestion_requests WHERE job_id=?",
                (job.job_id,),
            ).fetchone()[0]
            == "running"
        )


def test_later_quality_failure_revokes_old_pass_without_deleting_history(
    configured: tuple[ProductHarness, str],
) -> None:
    harness, profile_id = configured
    control = harness.runtime.control
    profile = control.get_profile(profile_id)
    record = QualityValidationRecord(
        profile_revision_id=profile_id,
        kind="retrieval_quality_verified",
        validation_mode="live",
        run_id="TEST_ONLY_state_machine",
        dataset_sha256="a" * 64,
        artifact_sha256="b" * 64,
        index_fingerprint=profile.index_semantic_fingerprint,
        serving_fingerprint=profile.serving_fingerprint,
        gates=dict.fromkeys(
            (
                "independent_labels",
                "source_precision",
                "recall",
                "negative_leakage",
            ),
            True,
        ),
        independent_holdout=True,
        labeled_queries=20,
        negative_queries=10,
        citation_source_precision=0.95,
        recall=0.9,
        negative_leakage=0,
    )
    control.quality.record(record)
    assert (
        control.quality.states(profile_id)["retrieval_quality_verified"]
        == "live"
    )
    control.quality.record(record.model_copy(update={"recall": 0.1}))
    assert "retrieval_quality_verified" not in control.quality.states(
        profile_id
    )
    assert control.quality.calibrated_spaces(profile_id) == ()
    with harness.runtime.connections.transaction() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM quality_validation_records "
                "WHERE profile_revision_id=?",
                (profile_id,),
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize(
    "data_plane",
    ({"reindex_required": True}, {"integrity_status": "attention_required"}),
)
def test_product_ready_requires_current_data_plane(
    configured: tuple[ProductHarness, str],
    monkeypatch: pytest.MonkeyPatch,
    data_plane: dict[str, object],
) -> None:
    harness, profile_id = configured
    control = harness.runtime.control
    actual = control.profile_validations(profile_id)
    validations = {
        key: run.model_copy(update={"validation_mode": "live"})
        for key, run in actual.items()
        if run is not None
    }
    # 以下仅模拟接受状态，专门验证数据面失效能撤销总就绪状态。
    states = dict.fromkeys(
        (
            "provider_connectivity_verified",
            "dual_slot_function_verified",
            "retrieval_quality_verified",
            "release_candidate_verified",
        ),
        "live",
    )
    states.update(
        {
            "local_contract_verified": "offline",
            "offline_evaluation_ready": "offline",
        }
    )
    monkeypatch.setattr(
        control,
        "system_evidence",
        lambda: {
            "active_profile_ids": [profile_id],
            "reindex_required": False,
        },
    )
    monkeypatch.setattr(control, "profile_validations", lambda _: validations)
    monkeypatch.setattr(control.quality, "states", lambda _: states)
    status = harness.runtime.sdk.health().model_copy(
        update={
            "reindex_required": False,
            "integrity_status": "ok",
        }
    )
    assert (
        _product_status(
            status,
            control,
            harness.runtime.compatibility,
        ).remote_production_profile_ready
        is True
    )
    assert (
        _product_status(
            status.model_copy(update=data_plane),
            control,
            harness.runtime.compatibility,
        ).remote_production_profile_ready
        is False
    )
