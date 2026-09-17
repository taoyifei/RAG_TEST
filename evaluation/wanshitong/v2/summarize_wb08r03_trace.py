"""按候选 Trace 汇总 WB08R-03 的调用、逐原子状态和时延。"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def _percentile(values: list[float], fraction: float) -> float | None:
    """使用保守的最近秩分位数。"""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[
        min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    ]


def summarize(results: Path, trace_db: Path) -> dict[str, Any]:
    """只读取已完成样本的安全 Trace 属性，不导出问题或答案正文。"""
    records = tuple(
        json.loads(line)
        for line in results.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    connection = sqlite3.connect(f"file:{trace_db}?mode=ro", uri=True)
    cases: list[dict[str, Any]] = []
    try:
        for record in records:
            trace_id = record.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id:
                raise ValueError(f"缺少 Trace ID：{record['run_id']}")
            events: dict[str, list[dict[str, Any]]] = {}
            for event_name, payload_json in connection.execute(
                "SELECT event_name, payload_json FROM query_trace_events "
                "WHERE trace_id=? ORDER BY sequence",
                (trace_id,),
            ):
                attributes = dict(json.loads(payload_json)["attributes"])
                events.setdefault(event_name, []).append(attributes)
            if not events:
                raise ValueError(f"未找到候选 Trace：{record['run_id']}")
            plan = events.get("retrieval.query_plan", [])
            route = events.get("retrieval.query_embedding_route", [])
            reranks = events.get("retrieval.rerank", [])
            generation = events.get("retrieval.generate", [])
            grounding = events.get("retrieval.atom_grounding", [])
            coverage = events.get("retrieval.atom_coverage", [])
            correction = events.get("retrieval.corrective_retrieval", [])
            provider_calls = tuple(
                call
                for event in generation
                for call in event.get("provider_calls", ())
            )
            call_counts = Counter(
                {
                    operation: sum(
                        int(call.get("call_count", 0))
                        for call in provider_calls
                        if call.get("operation") == operation
                    )
                    for operation in ("generation", "query.interpret")
                }
            )
            route_call_counts = [
                int(event["call_count"])
                for event in route
                if isinstance(event.get("call_count"), int)
            ]
            cases.append(
                {
                    "run_id": record["run_id"],
                    "group": record["group"],
                    "status": record["status"],
                    "expected_behavior": record["expected_behavior"],
                    "source_hit": record["expected_source_match"],
                    "citation_count": record["citation_count"],
                    "public_citations_have_quotes": record[
                        "public_citations_have_quotes"
                    ],
                    "verbatim_copy_ratio": record["verbatim_copy_ratio"],
                    "request_total_ms": record["request_total_ms"],
                    "atom_count": plan[-1].get("atom_count") if plan else None,
                    "planner_calls": call_counts["query.interpret"],
                    "embedding_calls": (
                        sum(route_call_counts) if route_call_counts else None
                    ),
                    "embedding_batch_size": (
                        route[-1].get("batch_size") if route else None
                    ),
                    "reranker_calls": sum(
                        event.get("mode") == "provider" for event in reranks
                    ),
                    "generation_calls": call_counts["generation"],
                    "repair_calls": (
                        grounding[-1].get("repair_calls") if grounding else 0
                    ),
                    "atom_coverage": tuple(
                        (event.get("atom_id"), event.get("status"))
                        for event in coverage
                    ),
                    "correction_triggered": any(
                        event.get("correction_triggered")
                        for event in correction
                    ),
                    "corrective_elapsed_ms": max(
                        (
                            float(event.get("elapsed_ms", 0))
                            for event in correction
                        ),
                        default=0.0,
                    ),
                    "generation_reason": (
                        generation[-1].get("reason_code")
                        if generation
                        else None
                    ),
                    "generation_ms": sum(
                        float(call.get("elapsed_ms", 0))
                        for call in provider_calls
                        if call.get("operation") == "generation"
                    ),
                }
            )
    finally:
        connection.close()
    groups: dict[str, dict[str, Any]] = {}
    for group in ("formal54", "natural_complex18", "latency24"):
        selected = [row for row in cases if row["group"] == group]
        latencies = [float(row["request_total_ms"]) for row in selected]
        groups[group] = {
            "count": len(selected),
            "answerable": sum(
                row["status"] == "ANSWERABLE" for row in selected
            ),
            "source_hit": sum(row["source_hit"] is True for row in selected),
            "p50_total_ms": statistics.median(latencies) if latencies else None,
            "p95_total_ms": _percentile(latencies, 0.95),
            "max_planner_calls": max(
                (row["planner_calls"] for row in selected), default=0
            ),
            "max_embedding_calls_observed": max(
                (row["embedding_calls"] or 0 for row in selected), default=0
            ),
            "max_reranker_calls": max(
                (row["reranker_calls"] for row in selected), default=0
            ),
            "max_generation_calls": max(
                (row["generation_calls"] for row in selected), default=0
            ),
            "max_repair_calls": max(
                (row["repair_calls"] for row in selected), default=0
            ),
            "p95_corrective_ms": _percentile(
                [float(row["corrective_elapsed_ms"]) for row in selected],
                0.95,
            ),
        }
    return {"sample_count": len(cases), "groups": groups, "cases": cases}


def main() -> None:
    """从只读候选数据库和本地结果文件生成安全汇总。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--trace-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(summarize(args.results, args.trace_db), ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
