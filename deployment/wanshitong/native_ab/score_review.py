#!/usr/bin/env python3
"""读取用户导出的具名评审 JSON，计算可复核的配对完成率。"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from make_review import GRADES
from replay_ab import _load_cases
from smoke_ab import _write_new

BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20260925


def _success(grade: str, answerability: str) -> bool:
    """按冻结规则映射单轮业务有效完成。"""
    expected = (
        "supported_correct_complete"
        if answerability == "answerable"
        else "appropriate_insufficiency_or_clarification"
    )
    return grade == expected


def _interval(pairs: list[tuple[bool, bool]]) -> list[float] | None:
    """场景为抽样单位，配对自助法计算 B-A 差值区间。"""
    if not pairs:
        return None
    generator = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - 统计重采样可复现。
    differences = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sampled = [generator.choice(pairs) for _ in pairs]
        differences.append(
            sum(int(b) - int(a) for a, b in sampled) / len(sampled)
        )
    differences.sort()
    return [
        round(differences[int(BOOTSTRAP_SAMPLES * 0.025)], 4),
        round(differences[int(BOOTSTRAP_SAMPLES * 0.975) - 1], 4),
    ]


def _subset(
    cases: list[dict[str, Any]],
    scores: dict[str, Any],
    split: str,
) -> dict[str, Any]:
    """只把两侧全部必要轮次均评分的场景放入配对分母。"""
    selected = [case for case in cases if case["split"] == split]
    pairs = []
    missing = []
    grade_counts = {"A": Counter(), "B": Counter()}
    for case in selected:
        side_success = {}
        complete = True
        for side in ("A", "B"):
            turn_success = []
            for turn in case["turns"]:
                key = f"{case['scenario_id']}:T{turn['turn_index']}:{side}"
                grade = scores.get(key, {}).get("grade")
                if grade not in GRADES:
                    complete = False
                    missing.append(key)
                    continue
                grade_counts[side][grade] += 1
                turn_success.append(_success(grade, turn["answerability"]))
            side_success[side] = len(turn_success) == len(
                case["turns"]
            ) and all(turn_success)
        if complete:
            pairs.append((side_success["A"], side_success["B"]))
    n = len(pairs)
    wins = sum(not a and b for a, b in pairs)
    losses = sum(a and not b for a, b in pairs)
    ties = n - wins - losses
    return {
        "total_scenarios": len(selected),
        "fully_reviewed_scenarios": n,
        "missing_score_keys": missing,
        "A_success": sum(a for a, _ in pairs),
        "B_success": sum(b for _, b in pairs),
        "A_success_rate": round(sum(a for a, _ in pairs) / n, 4) if n else None,
        "B_success_rate": round(sum(b for _, b in pairs) / n, 4) if n else None,
        "B_minus_A": round((wins - losses) / n, 4) if n else None,
        "B_wins": wins,
        "A_wins": losses,
        "ties": ties,
        "paired_bootstrap_95pct": _interval(pairs),
        "turn_grade_counts": {
            side: dict(counts) for side, counts in grade_counts.items()
        },
        "full_split_review": n == len(selected),
    }


def main() -> None:
    """校验题库版本与评分字段，输出尚未评完时的明确缺口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases, case_sha = _load_cases(args.cases)
    score_file = json.loads(args.scores.read_text(encoding="utf-8"))
    if score_file.get("case_sha256") != case_sha:
        raise ValueError("评分题库哈希与冻结题库不一致")
    scores = score_file.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("评分文件缺少 scores 对象")
    result = {
        "case_sha256": case_sha,
        "score_file_sha256": hashlib.sha256(
            args.scores.read_bytes()
        ).hexdigest(),
        "review_mode": "user_labeled",
        "development": _subset(cases, scores, "development"),
        "holdout": _subset(cases, scores, "holdout"),
    }
    result["decision_ready"] = result["holdout"]["full_split_review"]
    _write_new(
        args.output, json.dumps(result, ensure_ascii=False, indent=2).encode()
    )
    print(
        json.dumps(
            {
                "decision_ready": result["decision_ready"],
                "development_reviewed": result["development"][
                    "fully_reviewed_scenarios"
                ],
                "holdout_reviewed": result["holdout"][
                    "fully_reviewed_scenarios"
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
