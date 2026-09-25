#!/usr/bin/env python3
"""在同一内网模型入口下分时测量 1、2、4 会话响应。"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from replay_ab import AClient, BClient, _load_cases, _result_summary
from smoke_ab import _write_new

LEVELS = (1, 2, 4)
REPEATS = 3
CASE_IDS = ("AB-001", "AB-005", "AB-009", "AB-013")


def _one(
    side: str,
    case: dict[str, Any],
    identity: dict[str, Any],
    agent_id: str,
    output: Path,
) -> dict[str, Any]:
    """独立会话问单轮开发题；失败保留为测量结果。"""
    started = time.monotonic()
    try:
        client = AClient() if side == "A" else BClient(identity, agent_id)
        setup_seconds = time.monotonic() - started
        timings: dict[str, float] = {}
        request_started = time.monotonic()
        raw = client.ask(case["turns"][0]["user_query"], timings)
        _write_new(output, raw)
        result = {
            "setup_seconds": round(setup_seconds, 3),
            **_result_summary(side, raw, time.monotonic() - request_started),
            **timings,
        }
    except Exception as error:
        result = {
            "request_error": type(error).__name__,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    return {"scenario_id": case["scenario_id"], "side": side, **result}


def main() -> None:
    """同一并发级别 A/B 分组执行，顺序逐轮交叉。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    cases, case_sha = _load_cases(args.cases)
    by_id = {case["scenario_id"]: case for case in cases}
    if any(by_id[case_id]["split"] != "development" for case_id in CASE_IDS):
        raise ValueError("性能题只能来自开发集")
    identity = json.loads(args.identity.read_text(encoding="utf-8"))
    agent_id = json.loads(args.agent.read_text(encoding="utf-8"))["id"]
    args.outdir.mkdir(mode=0o700, exist_ok=False)
    _write_new(
        args.outdir / "run.json",
        json.dumps(
            {
                "case_sha256": case_sha,
                "levels": LEVELS,
                "repeats": REPEATS,
                "case_ids": CASE_IDS,
            },
            indent=2,
        ).encode(),
    )
    groups: list[dict[str, Any]] = []
    for level in LEVELS:
        for repeat in range(1, REPEATS + 1):
            sides = ("A", "B") if repeat % 2 else ("B", "A")
            for side in sides:
                group_start = time.monotonic()
                rows = []
                with ThreadPoolExecutor(max_workers=level) as pool:
                    futures = [
                        pool.submit(
                            _one,
                            side,
                            by_id[case_id],
                            identity,
                            agent_id,
                            args.outdir
                            / f"L{level}-R{repeat}-{side}-{case_id}.sse",
                        )
                        for case_id in CASE_IDS[:level]
                    ]
                    rows.extend(
                        future.result() for future in as_completed(futures)
                    )
                group = {
                    "level": level,
                    "repeat": repeat,
                    "side": side,
                    "group_seconds": round(time.monotonic() - group_start, 3),
                    "results": sorted(rows, key=lambda row: row["scenario_id"]),
                }
                groups.append(group)
                print(
                    json.dumps(
                        {
                            "level": level,
                            "repeat": repeat,
                            "side": side,
                            "group_seconds": group["group_seconds"],
                            "completed": sum(
                                row.get("terminal", False) for row in rows
                            ),
                            "errors": sum(
                                bool(
                                    row.get("request_error")
                                    or row.get("protocol_error")
                                )
                                for row in rows
                            ),
                        }
                    ),
                    flush=True,
                )
                (args.outdir / "summary.json").write_text(
                    json.dumps(groups, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )


if __name__ == "__main__":
    main()
