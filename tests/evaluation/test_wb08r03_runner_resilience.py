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
        "citations": [],
    }


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
                "expected_behavior": "ANSWERABLE",
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
    output = tmp_path / "public.ndjson"
    review = tmp_path / "private.ndjson"
    result = candidate.run("http://127.0.0.1:8289", output, review)
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
        "expected_behavior": "ANSWERABLE",
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
    output = tmp_path / "public.ndjson"
    result = candidate.run(
        "http://127.0.0.1:8289", output, tmp_path / "private.ndjson"
    )
    record = json.loads(output.read_text().strip())

    assert result == 0
    assert calls == 2
    assert record["retry_count"] == 1


def test_explicit_terminal_selection_includes_frozen_short_question() -> None:
    selected = candidate._cases(frozenset({"WB08R-N-003"}))
    assert len(selected) == 1
    assert selected[0][0] == "natural_terminal12"
    assert selected[0][1]["question_style"] == "short_ellipsis"
