"""仅对 Natural-60 的 24 条结构问题执行候选 A/B 抽查。"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import urllib.parse
from pathlib import Path
from typing import Any

from evaluation.wanshitong.v2.run_wb08r01_candidate import (
    _chat,
    _normalized,
    _percentile,
    _session,
)

_ROOT = Path(__file__).resolve().parent
# 全部 sequence/enumeration、全部 condition+time_limit，及跨问法的
# actor+time_limit 六例；仅是离线评测选择，不进入生产代码或索引。
_NATURAL_CASE_IDS = frozenset(
    {
        "WB08R-N-001",
        "WB08R-N-002",
        "WB08R-N-014",
        "WB08R-N-018",
        "WB08R-N-019",
        "WB08R-N-020",
        "WB08R-N-026",
        "WB08R-N-027",
        "WB08R-N-030",
        "WB08R-N-031",
        "WB08R-N-032",
        "WB08R-N-033",
        "WB08R-N-035",
        "WB08R-N-043",
        "WB08R-N-044",
        "WB08R-N-048",
        "WB08R-N-050",
        "WB08R-N-052",
        "WB08R-N-055",
        "WB08R-N-056",
        "WB08R-N-057",
        "WB08R-N-058",
        "WB08R-N-059",
        "WB08R-N-060",
    }
)
_LATENCY_CASE_IDS = frozenset(
    {
        "WB08R-F-039",
        "WB08R-F-045",
        "WB08R-F-015",
        "WB08R-F-017",
        "WB08R-F-013",
        "WB08R-F-048",
        "WB08R-N-043",
        "WB08R-N-059",
    }
)


def _cases(
    suite: str, selected_ids: frozenset[str] | None = None
) -> tuple[dict[str, Any], ...]:
    """读取冻结问题并核对 ID 与原题摘要。"""
    case_ids = _NATURAL_CASE_IDS if suite == "natural24" else _LATENCY_CASE_IDS
    if selected_ids is not None:
        if not selected_ids or not selected_ids <= case_ids:
            raise ValueError("抽查 ID 必须属于本阶段冻结子集。")
        case_ids = selected_ids
    filename = (
        "natural-60.ndjson" if suite == "natural24" else "latency-24.ndjson"
    )
    rows = tuple(
        json.loads(line)
        for line in (_ROOT / filename).read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    selected = tuple(row for row in rows if row["case_id"] in case_ids)
    if (
        len(selected) != len(case_ids)
        or {row["case_id"] for row in selected} != case_ids
    ):
        raise ValueError("Natural-60 结构抽查集身份不完整。")
    for row in selected:
        actual = hashlib.sha256(row["question"].encode()).hexdigest()
        if actual != row["question_sha256"]:
            raise ValueError(f"问题摘要失配：{row['case_id']}")
    return selected


def run(
    base_url: str,
    output: Path,
    *,
    suite: str = "natural24",
    selected_ids: frozenset[str] | None = None,
) -> None:
    """逐条独立会话记录状态、来源匹配与墙钟延迟。

    Args:
        base_url: 仅允许本机转发的隔离候选地址。
        output: 评测目录下的 NDJSON 结果路径。
        suite: 冻结的结构问题子集名称。
        selected_ids: 可选的子集内抽查 ID。

    Returns:
        无返回值；结果逐行写入指定文件。

    """
    if suite not in {"natural24", "latency8"}:
        raise ValueError("仅允许 Natural-24 或 Latency-8 结构子集。")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("仅允许本机转发的隔离候选端口。")
    if output.resolve().parent != (_ROOT / "results").resolve():
        raise ValueError("结果只能写入 evaluation/wanshitong/v2/results。")
    output.parent.mkdir(exist_ok=True)
    completed = (
        {
            json.loads(line)["case_id"]
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        if output.exists()
        else set()
    )
    for row in _cases(suite, selected_ids):
        case_id = row["case_id"]
        if case_id in completed:
            continue
        opener, csrf = _session(base_url)
        conversation_id = f"wb08r02-{case_id.lower()}"
        context = row.get("context_question")
        if isinstance(context, str) and context:
            _chat(opener, csrf, base_url, conversation_id, context)
        observed = _chat(
            opener, csrf, base_url, conversation_id, row["question"]
        )
        expected = row.get("expected_source_document")
        source_match = (
            any(
                _normalized(title) == _normalized(expected)
                for title in observed["citation_titles"]
            )
            if isinstance(expected, str) and expected
            else None
        )
        observed.pop("citation_titles")
        record = {
            "suite": suite,
            "case_id": case_id,
            "question_sha256": row["question_sha256"],
            "question_style": row["question_style"],
            "required_atoms": row["required_atoms"],
            "expected_source_match": source_match,
            **observed,
        }
        with output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(
            json.dumps(
                {
                    "case_id": case_id,
                    "status": record["status"],
                    "request_total_ms": record["request_total_ms"],
                    "citation_count": record["citation_count"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    records = tuple(
        json.loads(line)
        for line in output.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    latency = [item["request_total_ms"] for item in records]
    print(
        json.dumps(
            {
                "count": len(records),
                "answerable": sum(
                    item["status"] == "ANSWERABLE" for item in records
                ),
                "source_title_match": sum(
                    item["expected_source_match"] is True for item in records
                ),
                "p50_total_ms": statistics.median(latency) if latency else None,
                "p95_total_ms": _percentile(latency, 0.95),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    """解析只允许 loopback 候选的运行参数。

    Args:
        无参数；命令行选项从当前进程读取。

    Returns:
        无返回值；运行结果由 ``run`` 写入文件。

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--suite", choices=("natural24", "latency8"), default="natural24"
    )
    parser.add_argument("--case-ids", nargs="*")
    args = parser.parse_args()
    run(
        args.base_url.rstrip("/"),
        args.output,
        suite=args.suite,
        selected_ids=(frozenset(args.case_ids) if args.case_ids else None),
    )


if __name__ == "__main__":
    main()
