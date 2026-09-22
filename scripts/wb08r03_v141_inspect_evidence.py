"""拆分并汇总 WB08R-03 V14.1 私有回放证据。

脚本只读取指定 NDJSON，不访问网络，也不修改原始 Trace。它会保留逐题完整
JSON，并抽取来源范围、候选损失账本、字段解析、AnswerPlan、发布结果和 Provider
调用，供离线人工复核。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

_FOCUS_EVENTS = (
    "retrieval.source_context",
    "retrieval.query_plan",
    "retrieval.scoped_document_structure",
    "retrieval.source_scope_candidate_ledger",
    "retrieval.structural",
    "retrieval.lexical",
    "retrieval.dense",
    "retrieval.field_resolution",
    "retrieval.generation_evidence",
    "retrieval.answer_binding_cache_identity",
    "retrieval.atom_grounding",
    "retrieval.claim_publication",
    "retrieval.generate",
    "retrieval.complete",
)
_PAIR_LENGTH = 2


def _sha256(path: Path) -> str:
    """计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(value: Any) -> Any:  # noqa: ANN401
    """把 canonical JSON 中的键值对列表还原为便于阅读的对象。"""
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if not isinstance(value, list):
        return value
    if value and all(
        isinstance(item, list)
        and len(item) == _PAIR_LENGTH
        and isinstance(item[0], str)
        for item in value
    ):
        keys = [item[0] for item in value]
        if len(set(keys)) == len(keys):
            return {key: _normalize(item) for key, item in value}
    return [_normalize(item) for item in value]


def _events(record: dict[str, Any]) -> tuple[list[str], dict[str, list[Any]]]:
    """读取 ``[event_name, payload]`` 形式的 Trace。"""
    sequence: list[str] = []
    grouped: dict[str, list[Any]] = {}
    for raw_event in record.get("trace_events", []):
        if (
            not isinstance(raw_event, list)
            or len(raw_event) != _PAIR_LENGTH
            or not isinstance(raw_event[0], str)
        ):
            continue
        event_name, payload = raw_event
        sequence.append(event_name)
        grouped.setdefault(event_name, []).append(_normalize(payload))
    return sequence, grouped


def _last(grouped: dict[str, list[Any]], event_name: str) -> dict[str, Any]:
    """返回某事件的最后一份对象载荷。"""
    values = grouped.get(event_name, [])
    if not values or not isinstance(values[-1], dict):
        return {}
    return values[-1]


def _pick(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    """按固定字段表抽取诊断载荷。"""
    return {key: payload.get(key) for key in keys}


def _compiled_plan(grounding: dict[str, Any]) -> dict[str, Any]:
    """取得 AnswerPlan 编译记录。"""
    records = grounding.get("answer_plan_records", [])
    if not isinstance(records, list):
        return {}
    return next(
        (
            item
            for item in records
            if isinstance(item, dict)
            and item.get("event") == "ANSWER_PLAN_COMPILED"
        ),
        {},
    )


def _case_summary(record: dict[str, Any]) -> dict[str, Any]:
    """抽取 V14.1 三段因果链及终态。"""
    sequence, grouped = _events(record)
    observed = record.get("observed", {})
    if not isinstance(observed, dict):
        observed = {}
    source = _last(grouped, "retrieval.source_context")
    plan = _last(grouped, "retrieval.query_plan")
    structure = _last(grouped, "retrieval.scoped_document_structure")
    field_resolution = _last(grouped, "retrieval.field_resolution")
    evidence = _last(grouped, "retrieval.generation_evidence")
    grounding = _last(grouped, "retrieval.atom_grounding")
    publication = _last(grouped, "retrieval.claim_publication")
    generated = _last(grouped, "retrieval.generate")
    completed = _last(grouped, "retrieval.complete")
    compiled = _compiled_plan(grounding)
    citations = observed.get("citations", [])
    if not isinstance(citations, list):
        citations = []
    ledgers = grouped.get("retrieval.source_scope_candidate_ledger", [])
    channel_ledgers = [
        _pick(
            item,
            "logical_channel",
            "scope_pushdown_applied",
            "catalog_complete",
            "store_returned_count",
            "after_access_filter_count",
            "after_defense_count",
            "deduplicated_count",
            "rejected_counts",
            "allowed_identity_digests",
            "returned_identity_digests",
        )
        for item in ledgers
        if isinstance(item, dict)
    ]
    return {
        "case_id": record.get("case_id"),
        "question": record.get("question"),
        "observed": {
            "trace_id": observed.get("trace_id"),
            "status": observed.get("status"),
            "reason_code": observed.get("reason_code"),
            "answer": observed.get("answer"),
            "citations": citations,
            "citation_count": len(citations),
            "final_count": observed.get("final_count"),
            "request_total_ms": observed.get("request_total_ms"),
        },
        "source_context": _pick(
            source,
            "resolution_stage",
            "source_intent",
            "resolution",
            "allowed_document_count",
            "source_catalog_complete",
            "source_resolution_required",
            "source_scope_digest",
        ),
        "query_plan": _pick(
            plan,
            "plan_id",
            "atom_count",
            "planner_called",
            "reason_code",
            "source_scopes",
        ),
        "scoped_document_structure": _pick(
            structure,
            "atom_id",
            "entry_available",
            "complete",
            "catalog_complete",
            "item_count",
            "next_cursor",
            "identity_digests",
            "scope_digest",
        ),
        "candidate_ledgers": channel_ledgers,
        "field_resolution": _pick(
            field_resolution,
            "attempted",
            "candidate_count",
            "candidate_identity_digests",
            "discarded_incomplete_schema_candidate_count",
            "schema_complete_atom_ids",
            "deferred_from_pre_schema_interpret",
            "resolutions",
            "reason_code",
            "failure_category",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "finish_reason",
            "transport_timeout_ms",
        ),
        "evidence": _pick(
            evidence,
            "generation_evidence_count",
            "atom_candidate_count",
            "per_atom_candidate_count",
            "pre_generation_availability_by_atom",
            "hard_reject_reason_distribution",
            "missing_atom_ids",
            "pack_revision",
        ),
        "answer_plan": {
            "answer_plan_id": grounding.get("answer_plan_id"),
            "field_resolutions": compiled.get("field_resolutions"),
            "selection_digests": compiled.get("selection_digests"),
            "qualifier_requirements": compiled.get("qualifier_requirements"),
            "task_modes": compiled.get("task_modes"),
            "coverage": grounding.get("answer_plan_coverage"),
            "target_member_coverage": grounding.get("target_member_coverage"),
            "generation_calls": grounding.get("generation_calls"),
            "relation_review_calls": grounding.get("relation_review_calls"),
        },
        "publication": _pick(
            publication,
            "answer_path",
            "publication_path",
            "generated_claim_count",
            "accepted_claim_count",
            "published_claim_count",
            "published_support_ids",
            "final_atom_coverage",
            "final_coverage_by_atom",
        ),
        "provider_execution": _pick(
            generated,
            "generation_called",
            "provider_call_count_by_operation",
            "provider_calls",
            "reason_code",
        ),
        "complete": completed,
        "event_sequence": sequence,
        "focus_events": {
            event_name: grouped.get(event_name, [])
            for event_name in _FOCUS_EVENTS
        },
    }


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="拆分并汇总 V14.1 private-replay.ndjson。"
    )
    parser.add_argument("trace", type=Path, help="private-replay.ndjson 路径")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="派生文件输出目录；不会修改原始 Trace。",
    )
    return parser.parse_args()


def main() -> int:
    """生成逐 case 完整 JSON 和机器可读因果摘要。"""
    args = _parse_args()
    trace_path = args.trace.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError("输出目录必须是尚不存在的新目录。")
    cases_dir = output_dir / "cases"
    cases_dir.mkdir(parents=True)

    records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summaries: dict[str, Any] = {}
    for record in records:
        case_id = str(record.get("case_id", "UNKNOWN"))
        (cases_dir / f"{case_id}.full.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summaries[case_id] = _case_summary(record)

    unsafe = summaries.get("UNSAFE_TEMPORAL", {})
    artifact = {
        "schema_version": "wb08r03-v14.1-c5-analysis-v1",
        "source_trace": trace_path.name,
        "source_trace_sha256": _sha256(trace_path),
        "record_count": len(records),
        "focus": {
            "controls": [
                "F024",
                "N031",
                "EXPLICIT_SOURCE",
                "EXPLICIT_COLUMN",
            ],
            "p0_case": "UNSAFE_TEMPORAL",
            "expected_contract": (
                "发布来源支持的基础输入项；BEFORE/MUST 保持 NOT_ESTABLISHED，"
                "内部 PARTIAL、对外 ANSWERABLE。"
            ),
            "observed_contract_failure": (
                "范围内候选与完整结构证据均存在，但后移的 query.interpret "
                "被 Provider 以 HTTP 400 拒绝；字段解析保守回到 AMBIGUOUS，"
                "最终 selection_digests 为空并返回零引用拒答。"
            ),
            "unsafe_temporal": unsafe,
        },
        "cases": summaries,
    }
    summary_path = output_dir / "analysis-summary.json"
    summary_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已读取 {len(records)} 条记录。")
    print(f"原始 Trace SHA-256: {artifact['source_trace_sha256']}")
    print(f"分析摘要: {summary_path}")
    print(f"逐题完整 JSON: {cases_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
