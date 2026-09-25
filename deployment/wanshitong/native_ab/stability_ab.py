#!/usr/bin/env python3
"""按冻结类别各选两题，隔离重复三轮衡量输出稳定性。"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import time
from pathlib import Path
from typing import Any

from replay_ab import (
    AClient,
    BClient,
    _event_objects,
    _load_cases,
    _result_summary,
)
from smoke_ab import _write_new

REPEATS = 3
PER_CATEGORY = 2
SEED = 20260926


def _answer_hash(side: str, raw: bytes) -> str | None:
    """仅记录最终可见正文哈希，避免在汇总中写入正文。"""
    events = _event_objects(raw)
    if side == "A":
        final = next(
            (x for x in reversed(events) if x.get("type") == "final"), {}
        )
        answer = final.get("answer") if final.get("published") else None
    else:
        complete = next(
            (
                x
                for x in reversed(events)
                if x.get("response_type") == "complete"
            ),
            {},
        )
        data = (
            complete.get("data")
            if isinstance(complete.get("data"), dict)
            else {}
        )
        answer = data.get("final_content")
    if not isinstance(answer, str) or not answer:
        return None
    return hashlib.sha256(answer.encode()).hexdigest()


def _select(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按类别和冻结 ID 选题，不依据模型结果挑样本。"""
    selected = []
    category_counts: dict[str, int] = {}
    for case in sorted(cases, key=lambda item: item["scenario_id"]):
        category = case["category"]
        if category_counts.get(category, 0) < PER_CATEGORY:
            selected.append(case)
            category_counts[category] = category_counts.get(category, 0) + 1
    if len(selected) != 12 or len(category_counts) != 6:  # noqa: PLR2004
        raise ValueError("稳定性集应为六类各两题")
    return selected


def main() -> None:
    """所有重复均新建会话，保持每题两轮历史边界。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    cases, case_sha = _load_cases(args.cases)
    selected = _select(cases)
    identity = json.loads(args.identity.read_text(encoding="utf-8"))
    agent_id = json.loads(args.agent.read_text(encoding="utf-8"))["id"]
    args.outdir.mkdir(mode=0o700, exist_ok=False)
    _write_new(
        args.outdir / "run.json",
        json.dumps(
            {
                "case_sha256": case_sha,
                "seed": SEED,
                "repeats": REPEATS,
                "scenario_ids": [case["scenario_id"] for case in selected],
            },
            indent=2,
        ).encode(),
    )
    generator = random.Random(SEED)  # noqa: S311 - 仅安排重复顺序。
    results = []
    for repeat in range(1, REPEATS + 1):
        for case in selected:
            case_id = case["scenario_id"]
            sides = ["A", "B"] if generator.getrandbits(1) else ["B", "A"]
            for side in sides:
                try:
                    client = (
                        AClient()
                        if side == "A"
                        else BClient(identity, agent_id)
                    )
                except Exception as error:
                    results.extend(
                        {
                            "repeat": repeat,
                            "scenario_id": case_id,
                            "turn_index": turn["turn_index"],
                            "side": side,
                            "setup_error": type(error).__name__,
                        }
                        for turn in case["turns"]
                    )
                    continue
                for turn in case["turns"]:
                    started = time.monotonic()
                    timings: dict[str, float] = {}
                    try:
                        raw = client.ask(turn["user_query"], timings)
                        path = args.outdir / (
                            f"R{repeat}-{case_id}-T{turn['turn_index']}-{side}.sse"
                        )
                        _write_new(path, raw)
                        result = {
                            **_result_summary(
                                side, raw, time.monotonic() - started
                            ),
                            **timings,
                            "answer_sha256": _answer_hash(side, raw),
                        }
                    except Exception as error:
                        result = {
                            "request_error": type(error).__name__,
                            "duration_seconds": round(
                                time.monotonic() - started, 3
                            ),
                        }
                    row = {
                        "repeat": repeat,
                        "scenario_id": case_id,
                        "turn_index": turn["turn_index"],
                        "side": side,
                        **result,
                    }
                    results.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                    (args.outdir / "summary.json").write_text(
                        json.dumps(results, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )


if __name__ == "__main__":
    main()
