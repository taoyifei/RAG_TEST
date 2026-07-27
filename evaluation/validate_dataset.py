"""校验冻结题集结构、切分与 DOCX 原文证据。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.dataset import load_dataset, verify_source_evidence


def main() -> int:
    """执行只读核验并输出不含题目原文的计数。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--docs", type=Path, required=True)
    args = parser.parse_args()
    result = verify_source_evidence(
        load_dataset(args.dataset),
        args.docs,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
