"""只在 8289 候选上运行 WB08R-03F 来源真值评测。"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import re
import sqlite3
import statistics
import subprocess
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any

from evaluation.wanshitong.v2.run_wb08r03_candidate import (
    _append_private,
    _cases,
    _chat,
    _failed_observation,
    _session,
    _transport_retryable,
)

_ROOT = Path(__file__).resolve().parent
_TRUTH = _ROOT.parent / "wb08r03f-truth-v1"
_RESULTS = _ROOT / "results"
_TRACE_ID = re.compile(r"^trace_[0-9a-f]{32}$")
_CONTAINER_NAME = re.compile(r"^[a-zA-Z0-9_.-]+$")
_POLL_COUNT = 10
_POLL_SECONDS = 0.2
_CANDIDATE_PORT = 8289
_FULL_CASE_COUNT = 96
_EVIDENCE_PACK_12 = frozenset(
    {
        "WB08R-F-015",
        "WB08R-F-037",
        "WB08R-F-040",
        "WB08R-F-041",
        "WB08R-N-043",
        "WB08R-N-044",
        "WB08R-N-045",
        "WB08R-N-047",
        "WB08R-N-051",
        "WB08R-N-052",
        "WB08R-N-053",
        "WB08R-N-056",
    }
)
_CORE_ANSWER_16 = {
    "WB08R-N-044": "direct_duration",
    "WB08R-N-045": "direct_duration",
    "WB08R-N-047": "direct_fact",
    "WB08R-F-037": "responsible_party",
    "WB08R-F-040": "responsible_party",
    "WB08R-F-015": "enumeration",
    "WB08R-F-048": "duties",
    "WB08R-N-059": "duties",
    "WB08R-N-046": "procedure",
    "WB08R-N-054": "procedure",
    "WB08R-N-043": "short_followup",
    "WB08R-N-051": "short_followup",
    "WB08R-N-049": "template_catalog",
    "WB08R-F-049": "template_catalog",
    "WB08R-F-024": "template_body_refusal",
    "WB08R-F-034": "no_evidence_refusal",
}
_NATURAL_36 = frozenset(
    {
        "WB08R-N-001",
        "WB08R-N-003",
        "WB08R-N-006",
        "WB08R-N-011",
        "WB08R-N-016",
        "WB08R-N-018",
        "WB08R-N-019",
        "WB08R-N-022",
        "WB08R-N-025",
        "WB08R-N-027",
        "WB08R-N-031",
        "WB08R-N-033",
        "WB08R-N-034",
        "WB08R-N-035",
        "WB08R-N-036",
        "WB08R-N-038",
        "WB08R-N-040",
        "WB08R-N-043",
        "WB08R-N-044",
        "WB08R-N-045",
        "WB08R-N-047",
        "WB08R-N-049",
        "WB08R-N-051",
        "WB08R-N-052",
        "WB08R-N-053",
        "WB08R-N-054",
        "WB08R-N-055",
        "WB08R-N-056",
        "WB08R-N-059",
        "WB08R-N-060",
        "WB08R-A-001",
        "WB08R-A-007",
        "WB08R-A-012",
        "WB08R-A-013",
        "WB08R-A-016",
        "WB08R-A-025",
    }
)
_CONCURRENCY_4 = {
    "WB08R-F-040": "direct_fact",
    "WB08R-N-056": "compound",
    "WB08R-N-043": "short_followup",
    "WB08R-F-034": "safe_refusal",
}
_CONTAINER_TRACE_CODE = """
import json
import sqlite3
import sys

database = sqlite3.connect(
    "file:/data/universal-rag.sqlite3?mode=ro", uri=True, timeout=1
)
rows = database.execute(
    "SELECT event_name, payload_json FROM query_trace_events "
    "WHERE trace_id=? ORDER BY sequence",
    (sys.argv[1],),
).fetchall()
print(json.dumps(rows, ensure_ascii=False))
"""
_CONTAINER_HISTORY_CODE = """
import hashlib
import json
import sqlite3
import sys

database = sqlite3.connect(
    "file:/data/universal-rag.sqlite3?mode=ro", uri=True, timeout=1
)
row = database.execute(
    "SELECT owner_id, question_sha256, metadata_json, status "
    "FROM query_history WHERE trace_id=?",
    (sys.argv[1],),
).fetchone()
if row and row[3] == "STARTED":
    row = None
metadata = json.loads(row[2]) if row else {}
timings = metadata.get("diagnostics", {}).get("stage_timings")
stage_times = {}
if isinstance(timings, list):
    for item in timings:
        if isinstance(item, dict) and isinstance(item.get("stage"), str):
            elapsed = item.get("elapsed_ms")
            if (isinstance(elapsed, (int, float))
                    and not isinstance(elapsed, bool)):
                stage_times[item["stage"]] = (
                    stage_times.get(item["stage"], 0) + elapsed
                )
usage = metadata.get("provider_usage")
provider_counts = {}
if isinstance(usage, list):
    for item in usage:
        if isinstance(item, dict) and isinstance(item.get("operation"), str):
            count = item.get("call_count")
            if isinstance(count, int) and not isinstance(count, bool):
                provider_counts[item["operation"]] = count
print(json.dumps({
    "owner_sha256": hashlib.sha256(row[0].encode()).hexdigest(),
    "question_sha256": row[1],
    "conversation_context_present": metadata.get(
        "conversation_context_present"
    ),
    "stage_timings_ms": (
        stage_times if isinstance(timings, list) else "NOT_OBSERVED"
    ),
    "provider_call_counts": (
        provider_counts if isinstance(usage, list) else "NOT_OBSERVED"
    ),
} if row else {}, ensure_ascii=False))
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _normalized(value: str) -> str:
    return "".join(
        char.lower()
        for char in unicodedata.normalize("NFKC", value).replace("docx", "")
        if char.isalnum()
    )


def load_truth(  # noqa: PLR0912, PLR0915
    truth_dir: Path = _TRUTH,
) -> tuple[
    dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]
]:
    """核对真值文件、冻结题身份和逐字引文摘要。"""
    manifest = json.loads(
        (truth_dir / "manifest.json").read_text(encoding="utf-8")
    )
    files = (
        ("cases.ndjson", "cases_sha256"),
        ("evidence_truth.ndjson", "evidence_truth_sha256"),
    )
    for filename, digest_key in files:
        if _sha256(truth_dir / filename) != manifest[digest_key]:
            raise ValueError(f"TRUTH_DIGEST_MISMATCH:{filename}")
    for filename, expected_digest in manifest[
        "source_dataset_files_sha256"
    ].items():
        if _sha256(_ROOT / filename) != expected_digest:
            raise ValueError(f"FROZEN_DATASET_DIGEST_MISMATCH:{filename}")

    cases = {
        row["case_id"]: row for row in _read_ndjson(truth_dir / "cases.ndjson")
    }
    supports = {
        row["support_id"]: row
        for row in _read_ndjson(truth_dir / "evidence_truth.ndjson")
    }
    frozen = {
        row["case_id"]: row
        for filename in ("formal-54.ndjson", "natural-60.ndjson")
        for row in _read_ndjson(_ROOT / filename)
    }
    if len(cases) != manifest["case_count"]:
        raise ValueError("TRUTH_CASE_COUNT_MISMATCH")
    if len(supports) != manifest["evidence_count"]:
        raise ValueError("TRUTH_EVIDENCE_COUNT_MISMATCH")
    verified = 0
    review = 0
    used_support_ids: set[str] = set()
    for case_id, case in cases.items():
        original = frozen.get(case_id)
        if (
            original is None
            or case["question_sha256"] != original["question_sha256"]
        ):
            raise ValueError(f"TRUTH_CASE_IDENTITY_MISMATCH:{case_id}")
        if case["expected_source_documents"] != [
            original["expected_source_document"]
        ]:
            raise ValueError(f"TRUTH_SOURCE_IDENTITY_MISMATCH:{case_id}")
        if case["frozen_expected_behavior"] != original["expected_behavior"]:
            raise ValueError(f"TRUTH_BEHAVIOR_IDENTITY_MISMATCH:{case_id}")
        context = original.get("context_question") or ""
        expected_context_hash = (
            hashlib.sha256(context.encode()).hexdigest() if context else None
        )
        if case["context_question_sha256"] != expected_context_hash:
            raise ValueError(f"TRUTH_CONTEXT_IDENTITY_MISMATCH:{case_id}")
        if case["truth_status"] == "NEEDS_TRUTH_REVIEW":
            review += 1
            if (
                case["expected_behavior"] != "NEEDS_TRUTH_REVIEW"
                or case["gold_supports"]
                or case["required_fact_ids"]
            ):
                raise ValueError(f"REVIEW_CASE_HAS_GOLD:{case_id}")
            continue
        if case["truth_status"] != "VERIFIED" or case[
            "expected_behavior"
        ] not in {
            "ANSWER",
            "LIMITED",
            "REFUSE",
            "CLARIFY",
            "CORPUS_GAP",
        }:
            raise ValueError(f"TRUTH_STATUS_INVALID:{case_id}")
        verified += 1
        if not case["gold_supports"] and case["expected_behavior"] in {
            "ANSWER",
            "LIMITED",
        }:
            raise ValueError(f"SUPPORTED_CASE_HAS_NO_GOLD:{case_id}")
        supported_facts: set[str] = set()
        for gold in case["gold_supports"]:
            support_id = gold["support_id"]
            if support_id in used_support_ids:
                raise ValueError(f"DUPLICATE_GOLD_SUPPORT:{support_id}")
            used_support_ids.add(support_id)
            evidence = supports.get(support_id)
            if (
                evidence is None
                or evidence["case_id"] != case_id
                or evidence["document_version_id"]
                != gold["document_version_id"]
                or evidence["document_version_id"]
                not in case["expected_document_version_ids"]
                or evidence["locator"] != gold["locator"]
                or evidence["quote_sha256"] != gold["quote_sha256"]
                or evidence["is_citable"] is not True
                or "sha256:"
                + hashlib.sha256(evidence["quote"].encode()).hexdigest()
                != gold["quote_sha256"]
            ):
                raise ValueError(f"GOLD_SUPPORT_INVALID:{support_id}")
            quote = evidence["quote"]
            supported_facts.update(evidence["fact_ids"])
            if not all(value in quote for value in gold["required_literals"]):
                raise ValueError(f"GOLD_LITERAL_INVALID:{support_id}")
            if not all(value in quote for value in gold["required_roles"]):
                raise ValueError(f"GOLD_ROLE_INVALID:{support_id}")
            if not all(value in quote for value in gold["required_negations"]):
                raise ValueError(f"GOLD_NEGATION_INVALID:{support_id}")
        if not set(case["required_fact_ids"]).issubset(supported_facts):
            raise ValueError(f"REQUIRED_FACT_WITHOUT_GOLD:{case_id}")
    if (
        verified != manifest["verified_case_count"]
        or review != manifest["needs_truth_review_count"]
        or used_support_ids != supports.keys()
    ):
        raise ValueError("TRUTH_MANIFEST_COUNTS_MISMATCH")
    return manifest, cases, supports


def _parse_trace_rows(rows: list[list[str]]) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    for event_name, payload_json in rows:
        attributes = dict(json.loads(payload_json)["attributes"])
        events.setdefault(event_name, []).append(attributes)
    return events


def _trace_rows_from_file(path: Path, trace_id: str) -> list[list[str]]:
    database_uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=1) as connection:
        return [
            [event_name, payload_json]
            for event_name, payload_json in connection.execute(
                "SELECT event_name, payload_json FROM query_trace_events "
                "WHERE trace_id=? ORDER BY sequence",
                (trace_id,),
            )
        ]


def _trace_rows_from_container(
    container: str, trace_id: str
) -> list[list[str]]:
    if _CONTAINER_NAME.fullmatch(container) is None:
        raise ValueError("INVALID_TRACE_CONTAINER")
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "exec",
            container,
            "python",
            "-c",
            _CONTAINER_TRACE_CODE,
            trace_id,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def read_trace(
    trace_id: str | None,
    *,
    trace_db: Path | None,
    trace_container: str | None,
) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """从通用 Trace DB 只读获取候选来源身份；失败明确记录。"""
    if trace_id is None or _TRACE_ID.fullmatch(trace_id) is None:
        return {}, "INVALID_TRACE_ID"
    if (trace_db is None) == (trace_container is None):
        raise ValueError("REQUIRE_EXACTLY_ONE_TRACE_SOURCE")
    for attempt in range(_POLL_COUNT):
        try:
            rows = (
                _trace_rows_from_file(trace_db, trace_id)
                if trace_db is not None
                else _trace_rows_from_container(trace_container or "", trace_id)
            )
            events = _parse_trace_rows(rows)
        except (
            OSError,
            ValueError,
            KeyError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ):
            return {}, "TRACE_READ_FAILED"
        publication = _latest(events, "retrieval.claim_publication")
        if publication is not None and events.get("retrieval.complete"):
            if publication.get("shortcut_origin") in {
                "CATALOG_FAST_PATH",
                "CACHE_REPLAY",
            }:
                return events, None
            if events.get("retrieval.generation_evidence") and events.get(
                "retrieval.generate"
            ):
                return events, None
        if attempt + 1 < _POLL_COUNT:
            time.sleep(_POLL_SECONDS)
    return events, "TRACE_EVENTS_INCOMPLETE"


def read_history_identity(
    trace_id: str | None,
    *,
    trace_db: Path | None,
    trace_container: str | None,
) -> dict[str, Any]:
    """只读核对并发会话隔离，只公开 owner 摘要和问题摘要。"""
    if trace_id is None or _TRACE_ID.fullmatch(trace_id) is None:
        return {}
    for attempt in range(_POLL_COUNT):
        try:
            if trace_db is not None:
                with sqlite3.connect(
                    trace_db.resolve().as_uri() + "?mode=ro",
                    uri=True,
                    timeout=1,
                ) as connection:
                    row = connection.execute(
                        "SELECT owner_id, question_sha256, metadata_json, "
                        "status "
                        "FROM query_history WHERE trace_id=?",
                        (trace_id,),
                    ).fetchone()
                if row and row[3] == "STARTED":
                    row = None
                result = (
                    {
                        "owner_sha256": hashlib.sha256(
                            row[0].encode()
                        ).hexdigest(),
                        "question_sha256": row[1],
                        "conversation_context_present": json.loads(row[2]).get(
                            "conversation_context_present"
                        ),
                        "stage_timings_ms": _stage_timings(json.loads(row[2])),
                        "provider_call_counts": _provider_counts(
                            json.loads(row[2])
                        ),
                    }
                    if row
                    else {}
                )
            else:
                container = trace_container or ""
                if _CONTAINER_NAME.fullmatch(container) is None:
                    return {}
                process = subprocess.run(  # noqa: S603
                    [  # noqa: S607
                        "docker",
                        "exec",
                        container,
                        "python",
                        "-c",
                        _CONTAINER_HISTORY_CODE,
                        trace_id,
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                result = json.loads(process.stdout)
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            return {}
        if result:
            return result
        if attempt + 1 < _POLL_COUNT:
            time.sleep(_POLL_SECONDS)
    return {}


def _stage_timings(metadata: dict[str, Any]) -> dict[str, float] | str:
    """汇总同名重复检索阶段，缺少诊断时显式未知。"""
    diagnostics = metadata.get("diagnostics")
    timings = (
        diagnostics.get("stage_timings")
        if isinstance(diagnostics, dict)
        else None
    )
    if not isinstance(timings, list):
        return "NOT_OBSERVED"
    result: dict[str, float] = {}
    for item in timings:
        if not isinstance(item, dict):
            continue
        stage = item.get("stage")
        elapsed = item.get("elapsed_ms")
        if (
            isinstance(stage, str)
            and isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
        ):
            result[stage] = result.get(stage, 0.0) + elapsed
    return result


def _provider_counts(metadata: dict[str, Any]) -> dict[str, int] | str:
    """读取 History 中已结算的逐操作 Provider 调用次数。"""
    usage = metadata.get("provider_usage")
    if not isinstance(usage, list):
        return "NOT_OBSERVED"
    return {
        item["operation"]: item["call_count"]
        for item in usage
        if isinstance(item, dict)
        and isinstance(item.get("operation"), str)
        and isinstance(item.get("call_count"), int)
        and not isinstance(item["call_count"], bool)
    }


def _latest(
    events: dict[str, list[dict[str, Any]]], name: str
) -> dict[str, Any] | None:
    records = events.get(name)
    return records[-1] if records else None


def _same_gold_identity(
    source: dict[str, Any], evidence: dict[str, Any]
) -> bool:
    node_ids = source.get("node_ids")
    return (
        source.get("document_version_id") == evidence["document_version_id"]
        and source.get("chunk_id") == evidence["chunk_id"]
        and isinstance(node_ids, list)
        and evidence["node_id"] in node_ids
    )


def score_observation(  # noqa: PLR0912, PLR0915
    case: dict[str, Any],
    supports: dict[str, dict[str, Any]],
    observed: dict[str, Any],
    events: dict[str, list[dict[str, Any]]],
    trace_error: str | None = None,
) -> dict[str, Any]:
    """仅核算可机器确认的来源与终态，不把引用数量当 Claim 真值。"""
    citation_rows = observed.get("citations") or []
    expected_names = {
        _normalized(name)
        for name in case["expected_source_documents"]
        if isinstance(name, str) and name
    }
    cited_expected = any(
        _normalized(
            str(
                citation.get("document_title")
                or citation.get("document_name")
                or ""
            )
        )
        in expected_names
        for citation in citation_rows
        if isinstance(citation, dict)
    )
    result: dict[str, Any] = {
        "case_id": case["case_id"],
        "truth_status": case["truth_status"],
        "expected_behavior": case["expected_behavior"],
        "answer_shape": case.get("answer_shape"),
        "status": observed.get("status"),
        "reason_code": observed.get("reason_code"),
        "trace_id": observed.get("trace_id"),
        "trace_error": trace_error,
        "trace_original_query_sha256": "NOT_OBSERVED",
        "terminal_event_count": observed.get("terminal_event_count"),
        "final_count": observed.get("final_count"),
        "request_total_ms": observed.get("request_total_ms"),
        "citation_count": len(citation_rows),
        "answer_chars": len(observed.get("answer") or ""),
        "answer_sha256": hashlib.sha256(
            (observed.get("answer") or "").encode()
        ).hexdigest(),
        "expected_source_in_citations": cited_expected,
        "expected_source_in_pack": "NOT_OBSERVED",
        "gold_support_in_pack": "NOT_OBSERVED",
        "gold_hard_rejected": "NOT_OBSERVED",
        "structural_sibling_pollution_count": "NOT_OBSERVED",
        "structural_sibling_observation_status": "NOT_OBSERVED",
        "table_sibling_atom_conflict_count": "NOT_APPLICABLE",
        "accepted_claim_count": "NOT_OBSERVED",
        "published_claim_count": "NOT_OBSERVED",
        "extractive_fallback_used": "NOT_OBSERVED",
        "answer_path": "NOT_OBSERVED",
        "repair_calls": "NOT_OBSERVED",
        "nonempty_answer_with_support": "NOT_OBSERVED",
    }
    if case["truth_status"] == "NEEDS_TRUTH_REVIEW":
        result["truth_review_reason"] = case["notes"]
    context_event = _latest(events, "retrieval.context_resolution")
    if context_event is not None:
        query_hash = context_event.get("original_query_sha256")
        if isinstance(query_hash, str):
            result["trace_original_query_sha256"] = query_hash
    pack = _latest(events, "retrieval.generation_evidence")
    if pack is not None:
        observation_status = pack.get("structural_sibling_observation_status")
        if isinstance(observation_status, str) and observation_status in {
            "COMPLETE",
            "PARTIAL",
        }:
            result["structural_sibling_observation_status"] = observation_status
        pollution = pack.get("structural_sibling_pollution_count")
        if (
            isinstance(pollution, int)
            and not isinstance(pollution, bool)
            and pollution >= 0
        ):
            result["structural_sibling_pollution_count"] = pollution
    grounding = _latest(events, "retrieval.atom_grounding")
    if grounding is not None:
        repair_calls = grounding.get("repair_calls")
        if isinstance(repair_calls, int) and not isinstance(repair_calls, bool):
            result["repair_calls"] = repair_calls
    shortcut_publication = _latest(events, "retrieval.claim_publication")
    if shortcut_publication is not None and shortcut_publication.get(
        "shortcut_origin"
    ) in {"CATALOG_FAST_PATH", "CACHE_REPLAY"}:
        result["repair_calls"] = 0
    if case["truth_status"] == "NEEDS_TRUTH_REVIEW":
        return result
    admitted = pack.get("admitted_sources") if pack else None
    if isinstance(admitted, list):
        expected_versions = set(case["expected_document_version_ids"])
        result["expected_source_in_pack"] = any(
            isinstance(source, dict)
            and source.get("document_version_id") in expected_versions
            for source in admitted
        )
        gold = [supports[item["support_id"]] for item in case["gold_supports"]]
        if all(
            isinstance(source, dict)
            and isinstance(source.get("node_ids"), list)
            and isinstance(source.get("chunk_id"), str)
            for source in admitted
        ):
            result["gold_support_in_pack"] = all(
                any(_same_gold_identity(source, item) for source in admitted)
                for item in gold
            )
        rejected = pack.get("hard_rejected_sources")
        if isinstance(rejected, list) and all(
            isinstance(source, dict)
            and isinstance(source.get("node_ids"), list)
            and isinstance(source.get("chunk_id"), str)
            for source in rejected
        ):
            result["gold_hard_rejected"] = any(
                _same_gold_identity(source, item)
                for source in rejected
                for item in gold
            )
        gold_table_rows = {
            (item["document_version_id"], item["table_node_id"]): item[
                "table_row_index"
            ]
            for item in gold
            if item.get("table_node_id") is not None
            and item.get("table_row_index") is not None
        }
        if gold_table_rows:
            table_sources = [
                source
                for source in admitted
                if isinstance(source, dict)
                and (
                    source.get("document_version_id"),
                    source.get("table_node_id"),
                )
                in gold_table_rows
            ]
            if table_sources and all(
                isinstance(source.get("source_group_id"), str)
                and isinstance(source.get("table_group_id"), str)
                and isinstance(source.get("table_row_index"), int)
                and isinstance(source.get("linked_atom_ids"), list)
                for source in table_sources
            ):
                result["table_sibling_atom_conflict_count"] = sum(
                    source["table_row_index"]
                    not in {
                        0,  # 表头是目标行值的列语境，不是兄弟数据行。
                        gold_table_rows[
                            (
                                source["document_version_id"],
                                source["table_node_id"],
                            )
                        ],
                    }
                    and bool(source["linked_atom_ids"])
                    for source in table_sources
                )
            else:
                result["table_sibling_atom_conflict_count"] = "NOT_OBSERVED"
    publication = _latest(events, "retrieval.claim_publication")
    if publication is not None:
        shortcut = publication.get("shortcut_origin")
        if isinstance(shortcut, str):
            result["answer_path"] = shortcut
        accepted = publication.get("accepted_claim_count")
        if isinstance(accepted, int) and not isinstance(accepted, bool):
            result["accepted_claim_count"] = accepted
        published = publication.get("published_claim_count")
        if isinstance(published, int) and not isinstance(published, bool):
            result["published_claim_count"] = published
        fallback = publication.get("extractive_fallback_used")
        if isinstance(fallback, bool):
            result["extractive_fallback_used"] = fallback
    generation = _latest(events, "retrieval.generate")
    if generation is not None:
        path = generation.get("answer_path")
        if isinstance(path, str):
            result["answer_path"] = path
        if result["extractive_fallback_used"] == "NOT_OBSERVED" and isinstance(
            generation.get("mode"), str
        ):
            result["extractive_fallback_used"] = (
                generation["mode"] == "extractive_fallback"
            )
    if result["answer_path"] == "CATALOG_FAST_PATH":
        result["nonempty_answer_with_support"] = (
            observed.get("status") == "ANSWERABLE"
            and bool(observed.get("answer"))
            and len(citation_rows) == 1
            and cited_expected
        )
        return result
    if isinstance(result["accepted_claim_count"], int) or isinstance(
        result["extractive_fallback_used"], bool
    ):
        result["nonempty_answer_with_support"] = (
            observed.get("status") == "ANSWERABLE"
            and bool(observed.get("answer"))
            and len(citation_rows) > 0
            and cited_expected
            and (
                (
                    isinstance(result["accepted_claim_count"], int)
                    and result["accepted_claim_count"] > 0
                )
                or result["extractive_fallback_used"] is True
            )
        )
    return result


def _telemetry_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """只聚合已观测的阶段时延和调用数，保留观测覆盖。"""
    total = len(rows)

    def percentile(values: list[float], quantile: float) -> float:
        ordered = sorted(values)
        index = min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1)
        return round(
            ordered[index],
            2,
        )

    stage_names = {
        name
        for row in rows
        if isinstance(row.get("stage_timings_ms"), dict)
        for name in row["stage_timings_ms"]
    }
    stage_names.add("request_total")
    latency: dict[str, Any] = {}
    for stage in sorted(stage_names):
        values = [
            row["request_total_ms"]
            if stage == "request_total"
            else row["stage_timings_ms"].get(stage)
            if isinstance(row.get("stage_timings_ms"), dict)
            else None
            for row in rows
        ]
        observed = [
            float(value)
            for value in values
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        latency[stage] = {
            "observed_count": len(observed),
            "observation_status": (
                "COMPLETE"
                if len(observed) == total
                else "PARTIAL"
                if observed
                else "NOT_OBSERVED"
            ),
            "p50_ms": round(statistics.median(observed), 2)
            if observed
            else "NOT_OBSERVED",
            "p95_ms": percentile(observed, 0.95)
            if observed
            else "NOT_OBSERVED",
        }
    operations = {
        operation
        for row in rows
        if isinstance(row.get("provider_call_counts"), dict)
        for operation in row["provider_call_counts"]
    }
    provider_totals: dict[str, Any] = {}
    for operation in sorted(operations):
        counts = [
            row["provider_call_counts"][operation]
            for row in rows
            if isinstance(row.get("provider_call_counts"), dict)
            and operation in row["provider_call_counts"]
        ]
        provider_totals[operation] = {
            "total": sum(counts) if len(counts) == total else "NOT_OBSERVED",
            "observed_sum": sum(counts),
            "observed_count": len(counts),
        }
    repairs = [
        row["repair_calls"]
        for row in rows
        if isinstance(row.get("repair_calls"), int)
        and not isinstance(row["repair_calls"], bool)
    ]
    return {
        "stage_latency_ms": latency,
        "provider_call_totals": provider_totals,
        "repair_calls": sum(repairs)
        if len(repairs) == total
        else "NOT_OBSERVED",
        "repair_calls_observed_count": len(repairs),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """输出可审计分母；待审题不算成功或失败。"""
    verified = [row for row in rows if row["truth_status"] == "VERIFIED"]
    review = [
        row["case_id"]
        for row in rows
        if row["truth_status"] == "NEEDS_TRUTH_REVIEW"
    ]
    supported = [
        row
        for row in verified
        if row["expected_behavior"] in {"ANSWER", "LIMITED"}
    ]
    pack_supported = [
        row for row in supported if row.get("answer_shape") != "CATALOG"
    ]
    denominator = len(supported)
    pack_denominator = len(pack_supported)
    pack_observed = all(
        isinstance(row["expected_source_in_pack"], bool)
        for row in pack_supported
    )
    gold_observed = all(
        isinstance(row["gold_support_in_pack"], bool) for row in pack_supported
    )
    fallback_observed = all(
        isinstance(row["extractive_fallback_used"], bool) for row in supported
    )
    answer_observed = all(
        isinstance(row["nonempty_answer_with_support"], bool)
        for row in supported
    )
    source_hits = sum(
        row["expected_source_in_pack"] is True for row in pack_supported
    )
    gold_hits = sum(
        row["gold_support_in_pack"] is True for row in pack_supported
    )
    nonempty = sum(
        row["nonempty_answer_with_support"] is True for row in supported
    )
    false_refusals = sum(
        row["status"] == "INSUFFICIENT_EVIDENCE" for row in supported
    )
    fallback_count = sum(
        row["extractive_fallback_used"] is True for row in supported
    )
    pollution_observed = all(
        isinstance(row["structural_sibling_pollution_count"], int)
        and not isinstance(row["structural_sibling_pollution_count"], bool)
        and row["structural_sibling_observation_status"] == "COMPLETE"
        for row in supported
    )
    pollution_count = sum(
        row["structural_sibling_pollution_count"]
        for row in supported
        if isinstance(row["structural_sibling_pollution_count"], int)
        and not isinstance(row["structural_sibling_pollution_count"], bool)
    )

    def rate(
        count: int, observed: bool = True, *, pack: bool = False
    ) -> float | str:
        current_denominator = pack_denominator if pack else denominator
        if not current_denominator:
            return "NOT_APPLICABLE_NO_GOLD_SUPPORTED_CASES"
        return (
            round(count / current_denominator, 4)
            if observed
            else "NOT_OBSERVED"
        )

    return {
        "record_count": len(rows),
        "verified_count": len(verified),
        "needs_truth_review_case_ids": review,
        "exactly_one_final_count": sum(
            row["terminal_event_count"] == row["final_count"] == 1
            for row in rows
        ),
        "expected_source_in_pack_count": source_hits,
        "evidence_pack_source_recall": rate(
            source_hits, pack_observed, pack=True
        ),
        "expected_source_in_pack_not_observed": sum(
            row["expected_source_in_pack"] == "NOT_OBSERVED"
            for row in pack_supported
        ),
        "gold_support_in_pack_count": gold_hits,
        "evidence_pack_gold_recall": rate(gold_hits, gold_observed, pack=True),
        "gold_support_in_pack_not_observed": sum(
            row["gold_support_in_pack"] == "NOT_OBSERVED"
            for row in pack_supported
        ),
        "gold_hard_rejected_count": sum(
            row["gold_hard_rejected"] is True for row in supported
        ),
        "gold_hard_rejected_not_observed": sum(
            row["gold_hard_rejected"] == "NOT_OBSERVED" for row in supported
        ),
        "structural_sibling_pollution_count": (
            pollution_count if pollution_observed else "NOT_OBSERVED"
        ),
        "nonempty_answer_with_support_count": nonempty,
        "answer_or_limited_rate": rate(nonempty, answer_observed),
        "false_refusal_count": false_refusals,
        "false_refusal_rate": rate(false_refusals),
        "extractive_fallback_count": fallback_count,
        "extractive_fallback_rate": rate(fallback_count, fallback_observed),
        "supported_denominator": denominator,
        "pack_supported_denominator": pack_denominator,
        "unsupported_claim_severity": "NOT_OBSERVED_REQUIRES_CLAIM_REVIEW",
        **_telemetry_summary(rows),
    }


def _evidence_pack_12_gate(rows: list[dict[str, Any]]) -> str:
    """仅在 12 题全部可观测时给出 Gate A 判定。"""
    if (
        len(rows) != len(_EVIDENCE_PACK_12)
        or {row["case_id"] for row in rows} != _EVIDENCE_PACK_12
    ):
        return "NOT_OBSERVED_INCOMPLETE_CASE_SET"
    required = (
        "expected_source_in_pack",
        "gold_hard_rejected",
        "structural_sibling_pollution_count",
        "structural_sibling_observation_status",
    )
    if any(row[field] == "NOT_OBSERVED" for row in rows for field in required):
        return "NOT_OBSERVED_TRACE_FIELDS"
    if any(
        row["structural_sibling_observation_status"] != "COMPLETE"
        for row in rows
    ):
        return "NOT_OBSERVED_PARTIAL_GROUP_MAP"
    table_rows = [
        row for row in rows if row["case_id"] in {"WB08R-F-015", "WB08R-N-056"}
    ]
    if any(
        row["table_sibling_atom_conflict_count"] == "NOT_OBSERVED"
        for row in table_rows
    ):
        return "NOT_OBSERVED_TABLE_ROW_LINKS"
    if all(
        row["expected_source_in_pack"] is True
        and row["gold_hard_rejected"] is False
        and row["structural_sibling_pollution_count"] == 0
        and row["table_sibling_atom_conflict_count"] in {0, "NOT_APPLICABLE"}
        for row in rows
    ):
        return "PASSED"
    return "FAILED"


_REVIEW_FIELDS = (
    "unsupported_high_risk_fact_count",
    "wrong_source_severe_error_count",
    "template_body_overreach_count",
    "structural_sibling_error_count",
)


def _load_judgments(path: Path | None) -> dict[str, dict[str, Any]]:
    """只接受当前评测结果目录内逐题、逐答案摘要的人工判断。"""
    if path is None:
        return {}
    if path.resolve().parent != _RESULTS.resolve():
        raise ValueError("JUDGMENTS_MUST_STAY_IN_EVALUATION_DIRECTORY")
    records = _read_ndjson(path)
    judgments = {row.get("run_id") or row["case_id"]: row for row in records}
    if len(judgments) != len(records):
        raise ValueError("DUPLICATE_CLAIM_JUDGMENT")
    for case_id, row in judgments.items():
        if (
            not isinstance(row.get("reviewer"), str)
            or not row["reviewer"].strip()
            or not isinstance(row.get("question_sha256"), str)
            or not isinstance(row.get("answer_sha256"), str)
            or any(
                not isinstance(row.get(field), int)
                or isinstance(row[field], bool)
                or row[field] < 0
                for field in _REVIEW_FIELDS
            )
        ):
            raise ValueError(f"INVALID_CLAIM_JUDGMENT:{case_id}")
    return judgments


def _functional_gate(  # noqa: PLR0912
    rows: list[dict[str, Any]],
    expected_ids: frozenset[str],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """核对真实终态和来源；语义严重错误须绑定答案的人工审核。"""
    failures: list[str] = []
    review_gaps: list[str] = []
    if (
        len(rows) != len(expected_ids)
        or {row["case_id"] for row in rows} != expected_ids
    ):
        failures.append("CASE_SET_MISMATCH")
    for row in rows:
        case_id = row["case_id"]
        if (
            row.get("structural_sibling_observation_status") == "COMPLETE"
            and isinstance(row.get("structural_sibling_pollution_count"), int)
            and row["structural_sibling_pollution_count"] > 0
        ):
            failures.append(f"{case_id}:STRUCTURAL_SIBLING_POLLUTION")
        if row["terminal_event_count"] != 1 or row["final_count"] != 1:
            failures.append(f"{case_id}:TERMINAL_COUNT")
        behavior = row["frozen_expected_behavior"]
        if behavior in {"ANSWER", "LIMITED"}:
            if (
                row["status"] != "ANSWERABLE"
                or row["citation_count"] == 0
                or row["expected_source_in_citations"] is not True
                or row["answer_chars"] == 0
            ):
                failures.append(f"{case_id}:ANSWER_OR_SOURCE")
        elif behavior == "REFUSE":
            if row["status"] != "INSUFFICIENT_EVIDENCE":
                failures.append(f"{case_id}:SAFE_REFUSAL")
        elif behavior == "CLARIFY":
            if row["status"] != "AMBIGUOUS_NEEDS_CLARIFICATION":
                failures.append(f"{case_id}:CLARIFICATION")
        else:
            failures.append(f"{case_id}:UNSUPPORTED_BEHAVIOR")
        judgment = judgments.get(row.get("run_id", case_id)) or judgments.get(
            case_id
        )
        if judgment is None:
            review_gaps.append(f"{case_id}:MISSING_REVIEW")
        elif (
            judgment["question_sha256"] != row["question_sha256"]
            or judgment["answer_sha256"] != row["answer_sha256"]
        ):
            review_gaps.append(f"{case_id}:STALE_REVIEW")
        else:
            failures.extend(
                f"{case_id}:{field.upper()}"
                for field in _REVIEW_FIELDS
                if judgment[field] != 0
            )
    status = (
        "FAILED" if failures else "NOT_REVIEWED" if review_gaps else "PASSED"
    )
    return {
        "status": status,
        "case_count": len(rows),
        "exactly_one_final_count": sum(
            row["terminal_event_count"] == row["final_count"] == 1
            for row in rows
        ),
        "source_citation_count": sum(
            row["expected_source_in_citations"] is True
            for row in rows
            if row["frozen_expected_behavior"] in {"ANSWER", "LIMITED"}
        ),
        "failed_checks": failures,
        "semantic_review_gaps": review_gaps,
        "semantic_review_case_count": len(rows) - len(review_gaps),
    }


def _review_issues(
    row: dict[str, Any],
    judgments: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """按问题与本次答案摘要拒绝过期人工审阅。"""
    case_id = row["case_id"]
    judgment = judgments.get(row.get("run_id", case_id))
    if judgment is None:
        return [], [f"{row.get('run_id', case_id)}:MISSING_REVIEW"]
    if (
        judgment["question_sha256"] != row["question_sha256"]
        or judgment["answer_sha256"] != row["answer_sha256"]
    ):
        return [], [f"{row.get('run_id', case_id)}:STALE_REVIEW"]
    return [
        f"{row.get('run_id', case_id)}:{field.upper()}"
        for field in _REVIEW_FIELDS
        if judgment[field] != 0
    ], []


def _failed_24_gate(  # noqa: PLR0912
    rows: list[dict[str, Any]],
    truth_cases: dict[str, dict[str, Any]],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """目录捷径单列；其余 Gold 应答严格检查入包与发布。"""
    failures: list[str] = []
    unknown: list[str] = []
    review_gaps: list[str] = []
    if len(rows) != len(truth_cases) or {row["case_id"] for row in rows} != set(
        truth_cases
    ):
        failures.append("CASE_SET_MISMATCH")
    verified = [row for row in rows if row["truth_status"] == "VERIFIED"]
    for row in verified:
        case_id = row["case_id"]
        if row["terminal_event_count"] != 1 or row["final_count"] != 1:
            failures.append(f"{case_id}:TERMINAL_COUNT")
        behavior = row["expected_behavior"]
        if behavior in {"ANSWER", "LIMITED"}:
            if (
                row["status"] != "ANSWERABLE"
                or row["answer_chars"] == 0
                or row["citation_count"] == 0
                or row["expected_source_in_citations"] is not True
            ):
                failures.append(f"{case_id}:NONEMPTY_CORRECT_SOURCE_ANSWER")
            if row["answer_shape"] == "CATALOG":
                if row["answer_path"] != "CATALOG_FAST_PATH":
                    failures.append(f"{case_id}:CATALOG_FAST_PATH")
                if row["citation_count"] != 1:
                    failures.append(f"{case_id}:CATALOG_SINGLE_CITATION")
            else:
                if row["expected_source_in_pack"] == "NOT_OBSERVED":
                    unknown.append(f"{case_id}:PACK_SOURCE")
                elif row["expected_source_in_pack"] is not True:
                    failures.append(f"{case_id}:PACK_SOURCE")
                published = row["published_claim_count"]
                fallback = row["extractive_fallback_used"]
                if published == "NOT_OBSERVED" and fallback == "NOT_OBSERVED":
                    unknown.append(f"{case_id}:CLAIM_OR_FALLBACK")
                elif (
                    not (
                        isinstance(published, int)
                        and not isinstance(published, bool)
                        and published > 0
                    )
                    and fallback is not True
                ):
                    failures.append(f"{case_id}:CLAIM_OR_FALLBACK")
        elif behavior in {"REFUSE", "CORPUS_GAP"}:
            if row["status"] != "INSUFFICIENT_EVIDENCE":
                failures.append(f"{case_id}:SAFE_REFUSAL")
        elif behavior == "CLARIFY":
            if row["status"] != "AMBIGUOUS_NEEDS_CLARIFICATION":
                failures.append(f"{case_id}:CLARIFICATION")
        else:
            failures.append(f"{case_id}:UNKNOWN_EXPECTED_BEHAVIOR")
        bad, missing = _review_issues(row, judgments)
        failures.extend(bad)
        review_gaps.extend(missing)
    status = (
        "FAILED"
        if failures
        else "NOT_OBSERVED"
        if unknown
        else "NOT_REVIEWED"
        if review_gaps or len(verified) != len(truth_cases)
        else "PASSED"
    )
    return {
        "status": status,
        "verified_count": len(verified),
        "needs_truth_review_case_ids": [
            row["case_id"]
            for row in rows
            if row["truth_status"] == "NEEDS_TRUTH_REVIEW"
        ],
        "catalog_fast_path_case_ids": [
            row["case_id"]
            for row in verified
            if row["answer_shape"] == "CATALOG"
        ],
        "failed_checks": failures,
        "observation_gaps": unknown,
        "semantic_review_gaps": review_gaps,
    }


def _full_96_gate(
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    judgments: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """把数量阈值、控制题和答案语义审阅合成上线候选门禁。"""
    failures: list[str] = []
    unknown: list[str] = []
    review_gaps: list[str] = []
    if (
        len(rows) != _FULL_CASE_COUNT
        or len({row["run_id"] for row in rows}) != _FULL_CASE_COUNT
    ):
        failures.append("FULL_96_RECORD_SET")
    if summary["exactly_one_final_count"] != _FULL_CASE_COUNT:
        failures.append("FULL_96_TERMINAL_COUNT")
    thresholds = (
        ("evidence_pack_source_recall", 0.9, "min"),
        ("answer_or_limited_rate", 0.9, "min"),
        ("false_refusal_rate", 0.1, "max"),
    )
    for field, threshold, direction in thresholds:
        value = summary[field]
        if not isinstance(value, float):
            unknown.append(field.upper() + ":NOT_OBSERVED")
        elif (direction == "min" and value < threshold) or (
            direction == "max" and value > threshold
        ):
            failures.append(field.upper() + ":THRESHOLD")
    for row in rows:
        run_id = row["run_id"]
        if (
            row["frozen_expected_behavior"] in {"ANSWER", "LIMITED"}
            and row["status"] == "ANSWERABLE"
            and (
                row["citation_count"] == 0
                or row["expected_source_in_citations"] is not True
            )
        ):
            failures.append(f"{run_id}:WRONG_SOURCE_ANSWER")
        if row["frozen_expected_behavior"] == "REFUSE" and (
            row["status"] != "INSUFFICIENT_EVIDENCE"
        ):
            failures.append(f"{run_id}:UNSAFE_CONTROL_ANSWER")
        bad, missing = _review_issues(row, judgments)
        failures.extend(bad)
        review_gaps.extend(missing)
    status = (
        "FAILED"
        if failures
        else "NOT_OBSERVED"
        if unknown
        else "NOT_REVIEWED"
        if review_gaps
        else "PASSED"
    )
    return {
        "status": status,
        "failed_checks": failures,
        "observation_gaps": unknown,
        "semantic_review_gaps": review_gaps,
        "no_evidence_control_case_ids": [
            row["case_id"]
            for row in rows
            if row["frozen_expected_behavior"] == "REFUSE"
            and row.get("expected_source_document") is None
        ],
    }


def _concurrency_observations(
    selected: tuple[tuple[str, dict[str, Any]], ...],
    base_url: str,
) -> dict[str, dict[str, Any]]:
    """每个问题独立会话；上下文准备完后同时发起四个目标请求。"""
    barrier = threading.Barrier(len(selected), timeout=30)

    def worker(frozen: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        case_id = frozen["case_id"]
        conversation_id = "wb08r03f-concurrent-" + case_id.lower()
        try:
            opener, csrf = _session(base_url)
            context = frozen.get("context_question")
            if isinstance(context, str) and context:
                prelude = _chat(
                    opener, csrf, base_url, conversation_id, context
                )
                if prelude["final_count"] != 1:
                    return case_id, {
                        **prelude,
                        "error_code": "CONTEXT_NO_FINAL",
                    }
            barrier.wait()
            return case_id, _chat(
                opener,
                csrf,
                base_url,
                conversation_id,
                frozen["question"],
            )
        except Exception as error:
            return case_id, _failed_observation(error)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        return dict(executor.map(worker, (row for _, row in selected)))


def _concurrency_gate(  # noqa: PLR0912
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """核对四个独立请求的 Final、Trace 和 History 归属。"""
    failures: list[str] = []
    unknown: list[str] = []
    expected_count = len(_CONCURRENCY_4)
    if len(rows) != expected_count or {row["case_id"] for row in rows} != set(
        _CONCURRENCY_4
    ):
        failures.append("CASE_SET_MISMATCH")
    trace_ids = [row["trace_id"] for row in rows]
    owners = [row.get("history_owner_sha256") for row in rows]
    if len(set(trace_ids)) != expected_count or any(
        not isinstance(item, str) or _TRACE_ID.fullmatch(item) is None
        for item in trace_ids
    ):
        failures.append("TRACE_CROSS_SESSION_OR_MISSING")
    for row in rows:
        case_id = row["case_id"]
        if row["terminal_event_count"] != 1 or row["final_count"] != 1:
            failures.append(f"{case_id}:TERMINAL_COUNT")
        if row["status"] == "PROVIDER_UNAVAILABLE":
            failures.append(f"{case_id}:MODEL_UNAVAILABLE")
        behavior = row["frozen_expected_behavior"]
        if behavior == "ANSWER" and row["status"] != "ANSWERABLE":
            failures.append(f"{case_id}:ANSWER_STATUS")
        if behavior == "REFUSE" and row["status"] != "INSUFFICIENT_EVIDENCE":
            failures.append(f"{case_id}:SAFE_REFUSAL")
        if row["trace_original_query_sha256"] == "NOT_OBSERVED":
            unknown.append(f"{case_id}:TRACE_QUERY_IDENTITY")
        elif row["trace_original_query_sha256"] != row["question_sha256"]:
            failures.append(f"{case_id}:TRACE_QUERY_CROSS_SESSION")
        if not isinstance(row.get("history_owner_sha256"), str):
            unknown.append(f"{case_id}:HISTORY_OWNER")
        if row.get("history_question_sha256") is None:
            unknown.append(f"{case_id}:HISTORY_QUESTION")
        elif row["history_question_sha256"] != row["question_sha256"]:
            failures.append(f"{case_id}:HISTORY_QUERY_CROSS_SESSION")
        context_present = row.get("history_context_present")
        if not isinstance(context_present, bool):
            unknown.append(f"{case_id}:HISTORY_CONTEXT")
        elif context_present != (case_id == "WB08R-N-043"):
            failures.append(f"{case_id}:HISTORY_CONTEXT_CROSS_SESSION")
    if (
        len([owner for owner in owners if isinstance(owner, str)])
        == expected_count
        and len(set(owners)) != expected_count
    ):
        failures.append("HISTORY_OWNER_CROSS_SESSION")
    status = "FAILED" if failures else "NOT_OBSERVED" if unknown else "PASSED"
    return {
        "status": status,
        "failed_checks": failures,
        "observation_gaps": unknown,
    }


def run(  # noqa: PLR0912, PLR0913, PLR0915
    gate: str,
    output: Path,
    review_output: Path,
    *,
    base_url: str,
    trace_db: Path | None,
    trace_container: str | None,
    review_judgments: Path | None = None,
    case_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    """独立会话运行冻结题，并分开保存安全指标和私有正文。"""
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != _CANDIDATE_PORT
    ):
        raise ValueError("ONLY_8289_LOOPBACK_CANDIDATE_ALLOWED")
    if (trace_db is None) == (trace_container is None):
        raise ValueError("REQUIRE_EXACTLY_ONE_TRACE_SOURCE")
    if gate == "concurrency-4" and case_ids is not None:
        raise ValueError("CONCURRENCY_REQUIRES_ALL_FOUR_CASES")
    if (
        output.resolve().parent != _RESULTS.resolve()
        or review_output.resolve().parent != _RESULTS.resolve()
    ):
        raise ValueError("RESULTS_MUST_STAY_IN_EVALUATION_DIRECTORY")
    _, truth_cases, supports = load_truth()
    if case_ids is not None:
        selected = _cases(case_ids)
    elif gate == "evidence-pack-12":
        selected = _cases(_EVIDENCE_PACK_12)
    elif gate == "failed-24":
        selected = _cases(frozenset(truth_cases))
    elif gate == "core-answer-16":
        selected = _cases(frozenset(_CORE_ANSWER_16))
    elif gate == "natural-36":
        selected = _cases(_NATURAL_36)
    elif gate == "concurrency-4":
        selected = _cases(frozenset(_CONCURRENCY_4))
    elif gate == "full-96":
        selected = _cases()
    else:
        raise ValueError("UNKNOWN_GATE")
    _RESULTS.mkdir(exist_ok=True)
    completed = (
        {row["run_id"] for row in _read_ndjson(output)}
        if output.exists()
        else set()
    )
    if gate == "concurrency-4" and completed:
        raise ValueError("CONCURRENCY_REQUIRES_NEW_OUTPUT")
    concurrent = (
        _concurrency_observations(selected, base_url)
        if gate == "concurrency-4"
        else {}
    )
    for group, frozen in selected:
        run_id = f"{group}/{frozen['case_id']}"
        if run_id in completed:
            continue
        retry_count = 0
        if gate == "concurrency-4":
            observed = concurrent[frozen["case_id"]]
        else:
            conversation_id = "wb08r03f-" + frozen["case_id"].lower()
            while True:
                try:
                    opener, csrf = _session(base_url)
                    context = frozen.get("context_question")
                    if isinstance(context, str) and context:
                        context_result = _chat(
                            opener, csrf, base_url, conversation_id, context
                        )
                        if context_result["final_count"] != 1:
                            observed = {
                                **context_result,
                                "error_code": "CONTEXT_NO_FINAL",
                            }
                            break
                    observed = _chat(
                        opener,
                        csrf,
                        base_url,
                        conversation_id,
                        frozen["question"],
                    )
                    break
                except Exception as error:
                    if _transport_retryable(error) and retry_count == 0:
                        retry_count = 1
                        continue
                    observed = _failed_observation(error)
                    break
        events, trace_error = read_trace(
            observed.get("trace_id"),
            trace_db=trace_db,
            trace_container=trace_container,
        )
        case = truth_cases.get(frozen["case_id"])
        if case is None:
            expected_source = frozen.get("expected_source_document")
            case = {
                "case_id": frozen["case_id"],
                "truth_status": "NEEDS_TRUTH_REVIEW",
                "expected_behavior": "NEEDS_TRUTH_REVIEW",
                "expected_source_documents": (
                    [expected_source] if expected_source else []
                ),
                "gold_supports": [],
                "notes": "本题尚未建立 WB08R-03F 逐字来源真值。",
            }
        scored = score_observation(
            case, supports, observed, events, trace_error
        )
        history = read_history_identity(
            observed.get("trace_id"),
            trace_db=trace_db,
            trace_container=trace_container,
        )
        scored.update(
            {
                "stage_timings_ms": history.get(
                    "stage_timings_ms", "NOT_OBSERVED"
                ),
                "provider_call_counts": history.get(
                    "provider_call_counts", "NOT_OBSERVED"
                ),
            }
        )
        if gate == "concurrency-4":
            scored.update(
                {
                    "history_owner_sha256": history.get("owner_sha256"),
                    "history_question_sha256": history.get("question_sha256"),
                    "history_context_present": history.get(
                        "conversation_context_present"
                    ),
                }
            )
        scored.update(
            {
                "run_id": run_id,
                "gate": gate,
                "question_sha256": frozen["question_sha256"],
                "frozen_expected_behavior": frozen["expected_behavior"],
                "expected_source_document": frozen.get(
                    "expected_source_document"
                ),
                "question_style": frozen["question_style"],
                "gate_category": _CORE_ANSWER_16.get(frozen["case_id"])
                if gate == "core-answer-16"
                else _CONCURRENCY_4.get(frozen["case_id"])
                if gate == "concurrency-4"
                else None,
                "retry_count": retry_count,
                "error_code": observed.get("error_code"),
                "error_stage": observed.get("error_stage"),
            }
        )
        _append_private(
            review_output,
            {
                "run_id": run_id,
                "case_id": frozen["case_id"],
                "question_sha256": frozen["question_sha256"],
                "answer_sha256": scored["answer_sha256"],
                "question": frozen["question"],
                "answer": observed.get("answer"),
                "citations": observed.get("citations"),
            },
        )
        _append_private(output, scored)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": scored["status"],
                    "truth_status": scored["truth_status"],
                    "expected_source_in_pack": scored[
                        "expected_source_in_pack"
                    ],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    selected_ids = {f"{group}/{row['case_id']}" for group, row in selected}
    selected_rows = [
        row for row in _read_ndjson(output) if row["run_id"] in selected_ids
    ]
    summary = summarize(selected_rows)
    if gate == "evidence-pack-12":
        summary["evidence_pack_12_gate"] = _evidence_pack_12_gate(selected_rows)
    if gate in {"core-answer-16", "natural-36"}:
        expected = (
            frozenset(_CORE_ANSWER_16)
            if gate == "core-answer-16"
            else _NATURAL_36
        )
        summary["functional_gate"] = _functional_gate(
            selected_rows, expected, _load_judgments(review_judgments)
        )
    if gate == "failed-24":
        summary["failed_24_gate"] = _failed_24_gate(
            selected_rows,
            truth_cases,
            _load_judgments(review_judgments),
        )
    if gate == "full-96":
        summary["full_96_gate"] = _full_96_gate(
            selected_rows, summary, _load_judgments(review_judgments)
        )
    if gate == "concurrency-4":
        summary["concurrency_gate"] = _concurrency_gate(selected_rows)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return summary


def main() -> None:
    """解析候选地址和 Trace 的只读入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gate",
        choices=(
            "evidence-pack-12",
            "core-answer-16",
            "failed-24",
            "natural-36",
            "full-96",
            "concurrency-4",
        ),
        required=True,
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8289")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--review-judgments", type=Path)
    trace = parser.add_mutually_exclusive_group(required=True)
    trace.add_argument("--trace-db", type=Path)
    trace.add_argument("--trace-container")
    parser.add_argument("--case-id", action="append", default=[])
    args = parser.parse_args()
    run(
        args.gate,
        args.output,
        args.review_output,
        base_url=args.base_url.rstrip("/"),
        trace_db=args.trace_db,
        trace_container=args.trace_container,
        review_judgments=args.review_judgments,
        case_ids=frozenset(args.case_id) if args.case_id else None,
    )


if __name__ == "__main__":
    main()
