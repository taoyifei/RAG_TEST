"""核验候选 SAFE Trace 与公共 Final 的逐题合同。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

_TRACE_ID = re.compile(r"^trace_[0-9a-f]{32}$")
_SUPPORT_ID = re.compile(r"\[S\d+\]")
_TRACE_SETTLE_ATTEMPTS = 10
_TRACE_SETTLE_INTERVAL_SECONDS = 0.2
_REQUIRED_EVENTS = (
    "retrieval.context_resolution",
    "retrieval.query_plan",
    "retrieval.embedding_accounting",
    "retrieval.ownership_summary",
    "retrieval.claim_publication",
)


def _read_events(
    trace_db: Path, trace_id: str
) -> dict[str, list[dict[str, Any]]]:
    """以只读连接等待当前查询的安全终态事件落盘。"""
    if _TRACE_ID.fullmatch(trace_id) is None:
        raise ValueError("INVALID_TRACE_ID")
    database_uri = trace_db.resolve().as_uri() + "?mode=ro"
    for attempt in range(_TRACE_SETTLE_ATTEMPTS):
        with sqlite3.connect(database_uri, uri=True, timeout=1) as connection:
            rows = connection.execute(
                "SELECT event_name, payload_json FROM query_trace_events "
                "WHERE trace_id=? ORDER BY sequence",
                (trace_id,),
            ).fetchall()
        events: dict[str, list[dict[str, Any]]] = {}
        for event_name, payload_json in rows:
            raw_attributes = json.loads(payload_json)["attributes"]
            try:
                attributes = dict(raw_attributes)
            except (TypeError, ValueError) as error:
                raise ValueError("INVALID_TRACE_ATTRIBUTES") from error
            if not all(isinstance(key, str) for key in attributes):
                raise ValueError("INVALID_TRACE_ATTRIBUTES")
            events.setdefault(event_name, []).append(attributes)
        if all(name in events for name in _REQUIRED_EVENTS):
            return events
        if attempt + 1 < _TRACE_SETTLE_ATTEMPTS:
            time.sleep(_TRACE_SETTLE_INTERVAL_SECONDS)
    return events


def _last(
    events: dict[str, list[dict[str, Any]]], name: str
) -> dict[str, Any]:
    items = events.get(name, ())
    return items[-1] if items else {}


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_fields(
    errors: list[str],
    attributes: dict[str, Any],
    names: tuple[str, ...],
    category: str,
) -> None:
    """一次性记录当前事件缺失的合同字段。"""
    errors.extend(
        f"{category}_FIELD_MISSING:{name}"
        for name in names
        if name not in attributes
    )


def _check_embedding(
    errors: list[str], embedding: dict[str, Any]
) -> object:
    """区分真实 0/1 次和带原因的未观测。"""
    call_count = embedding.get("query_embedding_provider_call_count")
    if call_count not in (0, 1, "NOT_OBSERVED") or isinstance(
        call_count, bool
    ):
        errors.append("EMBEDDING_CALL_COUNT_INVALID")
    if call_count == "NOT_OBSERVED" and not embedding.get(
        "query_embedding_not_observed_reason"
    ):
        errors.append("EMBEDDING_NOT_OBSERVED_REASON_MISSING")
    return call_count


def _check_claim_counts(
    errors: list[str], publication: dict[str, Any], claim_event_count: int
) -> tuple[object, object]:
    """验证内部 Claim 数和公共 Claim 事件的不同语义。"""
    count_fields = (
        "generated_claim_count",
        "accepted_claim_count",
        "published_claim_count",
    )
    errors.extend(
        "CLAIM_COUNT_INVALID:" + field
        for field in count_fields
        if field in publication and not _nonnegative_int(publication[field])
    )
    accepted_count = publication.get("accepted_claim_count")
    published_count = publication.get("published_claim_count")
    if (
        _nonnegative_int(accepted_count)
        and _nonnegative_int(published_count)
        and published_count > accepted_count
    ):
        errors.append("PUBLISHED_CLAIMS_EXCEED_ACCEPTED")
    if (
        claim_event_count
        and _nonnegative_int(published_count)
        and claim_event_count > published_count
    ):
        errors.append("PUBLIC_CLAIM_EVENTS_EXCEED_PUBLISHED")
    return accepted_count, published_count


def _check_final_references(
    errors: list[str],
    publication: dict[str, Any],
    accepted_count: object,
    answer: str,
    citations: list[dict[str, Any]],
) -> None:
    """使用安全 ID 与引文摘要交叉核对 Final。"""
    support_ids = publication.get("published_support_ids")
    if not isinstance(support_ids, list) or not all(
        isinstance(item, str) and item for item in support_ids
    ):
        errors.append("PUBLISHED_SUPPORT_IDS_INVALID")
        support_ids = []
    accepted_support_ids = publication.get("accepted_support_ids")
    if not isinstance(accepted_support_ids, list) or not all(
        isinstance(item, str) and item for item in accepted_support_ids
    ):
        errors.append("ACCEPTED_SUPPORT_IDS_INVALID")
    elif set(accepted_support_ids) - set(support_ids):
        errors.append("ACCEPTED_SUPPORT_NOT_PUBLISHED")
    final_support_ids = {value[1:-1] for value in _SUPPORT_ID.findall(answer)}
    if final_support_ids - set(support_ids):
        errors.append("FINAL_SUPPORT_NOT_PUBLISHED")
    if _nonnegative_int(accepted_count) and accepted_count > 0 and (
        not final_support_ids or not citations
    ):
        errors.append("ACCEPTED_CLAIM_MISSING_FINAL_CITATION")

    quote_hashes = publication.get("published_quote_sha256s")
    if not isinstance(quote_hashes, list) or not all(
        isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item)
        for item in quote_hashes
    ):
        errors.append("PUBLISHED_QUOTE_HASHES_INVALID")
        quote_hashes = []
    final_hashes = {
        _sha256(citation["quote"])
        for citation in citations
        if isinstance(citation, dict)
        and isinstance(citation.get("quote"), str)
    }
    if quote_hashes and set(quote_hashes) != final_hashes:
        errors.append("FINAL_CITATION_QUOTE_MISMATCH")
    if (
        _nonnegative_int(accepted_count)
        and accepted_count > 0
        and not quote_hashes
    ):
        errors.append("ACCEPTED_CLAIM_QUOTE_HASH_MISSING")


def audit_trace(
    trace_db: Path,
    trace_id: str | None,
    *,
    answer: str,
    citations: list[dict[str, Any]],
    claim_event_count: int,
) -> dict[str, Any]:
    """逐题读取 Trace；错误只进入当前题的安全错误码。"""
    errors: list[str] = []
    try:
        events = _read_events(trace_db, trace_id or "")
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        events = {}
        errors.append("TRACE_READ_FAILED")
    errors.extend(
        "TRACE_EVENT_MISSING:" + name
        for name in _REQUIRED_EVENTS
        if not events.get(name)
    )

    context = _last(events, "retrieval.context_resolution")
    plan = _last(events, "retrieval.query_plan")
    embedding = _last(events, "retrieval.embedding_accounting")
    ownership = _last(events, "retrieval.ownership_summary")
    publication = _last(events, "retrieval.claim_publication")
    _require_fields(
        errors,
        context,
        (
            "context_resolution_mode",
            "context_resolution_confidence",
            "context_resolution_revision",
            "original_query_sha256",
            "resolved_root_query_sha256",
            "context_digest",
            "referenced_turn_count",
        ),
        "CONTEXT",
    )
    _require_fields(
        errors,
        plan,
        (
            "planner_called",
            "planner_protocol",
            "planner_schema_revision",
            "planner_transport_timeout_ms",
            "planner_latency_ms",
            "planner_input_tokens",
            "planner_output_tokens",
            "planner_finish_reason",
            "planner_failure_category",
            "planner_fallback_mode",
            "atom_count",
        ),
        "PLANNER",
    )
    _require_fields(
        errors,
        embedding,
        (
            "query_embedding_provider_call_count",
            "query_embedding_batch_size",
            "query_embedding_slot_id",
            "query_embedding_latency_ms",
        ),
        "EMBEDDING",
    )
    _require_fields(
        errors,
        ownership,
        (
            "root_source_hit",
            "per_atom_source_hit",
            "retrieval_relevant_count",
            "ownership_qualified_count",
            "publishable_support_count",
            "evidence_present_but_rejected",
            "alignment_reason_distribution",
            "direct_root_rescue_count",
            "direct_atom_rescue_count",
            "constraint_failure_count",
            "relation_failure_count",
        ),
        "OWNERSHIP",
    )
    _require_fields(
        errors,
        publication,
        (
            "generated_claim_count",
            "accepted_claim_count",
            "published_claim_count",
            "accepted_support_ids",
            "published_support_ids",
            "published_quote_sha256s",
            "claim_rejection_code_distribution",
            "generation_gap_count",
            "final_atom_coverage",
            "false_limited_detected",
        ),
        "CLAIM",
    )
    call_count = _check_embedding(errors, embedding)
    accepted_count, published_count = _check_claim_counts(
        errors, publication, claim_event_count
    )
    _check_final_references(
        errors, publication, accepted_count, answer, citations
    )

    return {
        "trace_contract_ok": not errors,
        "trace_contract_errors": tuple(dict.fromkeys(errors)),
        "context_resolution_mode": context.get("context_resolution_mode"),
        "planner_failure_category": plan.get("planner_failure_category"),
        "planner_fallback_mode": plan.get("planner_fallback_mode"),
        "atom_count": plan.get("atom_count"),
        "query_embedding_provider_call_count": call_count,
        "query_embedding_batch_size": embedding.get(
            "query_embedding_batch_size"
        ),
        "root_source_hit": ownership.get("root_source_hit"),
        "per_atom_source_hit": ownership.get("per_atom_source_hit"),
        "retrieval_relevant_count": ownership.get(
            "retrieval_relevant_count"
        ),
        "ownership_qualified_count": ownership.get(
            "ownership_qualified_count"
        ),
        "publishable_support_count": ownership.get(
            "publishable_support_count"
        ),
        "evidence_present_but_rejected": ownership.get(
            "evidence_present_but_rejected"
        ),
        "accepted_claim_count": accepted_count,
        "published_claim_count": published_count,
        "generation_gap_count": publication.get("generation_gap_count"),
        "final_atom_coverage": publication.get("final_atom_coverage"),
        "false_limited_detected": publication.get("false_limited_detected"),
        "claim_rejection_code_distribution": publication.get(
            "claim_rejection_code_distribution"
        ),
    }
