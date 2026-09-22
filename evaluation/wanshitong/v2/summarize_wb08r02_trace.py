"""只汇总候选 Trace 的结构组与调用计数，不导出问题或正文。"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def _percentile(values: list[float], fraction: float) -> float | None:
    """返回保守的最近秩分位数。"""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[
        min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    ]


def summarize(results: Path, trace_db: Path) -> dict[str, Any]:
    """按结果 Trace ID 读取结构与调用摘要。

    Args:
        results: 候选小样本 NDJSON，不含答案正文。
        trace_db: 同一候选实例的只读 History SQLite。

    Returns:
        样本级和逐题结构摘要。

    Raises:
        ValueError: 结果缺少 Trace ID 或对应结构 Trace。

    """
    rows = tuple(
        json.loads(line)
        for line in results.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    connection = sqlite3.connect(f"file:{trace_db}?mode=ro", uri=True)
    case_summaries: list[dict[str, Any]] = []
    group_times: list[float] = []
    try:
        for row in rows:
            trace_id = row.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id:
                raise ValueError(f"缺少 Trace ID：{row.get('case_id')}")
            events = tuple(
                (
                    name,
                    dict(json.loads(payload)["attributes"]),
                )
                for name, payload in connection.execute(
                    "SELECT event_name, payload_json FROM query_trace_events "
                    "WHERE trace_id=? AND event_name IN "
                    "('retrieval.evidence_group_build', 'retrieval.rerank', "
                    "'retrieval.generate') ORDER BY sequence",
                    (trace_id,),
                )
            )
            group_events = [
                attributes
                for name, attributes in events
                if name == "retrieval.evidence_group_build"
            ]
            if not group_events:
                raise ValueError(f"缺少结构组 Trace：{row['case_id']}")
            group_times.extend(
                float(attributes["build_elapsed_ms"])
                for attributes in group_events
            )
            diagnostics = [
                item
                for attributes in group_events
                for item in attributes.get("group_diagnostics", ())
            ]
            selected = [item for item in diagnostics if item["selected"]]
            rerank_modes = [
                attributes.get("mode")
                for name, attributes in events
                if name == "retrieval.rerank"
            ]
            provider_calls = [
                call
                for name, attributes in events
                if name == "retrieval.generate"
                for call in attributes.get("provider_calls", ())
            ]
            operations = Counter(
                str(call.get("operation")) for call in provider_calls
            )
            case_summaries.append(
                {
                    "case_id": row["case_id"],
                    "source_hit": row.get("expected_source_match"),
                    "answer_behavior": row["status"],
                    "total_latency_ms": row["request_total_ms"],
                    "group_types": dict(
                        sorted(
                            Counter(
                                item["group_type"] for item in selected
                            ).items()
                        )
                    ),
                    "selected_groups_complete": all(
                        item["complete"] for item in selected
                    ),
                    "selected_group_count": len(selected),
                    "max_group_member_count": max(
                        (item["member_count"] for item in selected),
                        default=0,
                    ),
                    "evidence_drop_reasons": dict(
                        sorted(
                            Counter(
                                item["drop_reason"]
                                for item in diagnostics
                                if item["drop_reason"]
                            ).items()
                        )
                    ),
                    "group_build_ms": [
                        attributes["build_elapsed_ms"]
                        for attributes in group_events
                    ],
                    "reranker_calls": len(rerank_modes),
                    "reranker_modes": rerank_modes,
                    "generation_calls": operations["generation"],
                }
            )
    finally:
        connection.close()
    latencies = [float(row["request_total_ms"]) for row in rows]
    return {
        "sample_count": len(rows),
        "answerable": sum(row["status"] == "ANSWERABLE" for row in rows),
        "source_hit": sum(
            row.get("expected_source_match") is True for row in rows
        ),
        "p95_total_ms": _percentile(latencies, 0.95),
        "p95_group_build_ms": _percentile(group_times, 0.95),
        "max_reranker_calls_per_question": max(
            (item["reranker_calls"] for item in case_summaries), default=0
        ),
        "max_generation_calls_per_question": max(
            (item["generation_calls"] for item in case_summaries), default=0
        ),
        "cases": case_summaries,
    }


def main() -> None:
    """输出仅含安全结构指标的 JSON。

    Args:
        无参数；命令行选项从当前进程读取。

    Returns:
        无返回值；安全摘要写入标准输出。

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--trace-db", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(summarize(args.results, args.trace_db), ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
