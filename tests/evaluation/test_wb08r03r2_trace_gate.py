"""SAFE Trace 缺失、引用错配和未观测 Provider 调用必须失败。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from evaluation.wanshitong.v2 import trace_gate

_TRACE_ID = "trace_" + "a" * 32


def _events(quote: str) -> dict[str, list[dict[str, object]]]:
    return {
        "retrieval.context_resolution": [
            {
                "context_resolution_mode": "ORIGINAL",
                "context_resolution_confidence": "HIGH",
                "context_resolution_revision": "v1",
                "original_query_sha256": "a" * 64,
                "resolved_root_query_sha256": "a" * 64,
                "context_digest": "b" * 64,
                "referenced_turn_count": 0,
            }
        ],
        "retrieval.query_plan": [
            {
                "planner_called": False,
                "planner_protocol": "NONE",
                "planner_schema_revision": "v3",
                "planner_transport_timeout_ms": 8000,
                "planner_latency_ms": 0,
                "planner_input_tokens": 0,
                "planner_output_tokens": 0,
                "planner_finish_reason": None,
                "planner_failure_category": None,
                "planner_fallback_mode": None,
                "atom_count": 1,
            }
        ],
        "retrieval.embedding_accounting": [
            {
                "query_embedding_provider_call_count": 1,
                "query_embedding_batch_size": 2,
                "query_embedding_slot_id": "slot-a",
                "query_embedding_latency_ms": 30,
            }
        ],
        "retrieval.ownership_summary": [
            {
                "root_source_hit": True,
                "per_atom_source_hit": [["A1", True]],
                "retrieval_relevant_count": 1,
                "ownership_qualified_count": 1,
                "publishable_support_count": 1,
                "evidence_present_but_rejected": False,
                "alignment_reason_distribution": {},
                "direct_root_rescue_count": 0,
                "direct_atom_rescue_count": 0,
                "constraint_failure_count": 0,
                "relation_failure_count": 0,
            }
        ],
        "retrieval.claim_publication": [
            {
                "generated_claim_count": 1,
                "accepted_claim_count": 1,
                "published_claim_count": 1,
                "accepted_support_ids": ["S1"],
                "published_support_ids": ["S1"],
                "published_quote_sha256s": [
                    hashlib.sha256(quote.encode()).hexdigest()
                ],
                "claim_rejection_code_distribution": {},
                "generation_gap_count": 0,
                "final_atom_coverage": [["A1", "SUPPORTED"]],
                "false_limited_detected": False,
            }
        ],
    }


def test_accepted_claim_is_independent_of_public_claim_event(
    monkeypatch: object,
) -> None:
    quote = "合成来源原文。"
    monkeypatch.setattr(  # type: ignore[attr-defined]
        trace_gate, "_read_events", lambda *_args: _events(quote)
    )

    result = trace_gate.audit_trace(
        Path("unused.db"),
        _TRACE_ID,
        answer="合成事实。[S1]",
        citations=[{"quote": quote}],
        claim_event_count=0,
    )

    assert result["trace_contract_ok"] is True
    assert result["accepted_claim_count"] == 1


def test_missing_required_trace_field_is_case_failure(
    monkeypatch: object,
) -> None:
    events = _events("合成来源原文。")
    del events["retrieval.embedding_accounting"][0][
        "query_embedding_provider_call_count"
    ]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        trace_gate, "_read_events", lambda *_args: events
    )

    result = trace_gate.audit_trace(
        Path("unused.db"),
        _TRACE_ID,
        answer="合成事实。[S1]",
        citations=[{"quote": "合成来源原文。"}],
        claim_event_count=0,
    )

    assert result["trace_contract_ok"] is False
    assert "EMBEDDING_CALL_COUNT_INVALID" in result["trace_contract_errors"]


def test_accepted_claim_requires_matching_final_reference(
    monkeypatch: object,
) -> None:
    quote = "合成来源原文。"
    monkeypatch.setattr(  # type: ignore[attr-defined]
        trace_gate, "_read_events", lambda *_args: _events(quote)
    )

    result = trace_gate.audit_trace(
        Path("unused.db"),
        _TRACE_ID,
        answer="合成事实。[S2]",
        citations=[{"quote": "另一段来源。"}],
        claim_event_count=0,
    )

    assert result["trace_contract_ok"] is False
    assert "FINAL_SUPPORT_NOT_PUBLISHED" in result["trace_contract_errors"]
    assert "FINAL_CITATION_QUOTE_MISMATCH" in result["trace_contract_errors"]


def test_accepted_support_cannot_disappear_from_publication(
    monkeypatch: object,
) -> None:
    events = _events("合成来源原文。")
    events["retrieval.claim_publication"][0]["published_support_ids"] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        trace_gate, "_read_events", lambda *_args: events
    )

    result = trace_gate.audit_trace(
        Path("unused.db"),
        _TRACE_ID,
        answer="合成事实。[S1]",
        citations=[{"quote": "合成来源原文。"}],
        claim_event_count=0,
    )

    assert result["trace_contract_ok"] is False
    assert "ACCEPTED_SUPPORT_NOT_PUBLISHED" in result[
        "trace_contract_errors"
    ]


def test_not_observed_embedding_requires_reason(monkeypatch: object) -> None:
    events = _events("合成来源原文。")
    events["retrieval.embedding_accounting"][0][
        "query_embedding_provider_call_count"
    ] = "NOT_OBSERVED"
    monkeypatch.setattr(  # type: ignore[attr-defined]
        trace_gate, "_read_events", lambda *_args: events
    )

    result = trace_gate.audit_trace(
        Path("unused.db"),
        _TRACE_ID,
        answer="合成事实。[S1]",
        citations=[{"quote": "合成来源原文。"}],
        claim_event_count=0,
    )

    assert result["trace_contract_ok"] is False
    assert "EMBEDDING_NOT_OBSERVED_REASON_MISSING" in result[
        "trace_contract_errors"
    ]


def test_trace_reader_uses_persisted_query_events(tmp_path: Path) -> None:
    database = tmp_path / "trace.db"
    events = _events("合成来源原文。")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE query_trace_events ("
            "sequence INTEGER PRIMARY KEY, trace_id TEXT, "
            "event_name TEXT, payload_json TEXT)"
        )
        for name, attributes in events.items():
            connection.execute(
                "INSERT INTO query_trace_events "
                "(trace_id, event_name, payload_json) VALUES (?, ?, ?)",
                (
                    _TRACE_ID,
                    name,
                    json.dumps(
                        {"attributes": list(attributes[0].items())}
                    ),
                ),
            )

    result = trace_gate.audit_trace(
        database,
        _TRACE_ID,
        answer="合成事实。[S1]",
        citations=[{"quote": "合成来源原文。"}],
        claim_event_count=0,
    )

    assert result["trace_contract_ok"] is True
