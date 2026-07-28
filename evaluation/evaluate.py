"""运行冻结集评测并以退出码执行质量门槛。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from evaluation.active_state import (
    add_active_state_arguments,
    load_trusted_active_evidence,
)
from evaluation.dataset import load_dataset
from evaluation.metrics import (
    evaluate_results,
    load_results,
)


def main() -> int:
    """计算指标；缺题、缺人工评分或未达门槛时返回非零。

    Args:
        无参数；命令行选项从当前进程读取。

    Returns:
        通过全部冻结门槛时返回 0，否则返回 1。

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    add_active_state_arguments(parser)
    args = parser.parse_args()
    report = evaluate_results(
        load_dataset(args.dataset),
        load_results(args.results),
        active_evidence_manifest=load_trusted_active_evidence(args),
    )
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
