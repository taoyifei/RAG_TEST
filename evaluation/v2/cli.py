"""`scripts/dev.py` 使用的 P08 安全命令行入口。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from evaluation.v2.comparison import (
    compare_run_directories,
    summarize_run,
)
from evaluation.v2.dataset import load_dataset_directory
from evaluation.v2.runner import (
    LiveLaneBlockedError,
    RunOptions,
    run_evaluation,
)

P08_COMMANDS = frozenset(
    {
        "eval-validate-dataset",
        "eval-run",
        "eval-compare",
        "eval-report",
    }
)


def p08_command(arguments: Sequence[str]) -> int:
    """解析并执行一个 P08 默认离线命令。

    Args:
        arguments: 不含程序名的命令行参数。

    Returns:
        操作和门禁通过时为 0，阻塞或回归时为非零。

    """
    parsed = _parser().parse_args(arguments)
    if parsed.command == "eval-validate-dataset":
        dataset = load_dataset_directory(parsed.dataset)
        _print(
            {
                "dataset_id": dataset.manifest.dataset_id,
                "dataset_sha256": dataset.dataset_sha256,
                "cases": len(dataset.cases),
                "tuning": len(dataset.tuning_cases()),
                "holdout": len(dataset.holdout_cases()),
                "content_classification": (
                    dataset.manifest.content_classification
                ),
            }
        )
        return 0
    if parsed.command == "eval-compare":
        comparison = compare_run_directories(
            parsed.baseline, parsed.candidate
        )
        _print(comparison)
        return 0 if comparison["baseline_not_regressed"] is True else 1
    if parsed.command == "eval-report":
        _print(summarize_run(parsed.run))
        return 0
    try:
        outcome = run_evaluation(
            RunOptions(
                dataset=parsed.dataset,
                profile=parsed.profile,
                lane=parsed.lane,
                reports_root=parsed.reports_root,
                gates=parsed.gates,
                seed=parsed.seed,
                run_id=parsed.run_id,
                live_provider=parsed.live_provider,
                acknowledge_egress=parsed.acknowledge_egress,
                budget_requests=parsed.budget_requests,
                budget_tokens=parsed.budget_tokens,
            )
        )
    except LiveLaneBlockedError as error:
        _print({"status": str(error), "external_services_actually_called": []})
        return 2
    _print(
        {
            "run_id": outcome.manifest.run_id,
            "run_directory": str(outcome.run_directory),
            "dataset_sha256": outcome.manifest.dataset_sha256,
            "selected_candidate": outcome.manifest.selected_candidate,
            "gates_passed": outcome.gates.passed,
            "external_services_actually_called": [],
        }
    )
    return 0 if outcome.gates.passed else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python scripts/dev.py")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("eval-validate-dataset")
    validate.add_argument("--dataset", type=Path, required=True)
    run = commands.add_parser("eval-run")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--profile", type=Path, required=True)
    run.add_argument(
        "--lane",
        choices=("offline-structural", "live-primary", "live-standby"),
        required=True,
    )
    run.add_argument(
        "--reports-root",
        type=Path,
        default=Path("evaluation/reports"),
    )
    run.add_argument(
        "--gates",
        type=Path,
        default=Path("evaluation/gates/p08-gates.json"),
    )
    run.add_argument("--seed", type=int, default=20260903)
    run.add_argument("--run-id")
    run.add_argument("--live-provider", action="store_true")
    run.add_argument("--acknowledge-egress", action="store_true")
    run.add_argument("--budget-requests", type=int)
    run.add_argument("--budget-tokens", type=int)
    compare = commands.add_parser("eval-compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    report = commands.add_parser("eval-report")
    report.add_argument("--run", type=Path, required=True)
    return parser


def _print(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


__all__ = ["P08_COMMANDS", "p08_command"]
