"""候选评测单题失败继续记录，并在批次结束后失败。"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

from evaluation.wanshitong.v2 import run_wb08r03_candidate as candidate


def _observed(*, final: bool) -> dict[str, object]:
    return {
        "trace_id": None,
        "status": "ANSWERABLE" if final else None,
        "reason_code": None,
        "terminal_event_count": 1 if final else 0,
        "terminal_type": "FINAL" if final else "MISSING",
        "error_code": None,
        "error_stage": None,
        "final_count": 1 if final else 0,
        "claim_count": 0,
        "claim_event_count": 0,
        "request_total_ms": 10.0,
        "stage_status_first_ms": None,
        "answer": "合成答案" if final else "",
        "citations": [{"quote": "合成来源原文。"}] if final else [],
    }


def _trace_audit(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {"trace_contract_ok": True, "trace_contract_errors": ()}


def test_runner_records_all_cases_after_semantic_failure(
    monkeypatch: object, tmp_path: Path
) -> None:
    rows = tuple(
        (
            "formal54",
            {
                "case_id": f"SYN-{index}",
                "question": "合成问题",
                "question_sha256": "synthetic",
                "question_style": "single",
                "expected_behavior": "ANSWER",
            },
        )
        for index in range(3)
    )
    monkeypatch.setattr(candidate, "_RESULTS", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(candidate, "_cases", lambda: rows)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        candidate, "_session", lambda _url: (object(), "csrf")
    )
    calls = 0

    def _chat(*_args: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _observed(final=calls != 1)

    monkeypatch.setattr(candidate, "_chat", _chat)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        candidate, "audit_trace", _trace_audit
    )
    output = tmp_path / "public.ndjson"
    review = tmp_path / "private.ndjson"
    result = candidate.run(
        "http://127.0.0.1:8289", output, review, tmp_path / "trace.db"
    )
    records = [json.loads(line) for line in output.read_text().splitlines()]

    assert result == 1
    assert len(records) == 3
    assert calls == 3
    assert records[0]["terminal_type"] == "MISSING"
    assert all(record["retry_count"] == 0 for record in records)


def test_transport_retry_is_explicit_and_bounded(
    monkeypatch: object, tmp_path: Path
) -> None:
    row = {
        "case_id": "SYN-1",
        "question": "合成问题",
        "question_sha256": "synthetic",
        "question_style": "single",
        "expected_behavior": "ANSWER",
    }
    monkeypatch.setattr(candidate, "_RESULTS", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        candidate, "_cases", lambda: (("formal54", row),)
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        candidate, "_session", lambda _url: (object(), "csrf")
    )
    calls = 0

    def _chat(*_args: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                "http://example.invalid", 502, "", None, None
            )
        return _observed(final=True)

    monkeypatch.setattr(candidate, "_chat", _chat)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        candidate, "audit_trace", _trace_audit
    )
    output = tmp_path / "public.ndjson"
    result = candidate.run(
        "http://127.0.0.1:8289",
        output,
        tmp_path / "private.ndjson",
        tmp_path / "trace.db",
    )
    record = json.loads(output.read_text().strip())

    assert result == 0
    assert calls == 2
    assert record["retry_count"] == 1


def test_runner_continues_after_missing_trace(
    monkeypatch: object, tmp_path: Path
) -> None:
    rows = tuple(
        (
            "formal54",
            {
                "case_id": f"SYN-{index}",
                "question": "合成问题",
                "question_sha256": "synthetic",
                "question_style": "single",
                "expected_behavior": "ANSWER",
            },
        )
        for index in range(2)
    )
    monkeypatch.setattr(candidate, "_RESULTS", tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setattr(candidate, "_cases", lambda: rows)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        candidate, "_session", lambda _url: (object(), "csrf")
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        candidate, "_chat", lambda *_args: _observed(final=True)
    )
    calls = 0

    def _audit(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "trace_contract_ok": calls == 2,
            "trace_contract_errors": (
                () if calls == 2 else ("TRACE_EVENT_MISSING",)
            ),
        }

    monkeypatch.setattr(candidate, "audit_trace", _audit)  # type: ignore[attr-defined]
    output = tmp_path / "public.ndjson"
    result = candidate.run(
        "http://127.0.0.1:8289",
        output,
        tmp_path / "private.ndjson",
        tmp_path / "trace.db",
    )
    records = [json.loads(line) for line in output.read_text().splitlines()]

    assert result == 1
    assert calls == 2
    assert len(records) == 2
    assert records[0]["trace_contract_errors"] == [
        "TRACE_EVENT_MISSING"
    ]
    assert records[1]["trace_contract_ok"] is True


def test_explicit_terminal_selection_includes_frozen_short_question() -> None:
    selected = candidate._cases(frozenset({"WB08R-N-003"}))
    assert len(selected) == 1
    assert selected[0][0] == "natural_terminal12"
    assert selected[0][1]["question_style"] == "short_ellipsis"


def test_explicit_claim_gate_selection_includes_frozen_adversarial() -> None:
    selected = candidate._cases(frozenset({"WB08R-A-007"}))
    assert len(selected) == 1
    assert selected[0][0] == "adversarial_audit"
    assert selected[0][1]["expected_behavior"] == "ANSWER"


def test_runner_uses_frozen_behavior_without_answer_table() -> None:
    check = candidate._expected_behavior_match
    assert check("ANSWER", "ANSWERABLE", "CLAIMS_VALIDATED", 1)
    assert not check("ANSWER", "ANSWERABLE", "LIMITED_ANSWER", 1)
    assert check("LIMITED", "ANSWERABLE", "LIMITED_ANSWER", 1)
    assert check("REFUSE", "INSUFFICIENT_EVIDENCE", None, 0)
    assert not check("REFUSE", "PROVIDER_UNAVAILABLE", None, 0)
    assert check("CLARIFY", "AMBIGUOUS_NEEDS_CLARIFICATION", None, 0)
