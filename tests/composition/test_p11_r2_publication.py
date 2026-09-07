"""候选方案发布失败、竞争和跨切换查询的行为回归。"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from rag_app.composition.product_runtime import _product_status
from rag_app.core.errors import IndexCorrupt, ProviderUnavailable
from rag_app.core.models import KnowledgeBaseScope, SearchRequest
from rag_app.product.models import (
    ImpactKind,
    RetrievalProfileDraft,
    RetrievalProfileRevision,
)
from rag_app.product.quality import QualityValidationRecord
from tests.adapters.parsers.docx.fixtures import build_package
from tests.composition.test_p11_r2_conformance import _draft
from tests.composition.test_product_runtime import _wait_for_job
from tests.product_support import (
    ProductHarness,
    activate_hot_standby_profile,
    build_product_harness,
    create_project_and_knowledge_base,
    create_provider_connections,
    validate_five_operations,
)


@pytest.fixture
def indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ProductHarness, str, str, str, str]]:
    monkeypatch.setenv("RAG_TEST_ALIYUN_CREDENTIAL", "synthetic-aliyun-value")
    harness = build_product_harness(tmp_path)
    try:
        project, kb = create_project_and_knowledge_base(harness)
        _, _, jina, aliyun = create_provider_connections(harness)
        validate_five_operations(harness, jina, aliyun)
        activate_hot_standby_profile(harness, kb, jina, aliyun)
        job = _wait_for_job(
            harness,
            harness.runtime.sdk.create_document(
                project,
                kb,
                display_name="合成合同.docx",
                content=build_package(
                    "<w:p><w:r><w:t>采购合同包含设备 ABC-123，"
                    "交付期限为三日。</w:t></w:r></w:p>"
                ),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                idempotency_key="publication-initial",
            ),
        )
        assert job.state.value == "succeeded"
        yield harness, project, kb, jina, aliyun
    finally:
        harness.close()


def _candidate(
    indexed: tuple[ProductHarness, str, str, str, str], instruction: str
) -> RetrievalProfileRevision:
    harness, _, kb, jina, aliyun = indexed
    profile = harness.runtime.control.create_profile(
        _draft(kb, jina, aliyun).model_copy(
            update={
                "standby_query_policy": {
                    "text_type": "query",
                    "query_instruct": instruction,
                },
            }
        )
    )
    harness.runtime.providers.validate(
        aliyun,
        operation="embedding.query",
        model="qwen3.7-text-embedding",
        expected_dimension=1024,
        request_policy=dict(profile.standby_query_policy),
    )
    return harness.runtime.control.activate_profile(
        profile.profile_revision_id,
        confirmed_impact=ImpactKind.NEW_INDEX_REVISION_REQUIRED,
    )


def _run(harness: ProductHarness, profile: RetrievalProfileRevision) -> None:
    assert profile.activation_job_id is not None
    harness.runtime.profiles.job_lifecycle(
        profile.activation_job_id, harness.runtime.p09.lifecycle
    ).run_ingestion(profile.activation_job_id)


@pytest.mark.parametrize("stage", ["build", "activate"])
def test_failed_build_keeps_old_pointers_and_can_retry(
    indexed: tuple[ProductHarness, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    harness, project, kb, _, _ = indexed
    control = harness.runtime.retrieval_runtime.persistence.control
    old_index = control.active_revision_id(kb)
    old_profile = harness.runtime.control.active_profile(kb)
    candidate = _candidate(indexed, "合成候选检索指令")

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise ProviderUnavailable(
            "TEST_ONLY 构建中断", stage="test.publication"
        )

    with monkeypatch.context() as patch:
        if stage == "build":
            patch.setattr(
                harness.runtime.profiles._resolve(
                    candidate
                ).lifecycle._builder._validator,
                "validate",
                _fail,
            )
        else:
            patch.setattr(control, "activate", _fail)
        _run(harness, candidate)
    assert control.active_revision_id(kb) == old_index
    assert harness.runtime.control.active_profile(kb) == old_profile
    assert harness.runtime.sdk.search(project, kb, "ABC-123").evidence
    job = harness.runtime.sdk.get_job(candidate.activation_job_id)
    assert job.state.value == "failed_retryable"
    retried = _wait_for_job(harness, harness.runtime.sdk.retry_job(job.job_id))
    assert retried.state.value == "succeeded"
    assert (
        harness.runtime.control.active_profile(kb).profile_revision_id
        == candidate.profile_revision_id
    )
    assert control.active_revision_id(kb) == retried.revision_id


def test_competing_drafts_cannot_overwrite_winning_publication(
    indexed: tuple[ProductHarness, str, str, str, str],
) -> None:
    harness, _, kb, _, _ = indexed
    first = _candidate(indexed, "第一方案")
    second = _candidate(indexed, "第二方案")
    _run(harness, first)
    control = harness.runtime.retrieval_runtime.persistence.control
    pointer = control.active_revision_id(kb)
    _run(harness, second)
    assert (
        harness.runtime.sdk.get_job(second.activation_job_id).state.value
        == "failed_terminal"
    )
    assert (
        harness.runtime.control.active_profile(kb).profile_revision_id
        == first.profile_revision_id
    )
    assert (
        harness.runtime.retrieval_runtime.persistence.control.active_revision_id(
            kb
        )
        == pointer
    )


def test_cancelled_candidate_and_expired_writer_cannot_activate(
    indexed: tuple[ProductHarness, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _, kb, _, _ = indexed
    control = harness.runtime.retrieval_runtime.persistence.control
    old_index = control.active_revision_id(kb)
    old_profile = harness.runtime.control.active_profile(kb)
    cancelled = _candidate(indexed, "将被取消的候选")
    harness.runtime.sdk.cancel_job(cancelled.activation_job_id)
    _run(harness, cancelled)
    assert control.active_revision_id(kb) == old_index
    expired = _candidate(indexed, "过期 Writer 的候选")
    activate = control.activate

    def _expire(kb_id: str, evidence: object, **kwargs: str) -> None:
        control.release_revision_lease(evidence.revision_id)
        activate(kb_id, evidence, **kwargs)

    monkeypatch.setattr(control, "activate", _expire)
    _run(harness, expired)
    assert (
        harness.runtime.sdk.get_job(expired.activation_job_id).state.value
        != "succeeded"
    )
    assert harness.runtime.control.active_profile(kb) == old_profile
    assert control.active_revision_id(kb) == old_index


def test_query_snapshot_survives_switch_and_rejects_mixed_profile(
    indexed: tuple[ProductHarness, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, project, kb, _, _ = indexed
    profile = harness.runtime.control.active_profile(kb)
    service = harness.runtime.profiles._resolve(profile).retrieval
    candidate = _candidate(indexed, "查询跨切换候选")
    control = harness.runtime.retrieval_runtime.persistence.control
    old_index = control.active_revision_id(kb)
    snapshot = control.active_query_snapshot

    def _switch(*args: object, **kwargs: object) -> object:
        frozen = snapshot(*args, **kwargs)
        _run(harness, candidate)
        return frozen

    request = SearchRequest(
        scope=KnowledgeBaseScope(project_id=project, knowledge_base_id=kb),
        text="ABC-123",
    )
    with monkeypatch.context() as patch:
        patch.setattr(control, "active_query_snapshot", _switch)
        result = service.search_and_answer(request)
    assert result.evidence
    assert result.active_index_revision_id == old_index
    old_chunks = {item.chunk_id for item in control.chunk_rows(old_index)}
    assert all(item.chunk_id in old_chunks for item in result.evidence)
    with pytest.raises(IndexCorrupt):
        service.search_and_answer(request)
    assert harness.runtime.sdk.search(project, kb, "ABC-123").evidence


def test_evidence_limits_change_query_behavior_without_reindex(
    indexed: tuple[ProductHarness, str, str, str, str],
) -> None:
    harness, project, kb, jina, aliyun = indexed
    job = _wait_for_job(
        harness,
        harness.runtime.sdk.create_document(
            project,
            kb,
            display_name="第二份合成合同.docx",
            content=build_package(
                "<w:p><w:r><w:t>采购合同包含设备 ABC-123，"
                "交付期限为五日。</w:t></w:r></w:p>"
            ),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            idempotency_key="second-evidence",
        ),
    )
    assert job.state.value == "succeeded"
    observed = []
    for policy in (
        {"max_evidence_items": 2},
        {"max_evidence_items": 1},
        {"minimum_support_items": 3},
        {"evidence_token_budget": 1},
    ):
        profile = harness.runtime.control.create_profile(
            _draft(kb, jina, aliyun).model_copy(
                update={"evidence_policy": policy}
            )
        )
        impact = harness.runtime.control.preview_impact(
            profile.profile_revision_id
        )
        assert impact.impact is ImpactKind.SERVING_RELOAD
        harness.runtime.control.activate_profile(
            profile.profile_revision_id, confirmed_impact=impact.impact
        )
        result = harness.runtime.sdk.search(project, kb, "ABC-123")
        assert result.active_index_revision_id == job.revision_id
        assert result.cache_hit is False
        for evidence in result.evidence:
            assert evidence.source_spans
            assert all(span.is_citable for span in evidence.source_spans)
            assert "<EMPTY>" not in evidence.citation_text
            assert "ABC-123" in evidence.citation_text
        observed.append(result)
    assert [len(item.evidence) for item in observed] == [2, 1, 2, 0]
    assert observed[0].status.value == "ANSWERABLE"
    assert observed[2].status.value == "INSUFFICIENT_EVIDENCE"
    assert observed[2].answer is None
    assert observed[3].answer is None


def test_crash_before_activation_recovers_ready_revision(
    indexed: tuple[ProductHarness, str, str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness, _, kb, _, _ = indexed
    control = harness.runtime.retrieval_runtime.persistence.control
    old_pointer = control.active_revision_id(kb)
    candidate = _candidate(indexed, "激活前崩溃")

    def _crash(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("TEST_ONLY 进程中断")

    with monkeypatch.context() as patch:
        patch.setattr(control, "activate", _crash)
        with pytest.raises(KeyboardInterrupt):
            _run(harness, candidate)
    assert control.active_revision_id(kb) == old_pointer
    assert control.recover_stale_jobs("2099-01-01T00:00:00+00:00") == 1
    harness.runtime.jobs.recover()
    job = _wait_for_job(
        harness, harness.runtime.sdk.get_job(candidate.activation_job_id)
    )
    assert job.state.value == "succeeded"
    assert control.active_revision_id(kb) == job.revision_id


def test_identical_profile_rebind_keeps_index_and_uses_new_query_snapshot(
    indexed: tuple[ProductHarness, str, str, str, str],
) -> None:
    harness, project, kb, _, _ = indexed
    current = harness.runtime.control.active_profile(kb)
    old_result = harness.runtime.sdk.search(project, kb, "ABC-123")
    draft = RetrievalProfileDraft.model_validate(
        current.model_dump(
            include=set(RetrievalProfileDraft.model_fields),
        )
    )
    identical = harness.runtime.control.create_profile(draft)
    assert (
        harness.runtime.control.preview_impact(
            identical.profile_revision_id
        ).impact
        is ImpactKind.NO_REINDEX
    )
    harness.runtime.control.activate_profile(
        identical.profile_revision_id, confirmed_impact=ImpactKind.NO_REINDEX
    )
    result = harness.runtime.sdk.search(project, kb, "ABC-123")
    assert (
        result.active_index_revision_id == old_result.active_index_revision_id
    )
    assert result.evidence == old_result.evidence
    assert (
        harness.runtime.control.active_profile(kb).profile_revision_id
        == identical.profile_revision_id
    )


def test_rotated_credential_invalidates_pending_publication(
    indexed: tuple[ProductHarness, str, str, str, str],
) -> None:
    harness, _, kb, jina, _ = indexed
    current = harness.runtime.control.active_profile(kb)
    control = harness.runtime.retrieval_runtime.persistence.control
    old_pointer = control.active_revision_id(kb)
    candidate = _candidate(indexed, "构建期间轮换凭据")
    credential = harness.runtime.control.get_connection(jina).credential_id
    harness.runtime.credentials.rotate(
        credential, "synthetic-rotation-during-build"
    )
    _run(harness, candidate)
    assert (
        harness.runtime.sdk.get_job(candidate.activation_job_id).state.value
        != "succeeded"
    )
    assert harness.runtime.control.active_profile(kb) == current
    assert control.active_revision_id(kb) == old_pointer


def test_persisted_records_can_satisfy_readiness_without_fixed_false(
    indexed: tuple[ProductHarness, str, str, str, str],
) -> None:
    harness, _, kb, _, _ = indexed
    profile = harness.runtime.control.active_profile(kb)
    control = harness.runtime.control
    assert profile is not None
    prerequisites = {
        "local_contract_verified": ("offline", {"check", "smoke", "frontend"}),
        "offline_evaluation_ready": ("offline", {"offline_eval"}),
        "provider_connectivity_verified": ("live", {"required_operations"}),
        "dual_slot_function_verified": (
            "live",
            {"primary", "standby", "failover", "isolation"},
        ),
        "retrieval_quality_verified": (
            "live",
            {
                "independent_labels",
                "source_precision",
                "recall",
                "negative_leakage",
            },
        ),
        "release_candidate_verified": (
            "live",
            {"release_verify", "release_acceptance"},
        ),
    }
    # 仅在隔离合成数据库中构造状态机输入，不表示执行过任何 Live 验收。
    with harness.runtime.connections.transaction(write=True) as connection:
        connection.execute(
            "UPDATE provider_validation_runs SET validation_mode='live', "
            "http_category='live_200'"
        )
    for kind, (mode, gates) in prerequisites.items():
        before = _product_status(
            harness.runtime.sdk.health(), control, harness.runtime.compatibility
        )
        assert before.remote_production_profile_ready is False
        control.quality.record(
            QualityValidationRecord.model_validate(
                {
                    "profile_revision_id": profile.profile_revision_id,
                    "kind": kind,
                    "validation_mode": mode,
                    "run_id": "TEST_ONLY_readiness_fixture",
                    "dataset_sha256": "c" * 64,
                    "artifact_sha256": "d" * 64,
                    "index_fingerprint": profile.index_semantic_fingerprint,
                    "serving_fingerprint": profile.serving_fingerprint,
                    "gates": dict.fromkeys(gates, True),
                    "independent_holdout": True,
                    "labeled_queries": 20,
                    "negative_queries": 10,
                    "citation_source_precision": 0.95,
                    "recall": 0.9,
                    "negative_leakage": 0,
                }
            )
        )
    ready = _product_status(
        harness.runtime.sdk.health(), control, harness.runtime.compatibility
    )
    assert ready.remote_dense_confidence_calibrated is True
    assert ready.remote_production_profile_ready is True
    assert harness.runtime.sdk.health().remote_production_profile_ready is False
