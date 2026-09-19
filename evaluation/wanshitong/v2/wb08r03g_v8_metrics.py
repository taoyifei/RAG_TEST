"""V8 的实际发送包与首次丢失阶段观测，不向运行时输入 Gold。"""

from __future__ import annotations

import math
import statistics
from typing import Any

_PAIR_LENGTH = 2


def _latency(samples: list[object]) -> dict[str, object]:
    values = sorted(
        float(value)
        for value in samples
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    )
    return {
        "sample_count": len(values),
        "missing_count": len(samples) - len(values),
        "p50_ms": statistics.median(values) if values else "NOT_OBSERVED",
        "p95_ms": values[math.ceil(len(values) * 0.95) - 1]
        if values
        else "NOT_OBSERVED",
    }


def performance_observation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """分开统计回答路径，不以快速拒答代表生成性能。

    Args:
        rows: 本次版本包中全部请求的真实观测。

    Returns:
        各路径样本数、端到端与事件时延；未知TTFT及排队保持未知。

    """
    groups: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "direct",
            "generate",
            "structural",
            "repair",
            "refuse",
            "unknown",
        )
    }
    for row in rows:
        if row.get("answer_path") == "DIRECT_EXTRACT":
            category = "direct"
        elif row.get("repair_calls", 0) not in {0, "NOT_OBSERVED", None}:
            category = "repair"
        elif row.get("status") in {
            "INSUFFICIENT_EVIDENCE",
            "AMBIGUOUS_NEEDS_CLARIFICATION",
        }:
            category = "refuse"
        elif row.get("answer_shape") in {
            "TABLE",
            "TABLE_INTERSECTION",
            "DUTIES",
        }:
            category = "structural"
        elif row.get("answer_path") in {
            "LLM_CLAIM_VALIDATED",
            "EXTRACTIVE_FALLBACK",
        }:
            category = "generate"
        else:
            category = "unknown"
        groups[category].append(row)
    return {
        category: {
            "request_count": len(members),
            "end_to_end": _latency(
                [row.get("request_total_ms") for row in members]
            ),
            "first_stage_event": _latency(
                [row.get("stage_status_first_ms") for row in members]
            ),
            "first_answer_event": _latency(
                [row.get("first_answer_event_ms") for row in members]
            ),
            "model_ttft": "NOT_OBSERVED",
            "queue_wait": "NOT_OBSERVED",
            "provider_calls_by_request": [
                row.get("provider_call_counts", "NOT_OBSERVED")
                for row in members
            ],
        }
        for category, members in groups.items()
    }


def _registry_failures(packet: dict[str, Any]) -> list[str]:
    aliases = packet.get("alias_to_support_key")
    if not isinstance(aliases, list):
        return ["ALIAS_MAP_NOT_OBSERVED"]
    if any(
        not isinstance(pair, list)
        or len(pair) != _PAIR_LENGTH
        or not all(isinstance(value, str) and value for value in pair)
        for pair in aliases
    ):
        return ["INVALID_ALIAS_MAP"]
    alias_map = dict(aliases)
    invalid = ["DUPLICATE_ALIAS"] if len(alias_map) != len(aliases) else []
    allowances = packet.get("per_atom_support_ids")
    if not isinstance(allowances, list) or any(
        not isinstance(pair, list)
        or len(pair) != _PAIR_LENGTH
        or not isinstance(pair[0], str)
        or not isinstance(pair[1], list)
        or not all(isinstance(alias, str) for alias in pair[1])
        for pair in allowances
    ):
        return [*invalid, "INVALID_ATOM_ALLOWANCE"]
    if any(not set(allowed) <= alias_map.keys() for _, allowed in allowances):
        invalid.append("ALLOWANCE_OUTSIDE_PACKET")
    return invalid


def _transport_observation(
    packet: dict[str, Any],
) -> tuple[set[str], list[str], list[str]]:
    invalid = _registry_failures(packet)
    missing: list[str] = []
    if invalid:
        return set(), invalid, missing
    sent_keys = {key for _, key in packet["alias_to_support_key"]}
    missing.extend(
        field.upper() + "_NOT_OBSERVED"
        for field in ("messages_sha256", "transport_body_sha256")
        if not packet.get(field)
    )
    sources = packet.get("support_sources")
    if not isinstance(sources, list) or not all(
        isinstance(source, dict) and isinstance(source.get("support_key"), str)
        for source in sources
    ):
        missing.append("SUPPORT_SOURCES_NOT_OBSERVED")
    elif {source["support_key"] for source in sources} != sent_keys:
        invalid.append("PACKET_SOURCE_REGISTRY_MISMATCH")
    if packet.get("evidence_level") != "TRANSPORT_SENT":
        invalid.append("TRANSPORT_NOT_SENT")
        sent_keys = set()
    return sent_keys, invalid, missing


def _publication_failures(
    publication: dict[str, Any], sent_keys: set[str]
) -> tuple[list[str], list[str]]:
    path = publication.get("publication_path")
    if path is None:
        return [], ["PUBLICATION_PATH_NOT_OBSERVED"]
    if path != "MODEL_VALIDATED_CLAIM":
        return [], []
    published = publication.get("published_support_keys")
    if not isinstance(published, list) or not all(
        isinstance(key, str) for key in published
    ):
        return [], ["PUBLISHED_SUPPORT_KEYS_NOT_OBSERVED"]
    return (
        ["PUBLISHED_MODEL_SUPPORT_NOT_SENT"]
        if not set(published) <= sent_keys
        else [],
        [],
    )


def packet_observation(
    events: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """按 attempt 保留发送身份，字段缺失不解释为零或成功。

    Args:
        events: 单次请求的安全Trace事件集合。

    Returns:
        发送观测、发布路径和缓存状态；事实支持另行评审。

    """
    rows = events.get("retrieval.claim_publication", [])
    publication = rows[-1] if rows else {}
    cache_rows = events.get("retrieval.cache", [])
    cache = cache_rows[-1] if cache_rows else {}
    cache_hit = (
        cache["result"] == "hit"
        if cache.get("result") in {"hit", "miss"}
        else publication.get("cache_hit", "NOT_OBSERVED")
    )
    packets = publication.get("prepared_packets")
    published = {
        "publication_path": publication.get("publication_path", "NOT_OBSERVED"),
        "published_support_keys": publication.get(
            "published_support_keys", "NOT_OBSERVED"
        ),
        "fact_coverage": "NOT_CERTIFIED",
    }
    if not isinstance(packets, list) or not packets:
        generated = publication.get("generation_called")
        return {
            "prepared_sent_status": (
                "N/A_SERVER_DIRECT"
                if generated is False
                and (
                    publication.get("published_claim_count", 0) > 0
                    or publication.get("published_support_ids")
                )
                else "N/A_NO_GENERATION"
                if generated is False
                else "NOT_OBSERVED"
            ),
            "cache_hit": cache_hit,
            "attempts": [],
            **published,
        }
    identities: set[tuple[str, str]] = set()
    invalid: list[str] = []
    missing: list[str] = []
    sent_keys: set[str] = set()
    for packet in packets:
        if not isinstance(packet, dict):
            invalid.append("INVALID_PACKET")
            continue
        request_id, attempt_id = (
            packet.get("request_id"),
            packet.get("attempt_id"),
        )
        if not (
            isinstance(request_id, str)
            and request_id
            and isinstance(attempt_id, str)
            and attempt_id
        ):
            invalid.append("ATTEMPT_IDENTITY_MISSING")
        elif (request_id, attempt_id) in identities:
            invalid.append("DUPLICATE_ATTEMPT_IDENTITY")
        else:
            identities.add((request_id, attempt_id))
        keys, packet_errors, packet_missing = _transport_observation(packet)
        sent_keys.update(keys)
        invalid.extend(packet_errors)
        missing.extend(packet_missing)
    publication_errors, publication_missing = _publication_failures(
        publication, sent_keys
    )
    invalid.extend(publication_errors)
    missing.extend(publication_missing)
    return {
        "prepared_sent_status": (
            "FAILED" if invalid else "NOT_OBSERVED" if missing else "OBSERVED"
        ),
        "packet_failures": invalid,
        "packet_missing_observations": missing,
        "cache_hit": cache_hit,
        "attempts": packets,
        **published,
    }


def first_loss(
    stages: list[dict[str, Any]],
    *,
    document_version_id: str,
    chunk_id: str,
) -> dict[str, str]:
    """只用评测器中的目标身份比较完整、有序的阶段集合。

    Args:
        stages: 按执行次序记录、声明总量和截断状态的候选集合。
        document_version_id: 评测器持有的目标文档版本。
        chunk_id: 评测器持有的目标块身份。

    Returns:
        首次丢失的阶段与原因，缺失观测不当作候选不存在。

    """
    seen = False
    for stage in stages:
        name = str(stage["stage"])
        candidates = stage.get("candidates")
        total = stage.get("total")
        if (
            stage.get("truncated") is not False
            or not isinstance(candidates, list)
            or type(total) is not int
            or total != len(candidates)
        ):
            return {"stage": name, "reason": "NOT_OBSERVED_COMPLETE_ID_SET"}
        present = any(
            item.get("chunk_id") == chunk_id
            and item.get("document_version_id") == document_version_id
            and item.get("selected", True) is True
            for item in candidates
        )
        if not present:
            return {
                "stage": name,
                "reason": (
                    str(stage.get("drop_reason", "DROP_REASON_NOT_OBSERVED"))
                    if seen
                    else "ABSENT_FROM_RAW_CHANNELS"
                ),
            }
        seen = True
    return {"stage": "NONE", "reason": "PRESENT_THROUGH_OBSERVED_STAGES"}
