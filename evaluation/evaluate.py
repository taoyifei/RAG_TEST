"""运行冻结集评测并以退出码执行质量门槛。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from evaluation.dataset import load_dataset
from evaluation.metrics import (
    evaluate_results,
    load_active_evidence_manifest,
    load_results,
)


def main() -> int:
    """计算指标；缺题、缺人工评分或未达门槛时返回非零。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument(
        "--active-evidence-manifest",
        type=Path,
        required=True,
    )
    args = parser.parse_args()
    report = evaluate_results(
        load_dataset(args.dataset),
        load_results(args.results),
        active_evidence_manifest=load_active_evidence_manifest(
            args.active_evidence_manifest
        ),
    )
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
