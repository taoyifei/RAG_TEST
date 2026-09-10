"""V3-07 公开查询质量数据集与评分器回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.v3_07_query_quality_dataset import (
    CASES,
    DOCUMENTS,
    QueryCase,
    dataset_sha256,
    validate_dataset,
)
from evaluation.v3_07_query_quality_runner import (
    _document_bytes,
    _score_case,
    _span_is_valid,
    compare_reports,
)


def _span(text: str) -> dict[str, object]:
    """构造评分器使用的最小公开 SourceSpan JSON。"""
    return {
        "schema_version": "3",
        "span_type": "original_text",
        "node_id": "node_" + "1" * 32,
        "source_anchor": {"ordinal": 1, "structural_path": []},
        "structural_path": [],
        "chunk_start_char": 0,
        "chunk_end_char": len(text),
        "source_start_char": 0,
        "source_end_char": len(text),
        "is_repeated": False,
        "is_citable": True,
        "metadata": [],
    }


def test_public_dataset_is_balanced_and_split_isolated() -> None:
    validation = validate_dataset()
    tuning_documents = {
        item.document_key for item in DOCUMENTS if item.split == "tuning"
    }
    holdout_documents = {
        item.document_key for item in DOCUMENTS if item.split == "holdout"
    }

    assert validation["case_count"] == 88
    assert validation["split_counts"] == {"tuning": 44, "holdout": 44}
    assert validation["dataset_sha256"] == dataset_sha256()
    assert tuning_documents.isdisjoint(holdout_documents)
    assert all(case.split in case.paraphrase_group for case in CASES)


def test_public_docx_generation_is_byte_deterministic() -> None:
    for document in DOCUMENTS:
        assert _document_bytes(document) == _document_bytes(document)


def test_rebased_trimmed_span_matches_its_canonical_source() -> None:
    canonical_text = " 公开片段 "
    canonical = _span(canonical_text)
    actual = {
        **canonical,
        "chunk_end_char": len("公开片段"),
        "source_start_char": 1,
        "source_end_char": len(canonical_text) - 1,
    }

    assert _span_is_valid(
        actual,
        "公开片段",
        ({"span": canonical, "text": canonical_text},),
    )
    assert not _span_is_valid(
        {**actual, "source_start_char": 0, "source_end_char": 4},
        "公开片段",
        ({"span": canonical, "text": canonical_text},),
    )


def test_score_separates_answer_fragments_from_supported_source() -> None:
    source_text = "XQ-71 的维护周期为 18 天"
    span = _span(source_text)
    case = QueryCase(
        case_id="eval_v307_unit_table",
        split="tuning",
        slice_name="table",
        paraphrase_group="tuning_unit_table",
        query="XQ-71 的维护周期是多少？",
        expected_document_keys=("tuning_table",),
        expected_answer_fragments=("18 天",),
        expected_source_fragments=("XQ-71", "18 天"),
        answerable=True,
        expected_answer_type="FACT",
    )
    payload = {
        "answer": "18 天 [S1]",
        "status": "ANSWERABLE",
        "requested_answer_type": "FACT",
        "evidence": [
            {
                "document_id": "doc_public",
                "chunk_id": "chunk_public",
                "citation_text": source_text,
                "source_spans": [span],
                "metadata": [["answer_support", [["status", "SUPPORTED"]]]],
            }
        ],
    }
    history = {"status": "ANSWERED", "provider_usage": []}
    trace = {
        "trace": {"status": "ANSWERED"},
        "candidate_decisions": [
            {"stage": "rerank", "chunk_id": "chunk_public", "rank": 1}
        ],
    }
    chunks = {
        "chunk_public": {
            "document_key": "tuning_table",
            "source_spans": [{"span": span, "text": source_text}],
        }
    }

    score = _score_case(
        case,
        payload,
        history,
        trace,
        {"doc_public": "tuning_table"},
        chunks,
    )

    assert score["answer_correct"] is True
    assert score["answer_type_correct"] is True
    assert score["citation_spans_valid"] is True
    assert score["support_fragment_recall"] == 1.0
    assert score["support_precision"] == 1.0


def _comparison_report(
    target: str,
    cold_p95_ms: float,
    *,
    gates_passed: bool,
) -> dict[str, object]:
    """构造不含正文的最小 B/C 重复运行报告。"""
    quality = {
        "answerable_accuracy": 1.0 if gates_passed else 0.0,
        "false_refusal_rate": 0.0 if gates_passed else 1.0,
        "false_answer_rate": 0.0,
        "citation_source_precision": 1.0,
        "citation_span_validity": 1.0,
        "paraphrase_consistency": 1.0,
        "hard_constraint_violations": 0,
        "status_parity": 1.0,
        "retrieval_recall_at_10": 1.0,
        "target_document_recall": 1.0,
        "support_set_recall": 1.0,
        "support_set_precision": 1.0,
        "provider_call_count": 0,
    }
    performance = {
        "cold_p50_ms": cold_p95_ms - 10,
        "cold_p95_ms": cold_p95_ms,
        "ttfc_p50_ms": cold_p95_ms - 11,
        "ttfc_p95_ms": cold_p95_ms - 1,
        "warm_p50_ms": 10.0,
        "warm_p95_ms": 11.0,
        "warm_cache_hit_rate": 1.0,
        "process_cpu_seconds": 2.0,
        "singleflight": {"request_count": 8},
        "resource_after": {"threads": 2, "fds": 3},
    }
    return {
        "dataset_sha256": "sha256:public",
        "seed_manifest_sha256": "sha256:seed",
        "split": "tuning",
        "trace_mode": "DIAGNOSTIC",
        "corpus_identity": {"active_revision_id": "irev_public"},
        "hardware": {"fingerprint": "sha256:hardware"},
        "target": {"commit_sha": target, "dirty": False},
        "quality": quality,
        "performance": performance,
        "gates": {"passed": gates_passed},
    }


def _write_comparison_report(
    path: Path,
    payload: dict[str, object],
) -> Path:
    """写入比较器输入并返回路径。"""
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_compare_uses_three_run_median_for_p95_gate(tmp_path: Path) -> None:
    baseline = [
        _write_comparison_report(
            tmp_path / f"baseline-{index}.json",
            _comparison_report("baseline", value, gates_passed=False),
        )
        for index, value in enumerate((100.0, 130.0, 90.0), 1)
    ]
    candidate = [
        _write_comparison_report(
            tmp_path / f"candidate-{index}.json",
            _comparison_report("candidate", value, gates_passed=True),
        )
        for index, value in enumerate((119.0, 140.0, 80.0), 1)
    ]

    comparison = compare_reports(
        baseline,
        candidate,
        tmp_path / "comparison.json",
    )

    assert comparison["passed"] is True
    performance = comparison["performance"]
    assert isinstance(performance, dict)
    assert performance["replicate_count"] == 3
    assert performance["baseline_cold_p95_ms"] == 100.0
    assert performance["candidate_cold_p95_ms"] == 119.0
    assert performance["candidate_to_baseline_p95_ratio"] == 1.19


def test_compare_rejects_a_single_performance_sample(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="至少需要三次"):
        compare_reports(
            (tmp_path / "baseline.json",),
            (tmp_path / "candidate.json",),
            tmp_path / "comparison.json",
        )
