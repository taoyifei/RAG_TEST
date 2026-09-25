#!/usr/bin/env python3
"""汇总隔离 A/B 的执行、引用与时延；未人工评审前不计算准确率。"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from make_review import _a_answer, _b_answer
from replay_ab import MAX_CASE_TURNS, _event_objects, _load_cases
from smoke_ab import _write_new


def _quantile(values: list[float], fraction: float) -> float | None:
    """以最近秩给出保守分位数，空样本返回空值。"""
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999) - 1)
    )
    return round(ordered[index], 3)


def _stream_identity(side: str, raw: bytes) -> str | None:
    """从 SSE 验证两轮确实复用同一侧会话。"""
    for event in _event_objects(raw):
        value = (
            event.get("conversation_id")
            if side == "A"
            else event.get("session_id")
        )
        if isinstance(value, str) and value:
            return value
    return None


def _side_summary(
    side: str,
    cases: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    run_dirs: list[Path],
) -> dict[str, Any]:
    """区分检索候选数与真正出现在答案中的引用数。"""
    this_side = [row for row in rows if row["side"] == side]
    timings = [
        float(row["duration_seconds"])
        for row in this_side
        if "duration_seconds" in row
    ]
    first_text = [
        float(row["first_text_seconds"])
        for row in this_side
        if "first_text_seconds" in row
    ]
    cited_turns = 0
    cited_refs = 0
    missing_files = []
    session_mismatches = []
    for case in cases:
        case_id = case["scenario_id"]
        identities = []
        for turn in case["turns"]:
            filename = f"{case_id}-T{turn['turn_index']}-{side}.sse"
            paths = [
                directory / filename
                for directory in run_dirs
                if (directory / filename).is_file()
            ]
            if len(paths) != 1:
                missing_files.append(filename)
                continue
            raw = paths[0].read_bytes()
            visible = (_a_answer(raw) if side == "A" else _b_answer(raw))[0]
            citations = visible["citations"]
            cited_turns += bool(citations)
            cited_refs += len(citations)
            identities.append(_stream_identity(side, raw))
        if len(case["turns"]) == MAX_CASE_TURNS and (
            len(identities) != MAX_CASE_TURNS or len(set(identities)) != 1
        ):
            session_mismatches.append(case_id)
    result: dict[str, Any] = {
        "requests": len(this_side),
        "terminal": sum(bool(row.get("terminal")) for row in this_side),
        "protocol_errors": sum(
            bool(row.get("protocol_error")) for row in this_side
        ),
        "request_errors": sum(
            bool(row.get("request_error")) for row in this_side
        ),
        "answer_with_inline_citation_turns": cited_turns,
        "inline_citation_count": cited_refs,
        "missing_raw_files": missing_files,
        "multiturn_session_mismatches": session_mismatches,
        "latency_seconds": {
            "median": round(statistics.median(timings), 3) if timings else None,
            "p90": _quantile(timings, 0.9),
            "p95": _quantile(timings, 0.95),
        },
        "first_text_seconds": {
            "samples": len(first_text),
            "median": round(statistics.median(first_text), 3)
            if first_text
            else None,
            "p90": _quantile(first_text, 0.9),
        },
    }
    if side == "A":
        result["statuses"] = dict(
            Counter(row.get("status") for row in this_side)
        )
        result["published_turns"] = sum(
            bool(row.get("published")) for row in this_side
        )
    else:
        result["nonempty_final_turns"] = sum(
            int(row.get("answer_chars") or 0) > 0 for row in this_side
        )
        result["retrieved_candidate_count"] = sum(
            int(row.get("references") or 0) for row in this_side
        )
    return result


def main() -> None:
    """保存不含业务正文的汇总数据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases, case_sha = _load_cases(args.cases)
    rows = []
    for directory in args.run_dir:
        rows.extend(
            json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        )
    expected = sum(len(case["turns"]) for case in cases) * 2
    if len(rows) != expected:
        raise ValueError(f"结果计数不完整：{len(rows)}/{expected}")
    output = {
        "case_sha256": case_sha,
        "scenarios": len(cases),
        "turns_per_side": expected // 2,
        "quality_review_status": "pending_user_labeled_review",
        "A": _side_summary("A", cases, rows, args.run_dir),
        "B": _side_summary("B", cases, rows, args.run_dir),
    }
    _write_new(
        args.output, json.dumps(output, ensure_ascii=False, indent=2).encode()
    )
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
