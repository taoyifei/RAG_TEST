"""已暴露集分类和实际回包收据；所有构造的 Live 标记仅为状态机单元夹具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from evaluation.p11_pilot import PilotLiveEvidence, PilotReport, evaluate_pilot
from evaluation.p11_pilot_data import load_pilot_dataset
from evaluation.p11_pilot_runtime import (
    _active_inventory,
    _PilotInventory,
    _PilotRuntime,
    _prepare_corpus,
    _query_cases,
    _record_quality,
    _remap_cases,
)
from evaluation.v2.models import CaseObservation
from evaluation.v2.runtime import _effective_cases
from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.core.models import SearchAnswerResult
from rag_app.product.live_acceptance import AcceptanceState
from rag_app.product.quality import QualityValidationRecord
from tests.application.test_p11_r4_quality import (
    configured as configured,  # noqa: PLC0414
)
from tests.product_support import ProductHarness
from tests.support.p11_closure_replay import run_replay


def _unit_synthetic_report(
    *, cached: bool = False, campaign_bound: bool = True
) -> PilotReport:
    """只为分类/来源门状态机重建受控输入，结果不得发布为 Live 证据。"""
    dataset = load_pilot_dataset()
    replay = run_replay()
    observations = {}
    for lane, values in replay["observations"].items():
        items = []
        for case, value in zip(dataset.cases, values, strict=True):
            item = CaseObservation.model_validate(value)
            document_ids = case.expected.relevant_document_ids
            items.append(
                item.model_copy(
                    update={
                        "retrieved_document_ids": document_ids,
                        "evidence_document_ids": document_ids,
                        "cited_document_ids": document_ids,
                        "retrieved_chunk_ids": case.expected.relevant_chunk_ids,
                        "fused_chunk_ids": case.expected.relevant_chunk_ids,
                        "reranked_chunk_ids": case.expected.relevant_chunk_ids,
                        "evidence_chunk_ids": case.expected.relevant_chunk_ids,
                        "cited_chunk_ids": case.expected.relevant_chunk_ids,
                        "channel_chunk_ids": (
                            ("dense:" + lane, case.expected.relevant_chunk_ids),
                        ),
                        "embedding_call_count": 1,
                        "cache_hit": cached,
                    }
                )
            )
        observations[lane] = tuple(items)
    return evaluate_pilot(
        dataset.cases,
        observations,
        PilotLiveEvidence(
            validation_mode="live",
            profile_revision_id="unit_synthetic",
            binding_identity="unit_synthetic",
            campaign_id="unit_synthetic",
            dataset_sha256=dataset.dataset_sha256,
            case_attempts={
                f"{lane}:{case.case_id}": (
                    f"unit_synthetic:{lane}:{case.case_id}",
                )
                for lane in observations
                for case in dataset.cases
            },
            retrieval_budget_campaign_ids=(
                {
                    case.knowledge_base_id: "unit_synthetic"
                    for case in dataset.cases
                }
                if campaign_bound
                else {}
            ),
            provider_models=("unit_synthetic",),
        ),
        evaluation_kind="exposed_regression",
    )


@pytest.mark.parametrize("cached", (False, True))
def test_exposed_classification_preserves_live_provenance(cached: bool) -> None:
    """分类改变不放宽实际调用和禁缓存条件。"""
    report = _unit_synthetic_report(cached=cached)
    assert report.evaluation_kind == "exposed_regression"
    assert report.status == ("BLOCKED" if cached else "PASS")
    assert report.reason == (
        "MISSING_CASE_BOUND_LIVE_ATTEMPTS_OR_ROUTE"
        if cached
        else "EXPOSED_REGRESSION_ACCEPTED"
    )


def test_live_provenance_requires_retrieval_campaign_binding() -> None:
    """实际请求必须能追溯到每个独立 Pilot 知识库的授权账本。"""
    report = _unit_synthetic_report(campaign_bound=False)
    assert report.status == "BLOCKED"
    assert report.reason == "MISSING_CASE_BOUND_LIVE_ATTEMPTS_OR_ROUTE"


def test_replay_classification_cannot_make_offline_quality_live() -> None:
    """本轮完整回放明确是已暴露回归，依旧不能成为 Live。"""
    report = run_replay()["report"]
    assert report["evaluation_kind"] == "exposed_regression"
    assert report["status"] == "NOT_RUN"


@pytest.mark.parametrize("mode", ("offline", "mock", "live"))
def test_exposed_quality_writer_keeps_all_existing_quality_conditions(
    configured: tuple[ProductHarness, str],
    mode: str,
) -> None:
    """受信任记录允许真实回归用途，但不允许离线或失败门获得校准。"""
    harness, profile_id = configured
    profile = harness.runtime.control.get_profile(profile_id)
    record = QualityValidationRecord.model_validate(
        {
            "profile_revision_id": profile_id,
            "kind": "retrieval_quality_verified",
            "validation_mode": mode,
            "run_id": "unit_synthetic",
            "dataset_sha256": "a" * 64,
            "artifact_sha256": "b" * 64,
            "index_fingerprint": profile.index_semantic_fingerprint,
            "serving_fingerprint": profile.serving_fingerprint,
            "gates": dict.fromkeys(
                (
                    "independent_labels",
                    "source_precision",
                    "recall",
                    "negative_leakage",
                ),
                True,
            ),
            "evaluation_kind": "exposed_regression",
            "independent_holdout": False,
            "labeled_queries": 20,
            "negative_queries": 10,
            "citation_source_precision": 0.95,
            "recall": 0.9,
            "negative_leakage": 0,
        }
    )
    quality = harness.runtime.control.quality
    quality.record(record)
    assert bool(quality.calibrated_spaces(profile_id)) is (mode == "live")
    for update in (
        {"recall": 0.1},
        {"labeled_queries": 19},
        {"negative_queries": 9},
        {"negative_leakage": 1.0},
        {"citation_source_precision": 0.1},
        {"independent_holdout": True},
        {"gates": record.gates | {"primary:source_ranges_complete": False}},
    ):
        quality.record(record.model_copy(update=update))
        assert quality.calibrated_spaces(profile_id) == ()
    with pytest.raises(ValueError, match="Extra inputs"):
        QualityValidationRecord.model_validate(
            record.model_dump() | {"accepted": True}
        )


def test_runtime_records_truthful_classification_and_rejects_offline_report(
    configured: tuple[ProductHarness, str],
    tmp_path: Path,
) -> None:
    """现有 writer 保存分类，不把伪造的离线 PASS 写成 Live。"""
    harness, profile_id = configured
    context = _PilotRuntime(
        harness.runtime,
        {"source_profile_revision_id": profile_id},
        AcceptanceState(tmp_path / "state.sqlite3", "unit_synthetic"),
        load_pilot_dataset(),
    )
    report = _unit_synthetic_report()
    assert report.identity is not None
    offline = report.model_copy(
        update={
            "identity": report.identity.model_copy(
                update={"validation_mode": "offline"}
            )
        }
    )
    assert _record_quality(context, offline, ()) == []
    identifiers = _record_quality(context, report, ())
    assert len(identifiers) == 1
    with harness.runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT accepted, record_json FROM quality_validation_records "
            "WHERE record_id=?",
            (identifiers[0],),
        ).fetchone()
    assert row[0] == 1
    stored = json.loads(row[1])
    assert stored["evaluation_kind"] == "exposed_regression"
    assert stored["independent_holdout"] is False
    assert all(stored["gates"].values())


def test_query_receipt_preserves_actual_callback_not_expected_answer(
    configured: tuple[ProductHarness, str],
    tmp_path: Path,
) -> None:
    """现有每题收据保存真实分数/来源/回答，故意不同于标签时仍原样保存。"""
    harness, profile_id = configured
    dataset = load_pilot_dataset()
    context = _PilotRuntime(
        harness.runtime,
        {"source_profile_revision_id": profile_id},
        AcceptanceState(tmp_path / "state.sqlite3", "unit_synthetic"),
        dataset,
    )
    documents, _ = _prepare_corpus(context)
    chunks, revisions = _active_inventory(harness.runtime, documents)
    cases = _effective_cases(
        _remap_cases(dataset, documents), chunks, require_fixed_labels=False
    )
    captured: dict[str, SearchAnswerResult] = {}

    def callback(
        project: str, kb: str, query: str, lane: str
    ) -> SearchAnswerResult:
        result = harness.runtime.sdk.search(project, kb, query)
        assert result.diagnostics is not None
        result = result.model_copy(
            update={
                "answer": "unit_synthetic actual callback text",
                "serving_fingerprint": "sha256:" + "f" * 64,
                "diagnostics": result.diagnostics.model_copy(
                    update={
                        "reranked": tuple(
                            item.model_copy(update={"score": 0.123})
                            for item in result.diagnostics.reranked
                        )
                    }
                ),
            }
        )
        captured[lane] = result
        return result

    _, attempts, ranges = _query_cases(
        context,
        (cases[0],),
        _PilotInventory(documents, chunks, revisions),
        callback,
        ProviderBudgetLedger(tmp_path / "ledger.sqlite3"),
    )
    assert not any(attempts.values())
    for lane, result in captured.items():
        receipt = cast(dict[str, object], ranges[f"{lane}:{cases[0].case_id}"])
        actual = cast(dict[str, object], receipt["actual_result"])
        assert result.diagnostics is not None
        assert actual["diagnostics"] == result.diagnostics.model_dump(
            mode="json"
        )
        assert actual["confidence"] == result.confidence.model_dump(mode="json")
        assert actual["evidence"] == [
            item.model_dump(mode="json") for item in result.evidence
        ]
        assert actual["answer"] == result.answer
        assert actual["serving_fingerprint"] == result.serving_fingerprint
        assert (
            cases[0].expected.required_source_ranges[0].exact_text
            != actual["answer"]
        )
