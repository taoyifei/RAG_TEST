#!/usr/bin/env python3
"""逐题运行 12 条隔离 A/B 冒烟并保存私有流式原文。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from smoke_ab import _summary, _write_new, run_a, run_b

EXPECTED_SMOKE_COUNT = 12


def main() -> None:
    """按交替顺序配对执行；任何一侧无终态均使门禁失败。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if (
        len(cases) != EXPECTED_SMOKE_COUNT
        or len({item["id"] for item in cases}) != EXPECTED_SMOKE_COUNT
    ):
        raise ValueError("冒烟集必须恰好包含 12 个唯一题目")
    args.outdir.mkdir(mode=0o700, exist_ok=False)
    results: list[dict[str, object]] = []
    for index, case in enumerate(cases):
        for side in ("A", "B") if index % 2 == 0 else ("B", "A"):
            raw = (
                run_a(case["question"])
                if side == "A"
                else run_b(case["question"], args.identity, args.agent)
            )
            _write_new(args.outdir / f"{case['id']}-{side}.sse", raw)
            summary = _summary(raw)
            types = summary["payload_types"]
            terminal = "final" if side == "A" else "complete"
            passed = bool(types.get(terminal)) and not bool(types.get("error"))
            result = {
                "id": case["id"],
                "side": side,
                "passed": passed,
                **summary,
            }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    _write_new(
        args.outdir / "summary.json",
        json.dumps(results, ensure_ascii=False, indent=2).encode(),
    )
    if not all(item["passed"] for item in results):
        raise SystemExit("AB01 冒烟门禁失败")


if __name__ == "__main__":
    main()
