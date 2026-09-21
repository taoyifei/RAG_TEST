"""F0 runner 区分来源入包、具体 Gold Span、真值待审和拒答。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evaluation.wanshitong.v2.run_wb08r03_candidate import _cases
from evaluation.wanshitong.v2.run_wb08r03f_candidate import (
    _CONCURRENCY_4,
    _CORE_ANSWER_16,
    _EVIDENCE_PACK_12,
    _NATURAL_36,
    _concurrency_gate,
    _evidence_pack_12_gate,
    _failed_24_gate,
    _full_96_gate,
    _functional_gate,
    _telemetry_summary,
    load_truth,
    preflight_trace_source,
    read_history_identity,
    read_trace,
    score_observation,
    summarize,
)

_TRACE_ID = "trace_" + "a" * 32


def _observed(
    *,
    status: str = "ANSWERABLE",
    citations: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "trace_id": _TRACE_ID,
        "status": status,
        "reason_code": status,
        "terminal_event_count": 1,
        "final_count": 1,
        "answer": "已核对事实。[S1]" if status == "ANSWERABLE" else "",
        "citations": citations or [],
        "request_total_ms": 100,
    }


def _sources_for_case(
    case: dict[str, object],
    supports: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "support_id": f"S{index}",
            "document_version_id": supports[gold["support_id"]][
                "document_version_id"
            ],
            "chunk_id": supports[gold["support_id"]]["chunk_id"],
            "node_ids": [supports[gold["support_id"]]["node_id"]],
        }
        for index, gold in enumerate(case["gold_supports"], start=1)
    ]


def test_gold_admission_requires_exact_chunk_and_node() -> None:
    _, cases, supports = load_truth()
    case = cases["WB08R-F-037"]
    admitted = _sources_for_case(case, supports)
    events = {
        "retrieval.generation_evidence": [
            {"admitted_sources": admitted, "hard_rejected_sources": []}
        ],
        "retrieval.claim_publication": [
            {"accepted_claim_count": 1, "extractive_fallback_used": False}
        ],
    }
    observed = _observed(
        citations=[
            {
                "document_title": case["expected_source_documents"][0],
                "quote": "原文",
            }
        ]
    )

    scored = score_observation(case, supports, observed, events)

    assert scored["expected_source_in_pack"] is True
    assert scored["gold_support_in_pack"] is True
    assert scored["gold_hard_rejected"] is False
    assert scored["nonempty_answer_with_support"] is True

    wrong_span = [{**admitted[0], "node_ids": ["node_other"]}]
    events["retrieval.generation_evidence"][0]["admitted_sources"] = wrong_span
    scored = score_observation(case, supports, observed, events)
    assert scored["expected_source_in_pack"] is True
    assert scored["gold_support_in_pack"] is False


def test_hard_rejected_gold_is_reported_separately() -> None:
    _, cases, supports = load_truth()
    case = cases["WB08R-F-037"]
    gold = _sources_for_case(case, supports)[0]
    events = {
        "retrieval.generation_evidence": [
            {
                "admitted_sources": [],
                "hard_rejected_sources": [
                    {**gold, "reasons": ["SCOPE_MISMATCH"]}
                ],
            }
        ]
    }

    scored = score_observation(case, supports, _observed(), events)

    assert scored["expected_source_in_pack"] is False
    assert scored["gold_support_in_pack"] is False
    assert scored["gold_hard_rejected"] is True


def test_review_case_never_becomes_a_refusal_success() -> None:
    _, cases, supports = load_truth()
    case = {
        **cases["WB08R-N-050"],
        "truth_status": "NEEDS_TRUTH_REVIEW",
        "expected_behavior": "NEEDS_TRUTH_REVIEW",
        "notes": "来源尚待审核",
    }
    scored = score_observation(
        case,
        supports,
        _observed(status="INSUFFICIENT_EVIDENCE"),
        {},
    )
    summary = summarize([scored])

    assert scored["truth_status"] == "NEEDS_TRUTH_REVIEW"
    assert scored["expected_source_in_pack"] == "NOT_OBSERVED"
    assert summary["verified_count"] == 0
    assert summary["false_refusal_count"] == 0
    assert summary["needs_truth_review_case_ids"] == ["WB08R-N-050"]


def test_verified_refusal_with_gold_is_counted_false_refusal() -> None:
    _, cases, supports = load_truth()
    scored = score_observation(
        cases["WB08R-F-037"],
        supports,
        _observed(status="INSUFFICIENT_EVIDENCE"),
        {},
    )
    summary = summarize([scored])

    assert summary["supported_denominator"] == 1
    assert summary["false_refusal_count"] == 1
    assert summary["nonempty_answer_with_support_count"] == 0
    assert summary["expected_source_in_pack_not_observed"] == 1


def test_trace_file_uses_read_only_universal_database(tmp_path: Path) -> None:
    trace_db = tmp_path / "universal-rag.sqlite3"
    with sqlite3.connect(trace_db) as connection:
        connection.execute(
            "CREATE TABLE query_trace_events "
            "(trace_id TEXT, sequence INTEGER, "
            "event_name TEXT, payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO query_trace_events VALUES (?, ?, ?, ?)",
            (
                _TRACE_ID,
                1,
                "retrieval.generation_evidence",
                json.dumps({"attributes": {"admitted_sources": []}}),
            ),
        )
        connection.execute(
            "INSERT INTO query_trace_events VALUES (?, ?, ?, ?)",
            (
                _TRACE_ID,
                2,
                "retrieval.claim_publication",
                json.dumps({"attributes": {"accepted_claim_count": 0}}),
            ),
        )
        connection.execute(
            "INSERT INTO query_trace_events VALUES (?, ?, ?, ?)",
            (
                _TRACE_ID,
                3,
                "retrieval.generate",
                json.dumps({"attributes": {"mode": "none"}}),
            ),
        )
        connection.execute(
            "INSERT INTO query_trace_events VALUES (?, ?, ?, ?)",
            (
                _TRACE_ID,
                4,
                "retrieval.complete",
                json.dumps({"attributes": {"status": "ANSWERABLE"}}),
            ),
        )

    events, error = read_trace(
        _TRACE_ID, trace_db=trace_db, trace_container=None
    )

    assert error is None
    assert events["retrieval.generation_evidence"][0] == {
        "admitted_sources": []
    }
    assert not (tmp_path / "product-traces.sqlite3").exists()


def test_trace_preflight_requires_main_query_tables(tmp_path: Path) -> None:
    trace_db = tmp_path / "universal-rag.sqlite3"
    with sqlite3.connect(trace_db) as connection:
        connection.execute("CREATE TABLE query_history (trace_id TEXT)")
        connection.execute(
            "CREATE TABLE query_trace_events "
            "(trace_id TEXT, sequence INTEGER, "
            "event_name TEXT, payload_json TEXT)"
        )

    observed = preflight_trace_source(
        trace_db=trace_db, trace_container=None
    )

    assert observed == {
        "database": "universal-rag.sqlite3",
        "mode": "ro",
        "required_tables": ["query_history", "query_trace_events"],
        "source": "file",
    }


def test_trace_preflight_never_creates_missing_database(
    tmp_path: Path,
) -> None:
    trace_db = tmp_path / "universal-rag.sqlite3"

    with pytest.raises(ValueError, match="TRACE_DATABASE_INVALID"):
        preflight_trace_source(trace_db=trace_db, trace_container=None)

    assert not trace_db.exists()


def test_catalog_shortcut_trace_is_complete_without_pack(
    tmp_path: Path,
) -> None:
    trace_db = tmp_path / "universal-rag.sqlite3"
    with sqlite3.connect(trace_db) as connection:
        connection.execute(
            "CREATE TABLE query_trace_events "
            "(trace_id TEXT, sequence INTEGER, "
            "event_name TEXT, payload_json TEXT)"
        )
        for sequence, (name, attributes) in enumerate(
            (
                (
                    "retrieval.claim_publication",
                    {"shortcut_origin": "CATALOG_FAST_PATH"},
                ),
                ("retrieval.complete", {"status": "ANSWERABLE"}),
            ),
            start=1,
        ):
            connection.execute(
                "INSERT INTO query_trace_events VALUES (?, ?, ?, ?)",
                (
                    _TRACE_ID,
                    sequence,
                    name,
                    json.dumps({"attributes": attributes}),
                ),
            )
    events, error = read_trace(
        _TRACE_ID, trace_db=trace_db, trace_container=None
    )
    assert error is None
    assert "retrieval.generation_evidence" not in events


def test_evidence_pack_12_has_only_verified_gold_cases() -> None:
    _, cases, _ = load_truth()
    assert len(_EVIDENCE_PACK_12) == 12
    assert all(
        cases[case_id]["truth_status"] == "VERIFIED"
        and cases[case_id]["gold_supports"]
        for case_id in _EVIDENCE_PACK_12
    )


def test_fallback_rate_uses_actual_generation_mode() -> None:
    _, cases, supports = load_truth()
    case = cases["WB08R-F-037"]
    observed = _observed(
        citations=[{"document_title": case["expected_source_documents"][0]}]
    )
    scored = score_observation(
        case,
        supports,
        observed,
        {
            "retrieval.claim_publication": [{"accepted_claim_count": 0}],
            "retrieval.generate": [{"mode": "extractive_fallback"}],
        },
    )

    assert scored["extractive_fallback_used"] is True
    assert scored["nonempty_answer_with_support"] is True
    assert summarize([scored])["extractive_fallback_rate"] == 1.0


def test_gate_a_requires_observed_zero_structural_pollution() -> None:
    rows = [
        {
            "case_id": case_id,
            "expected_source_in_pack": True,
            "gold_hard_rejected": False,
            "structural_sibling_pollution_count": 0,
            "structural_sibling_observation_status": "COMPLETE",
            "table_sibling_atom_conflict_count": (
                0
                if case_id in {"WB08R-F-015", "WB08R-N-056"}
                else "NOT_APPLICABLE"
            ),
        }
        for case_id in sorted(_EVIDENCE_PACK_12)
    ]
    assert _evidence_pack_12_gate(rows) == "PASSED"

    rows[0]["structural_sibling_pollution_count"] = "NOT_OBSERVED"
    assert _evidence_pack_12_gate(rows) == "NOT_OBSERVED_TRACE_FIELDS"

    rows[0]["structural_sibling_pollution_count"] = 1
    assert _evidence_pack_12_gate(rows) == "FAILED"

    rows[0]["structural_sibling_pollution_count"] = 0
    rows[0]["structural_sibling_observation_status"] = "PARTIAL"
    assert _evidence_pack_12_gate(rows) == "NOT_OBSERVED_PARTIAL_GROUP_MAP"

    rows[0]["structural_sibling_observation_status"] = "COMPLETE"
    table_row = next(row for row in rows if row["case_id"] == "WB08R-F-015")
    table_row["table_sibling_atom_conflict_count"] = "NOT_OBSERVED"
    assert _evidence_pack_12_gate(rows) == "NOT_OBSERVED_TABLE_ROW_LINKS"
    table_row["table_sibling_atom_conflict_count"] = 1
    assert _evidence_pack_12_gate(rows) == "FAILED"


def test_table_sibling_row_linked_to_atom_is_detected() -> None:
    _, cases, supports = load_truth()
    case = cases["WB08R-F-015"]
    admitted = _sources_for_case(case, supports)
    first_gold = supports[case["gold_supports"][0]["support_id"]]
    for source in admitted:
        source.update(
            {
                "source_group_id": "group_gold",
                "table_group_id": "table_gold",
                "table_node_id": first_gold["table_node_id"],
                "table_row_index": 2,
                "linked_atom_ids": ["A1"],
            }
        )
    admitted.append(
        {
            **admitted[0],
            "support_id": "S_header",
            "table_row_index": 0,
            "linked_atom_ids": ["A1"],
        }
    )
    admitted.append(
        {
            **admitted[0],
            "support_id": "S_wrong",
            "table_row_index": 3,
            "linked_atom_ids": ["A1"],
        }
    )
    events = {
        "retrieval.generation_evidence": [
            {
                "admitted_sources": admitted,
                "hard_rejected_sources": [],
                "structural_sibling_pollution_count": 0,
                "structural_sibling_observation_status": "COMPLETE",
            }
        ]
    }
    scored = score_observation(case, supports, _observed(), events)
    assert scored["table_sibling_atom_conflict_count"] == 1


def test_core_and_natural_gate_selections_cover_contract_categories() -> None:
    assert len(_CORE_ANSWER_16) == 16
    assert len(_NATURAL_36) == 36
    assert len(_CONCURRENCY_4) == 4
    assert len(_cases(frozenset(_CORE_ANSWER_16))) == 16
    selected = _cases(_NATURAL_36)
    assert len(selected) == 36
    styles = {row["question_style"] for _, row in selected}
    assert {
        "short_ellipsis",
        "colloquial",
        "typo_abbreviation",
        "multi_turn",
        "compound",
        "adversarial",
    } <= styles
    assert list(_CORE_ANSWER_16.values()).count("template_catalog") == 2
    assert list(_CORE_ANSWER_16.values()).count("short_followup") == 2


def test_functional_gate_needs_answer_bound_semantic_review() -> None:
    case_id = "WB08R-F-037"
    row = {
        "case_id": case_id,
        "frozen_expected_behavior": "ANSWER",
        "status": "ANSWERABLE",
        "citation_count": 1,
        "expected_source_in_citations": True,
        "answer_chars": 12,
        "terminal_event_count": 1,
        "final_count": 1,
        "question_sha256": "q" * 64,
        "answer_sha256": "a" * 64,
    }
    result = _functional_gate([row], frozenset({case_id}), {})
    assert result["status"] == "NOT_REVIEWED"
    review = {
        case_id: {
            "question_sha256": "q" * 64,
            "answer_sha256": "a" * 64,
            "unsupported_high_risk_fact_count": 0,
            "wrong_source_severe_error_count": 0,
            "template_body_overreach_count": 0,
            "structural_sibling_error_count": 0,
        }
    }
    assert (
        _functional_gate([row], frozenset({case_id}), review)["status"]
        == "PASSED"
    )
    review[case_id]["answer_sha256"] = "b" * 64
    assert (
        _functional_gate([row], frozenset({case_id}), review)["status"]
        == "NOT_REVIEWED"
    )
    review[case_id]["answer_sha256"] = "a" * 64
    review[case_id]["unsupported_high_risk_fact_count"] = 1
    assert (
        _functional_gate([row], frozenset({case_id}), review)["status"]
        == "FAILED"
    )


def test_concurrency_gate_checks_trace_and_history_isolation() -> None:
    rows = [
        {
            "case_id": case_id,
            "frozen_expected_behavior": (
                "REFUSE" if category == "safe_refusal" else "ANSWER"
            ),
            "status": (
                "INSUFFICIENT_EVIDENCE"
                if category == "safe_refusal"
                else "ANSWERABLE"
            ),
            "terminal_event_count": 1,
            "final_count": 1,
            "trace_id": "trace_" + f"{index:032x}",
            "question_sha256": f"{index:064x}",
            "trace_original_query_sha256": f"{index:064x}",
            "history_owner_sha256": f"{index + 4:064x}",
            "history_question_sha256": f"{index:064x}",
            "history_context_present": case_id == "WB08R-N-043",
        }
        for index, (case_id, category) in enumerate(
            _CONCURRENCY_4.items(), start=1
        )
    ]
    assert _concurrency_gate(rows)["status"] == "PASSED"
    rows[0]["history_owner_sha256"] = rows[1]["history_owner_sha256"]
    assert _concurrency_gate(rows)["status"] == "FAILED"
    rows[0]["history_owner_sha256"] = None
    assert _concurrency_gate(rows)["status"] == "NOT_OBSERVED"


def test_history_identity_uses_read_only_universal_database(
    tmp_path: Path,
) -> None:
    trace_db = tmp_path / "universal-rag.sqlite3"
    with sqlite3.connect(trace_db) as connection:
        connection.execute(
            "CREATE TABLE query_history "
            "(trace_id TEXT, owner_id TEXT, question_sha256 TEXT, "
            "metadata_json TEXT, status TEXT)"
        )
        connection.execute(
            "INSERT INTO query_history VALUES (?, ?, ?, ?, ?)",
            (
                _TRACE_ID,
                "private-owner-id",
                "q" * 64,
                json.dumps({"conversation_context_present": True}),
                "ANSWERED",
            ),
        )
    identity = read_history_identity(
        _TRACE_ID, trace_db=trace_db, trace_container=None
    )
    assert identity["question_sha256"] == "q" * 64
    assert identity["conversation_context_present"] is True
    assert identity["owner_sha256"] != "private-owner-id"


def test_failed_24_catalog_shortcut_is_separate_from_pack_recall() -> None:
    _, cases, _ = load_truth()
    case = cases["WB08R-N-049"]
    row = {
        "case_id": case["case_id"],
        "truth_status": "VERIFIED",
        "expected_behavior": "ANSWER",
        "answer_shape": "CATALOG",
        "terminal_event_count": 1,
        "final_count": 1,
        "status": "ANSWERABLE",
        "answer_chars": 10,
        "citation_count": 1,
        "expected_source_in_citations": True,
        "answer_path": "CATALOG_FAST_PATH",
        "question_sha256": case["question_sha256"],
        "answer_sha256": "a" * 64,
    }
    judgment = {
        case["case_id"]: {
            "question_sha256": case["question_sha256"],
            "answer_sha256": "a" * 64,
            "unsupported_high_risk_fact_count": 0,
            "wrong_source_severe_error_count": 0,
            "template_body_overreach_count": 0,
            "structural_sibling_error_count": 0,
        }
    }
    result = _failed_24_gate([row], {case["case_id"]: case}, judgment)
    assert result["status"] == "PASSED"
    assert result["catalog_fast_path_case_ids"] == [case["case_id"]]

    pending = {
        **cases["WB08R-N-050"],
        "truth_status": "NEEDS_TRUTH_REVIEW",
        "expected_behavior": "NEEDS_TRUTH_REVIEW",
    }
    pending_row = {
        "case_id": pending["case_id"],
        "truth_status": "NEEDS_TRUTH_REVIEW",
    }
    result = _failed_24_gate(
        [row, pending_row],
        {case["case_id"]: case, pending["case_id"]: pending},
        judgment,
    )
    assert result["status"] == "NOT_REVIEWED"
    assert result["needs_truth_review_case_ids"] == [pending["case_id"]]


def test_full_96_requires_thresholds_and_bound_review() -> None:
    rows = []
    for group, frozen in _cases():
        behavior = frozen["expected_behavior"]
        rows.append(
            {
                "run_id": f"{group}/{frozen['case_id']}",
                "case_id": frozen["case_id"],
                "frozen_expected_behavior": behavior,
                "expected_source_document": frozen.get(
                    "expected_source_document"
                ),
                "status": "INSUFFICIENT_EVIDENCE"
                if behavior == "REFUSE"
                else "ANSWERABLE",
                "citation_count": 0 if behavior == "REFUSE" else 1,
                "expected_source_in_citations": behavior != "REFUSE",
                "question_sha256": frozen["question_sha256"],
                "answer_sha256": "a" * 64,
            }
        )
    summary = {
        "exactly_one_final_count": 96,
        "evidence_pack_source_recall": 0.9,
        "answer_or_limited_rate": 0.9,
        "false_refusal_rate": 0.1,
    }
    result = _full_96_gate(rows, summary, {})
    assert result["status"] == "NOT_REVIEWED"
    assert result["no_evidence_control_case_ids"] == ["WB08R-F-034"]
    rows[0]["status"] = "INSUFFICIENT_EVIDENCE"
    summary["evidence_pack_source_recall"] = 0.89
    assert _full_96_gate(rows, summary, {})["status"] == "FAILED"


def test_telemetry_reports_partial_stage_and_provider_coverage() -> None:
    rows = [
        {
            "request_total_ms": 100.0,
            "stage_timings_ms": {"plan": 20.0},
            "provider_call_counts": {"generation": 1},
            "repair_calls": 0,
        },
        {
            "request_total_ms": 200.0,
            "stage_timings_ms": "NOT_OBSERVED",
            "provider_call_counts": "NOT_OBSERVED",
            "repair_calls": "NOT_OBSERVED",
        },
    ]
    summary = _telemetry_summary(rows)
    assert summary["stage_latency_ms"]["request_total"]["p50_ms"] == 150.0
    assert (
        summary["stage_latency_ms"]["plan"]["observation_status"] == "PARTIAL"
    )
    assert (
        summary["provider_call_totals"]["generation"]["total"] == "NOT_OBSERVED"
    )
    assert summary["repair_calls"] == "NOT_OBSERVED"
