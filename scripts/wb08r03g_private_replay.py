"""只重放 WB08R-03G-P0 的四个冻结题并分离私有与 SAFE 结果。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evaluation.wanshitong.v2.run_wb08r03_candidate import (
    _cases,
    _chat,
    _session,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CASE_IDS = (
    "WB08R-N-031",
    "WB08R-N-033",
    "WB08R-F-015",
    "WB08R-F-013",
)
_CANDIDATE_PORT = 8289
_TRACE_TABLES = ("query_history", "query_trace_events")
_TRACE_COLUMNS = {
    "query_history": frozenset({"trace_id", "question_sha256", "status"}),
    "query_trace_events": frozenset(
        {"trace_id", "sequence", "event_name", "payload_json"}
    ),
}
_SAFE_TRACE_FIELDS = {
    "retrieval.generation_evidence": frozenset(
        {
            "admitted_sources",
            "atom_candidate_count",
            "complete_group_ids",
            "generation_evidence_count",
            "hard_reject_reason_distribution",
            "hard_rejected_sources",
            "missing_atom_ids",
            "pack_revision",
            "partial_group_ids",
            "per_atom_candidate_count",
            "pre_generation_availability_by_atom",
            "rerank_candidate_count",
            "root_candidate_count",
        }
    ),
    "retrieval.atom_grounding": frozenset(
        {
            "atom_count",
            "atom_coverage",
            "claim_rejection_codes",
            "claim_rejection_diagnostics",
            "extractive_fallback_reason",
            "generation_calls",
            "repair_calls",
        }
    ),
    "retrieval.claim_publication": frozenset(
        {
            "accepted_claim_count",
            "accepted_support_ids",
            "answer_path",
            "claim_rejection_code_distribution",
            "claim_rejection_diagnostics",
            "extractive_fallback_reason",
            "extractive_fallback_used",
            "final_atom_coverage",
            "final_coverage_by_atom",
            "generated_claim_count",
            "generation_called",
            "generation_gap_count",
            "published_claim_count",
            "published_quote_sha256s",
            "published_support_ids",
        }
    ),
}
_SAFE_NESTED_FIELDS = {
    "admitted_sources": (
        "support_id",
        "document_version_id",
        "chunk_id",
        "source_group_id",
        "table_node_id",
        "table_group_id",
        "table_row_index",
        "linked_atom_ids",
        "node_ids",
    ),
    "hard_rejected_sources": (
        "document_version_id",
        "chunk_id",
        "node_ids",
        "reasons",
    ),
    "claim_rejection_diagnostics": (
        "atom_id",
        "raw_reason_code",
        "public_reason_code",
        "validator_stage",
        "validator",
        "selected_support_ids",
        "allowed_support_ids",
        "claim_sha256",
        "quote_sha256s",
    ),
}


def _readonly_database(path: Path) -> sqlite3.Connection:
    """以不可写 URI 打开指定 Trace 快照。"""
    if not path.is_file():
        raise ValueError("TRACE_DATABASE_NOT_FOUND")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def inspect_trace_schema(path: Path) -> dict[str, tuple[str, ...]]:
    """先读取实际表和列，再允许执行固定的只读取证查询。"""
    with _readonly_database(path) as database:
        present = {
            str(row[0])
            for row in database.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name IN (?, ?)",
                _TRACE_TABLES,
            ).fetchall()
        }
        if present != set(_TRACE_TABLES):
            raise ValueError("TRACE_SCHEMA_TABLE_MISMATCH")
        schema = {
            table: tuple(
                str(row[1])
                for row in database.execute(
                    f"PRAGMA table_info('{table}')"
                ).fetchall()
            )
            for table in _TRACE_TABLES
        }
    if any(not _TRACE_COLUMNS[table] <= set(schema[table]) for table in schema):
        raise ValueError("TRACE_SCHEMA_COLUMN_MISMATCH")
    return schema


def read_trace_events(
    path: Path, trace_id: str
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """按序读取一个 Trace；调用方必须先完成 Schema 检查。"""
    with _readonly_database(path) as database:
        rows = database.execute(
            "SELECT event_name, payload_json FROM query_trace_events "
            "WHERE trace_id=? ORDER BY sequence",
            (trace_id,),
        ).fetchall()
    return tuple(
        (str(event_name), json.loads(str(payload_json)))
        for event_name, payload_json in rows
    )


def read_history_identity(path: Path, trace_id: str) -> dict[str, str] | None:
    """只读取历史身份列，不读取或回写业务正文。"""
    with _readonly_database(path) as database:
        row = database.execute(
            "SELECT trace_id, question_sha256, status FROM query_history "
            "WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "trace_id": str(row[0]),
        "question_sha256": str(row[1]),
        "status": str(row[2]),
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_trace_value(key: str, value: object) -> object:
    """对子对象再次按字段白名单裁剪，避免透传未知正文。"""
    nested_fields = _SAFE_NESTED_FIELDS.get(key)
    if nested_fields is None:
        return value
    rows = value if isinstance(value, (list, tuple)) else ()
    return [
        {field: row[field] for field in nested_fields if field in row}
        for row in rows
        if isinstance(row, dict)
    ]


def safe_observation(
    case_id: str,
    question: str,
    observed: dict[str, Any],
    events: Iterable[tuple[str, dict[str, Any]]],
    history: dict[str, str] | None,
) -> dict[str, Any]:
    """从私有响应生成只含身份、摘要和原因的 SAFE 记录。"""
    answer = str(observed.get("answer") or "")
    citations = observed.get("citations")
    citation_rows = citations if isinstance(citations, list) else []
    safe_events: dict[str, list[dict[str, Any]]] = {}
    for event_name, payload in events:
        allowed = _SAFE_TRACE_FIELDS.get(event_name)
        if allowed is None:
            continue
        safe_events.setdefault(event_name, []).append(
            {
                key: _safe_trace_value(key, value)
                for key, value in payload.items()
                if key in allowed
            }
        )
    return {
        "case_id": case_id,
        "question_sha256": _sha256_text(question),
        "trace_id": observed.get("trace_id"),
        "status": observed.get("status"),
        "reason_code": observed.get("reason_code"),
        "answer_sha256": _sha256_text(answer),
        "answer_chars": len(answer),
        "citation_count": len(citation_rows),
        "citation_quote_sha256s": [
            _sha256_text(str(item.get("quote") or ""))
            for item in citation_rows
            if isinstance(item, dict)
        ],
        "history_identity": history or "NOT_OBSERVED",
        "trace_events": safe_events,
    }


def _write_private_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def _validate_private_path(private_dir: Path) -> None:
    private_resolved = private_dir.resolve()
    if private_resolved == _REPO_ROOT or _REPO_ROOT in private_resolved.parents:
        raise ValueError("PRIVATE_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY")


def run(
    *,
    base_url: str,
    trace_db: Path,
    private_dir: Path,
    safe_manifest: Path,
) -> dict[str, Any]:
    """每题只请求一次，并在读取任何 Trace 前核验实际 Schema。"""
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != _CANDIDATE_PORT
    ):
        raise ValueError("ONLY_8289_LOOPBACK_CANDIDATE_ALLOWED")
    _validate_private_path(private_dir)
    schema = inspect_trace_schema(trace_db)
    selected = _cases(frozenset(_CASE_IDS))
    selected_ids = tuple(row["case_id"] for _group, row in selected)
    if set(selected_ids) != set(_CASE_IDS) or len(selected_ids) != len(
        _CASE_IDS
    ):
        raise ValueError("FROZEN_CASE_SET_MISMATCH")
    private_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    private_dir.chmod(0o700)
    private_rows: list[dict[str, Any]] = []
    safe_rows: list[dict[str, Any]] = []
    for _group, case in selected:
        opener, csrf = _session(base_url)
        observed = _chat(
            opener,
            csrf,
            base_url,
            f"wb08r03g-{case['case_id'].lower()}",
            str(case["question"]),
        )
        trace_id = observed.get("trace_id")
        events = (
            read_trace_events(trace_db, trace_id)
            if isinstance(trace_id, str)
            else ()
        )
        history = (
            read_history_identity(trace_db, trace_id)
            if isinstance(trace_id, str)
            else None
        )
        private_rows.append(
            {
                "case": case,
                "observed": observed,
                "trace_events": events,
            }
        )
        safe_rows.append(
            safe_observation(
                str(case["case_id"]),
                str(case["question"]),
                observed,
                events,
                history,
            )
        )
    private_output = private_dir / "private-replay.ndjson"
    _write_private_jsonl(private_output, private_rows)
    manifest = {
        "schema_version": "wb08r03g-safe-replay-v1",
        "case_ids": list(_CASE_IDS),
        "request_count": len(private_rows),
        "retry_count": 0,
        "trace_schema": schema,
        "private_output_sha256": hashlib.sha256(
            private_output.read_bytes()
        ).hexdigest(),
        "observations": safe_rows,
    }
    safe_manifest.parent.mkdir(parents=True, exist_ok=True)
    safe_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    """解析受控候选、只读数据库和分离输出目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8289")
    parser.add_argument("--trace-db", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--safe-manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = run(
        base_url=args.base_url.rstrip("/"),
        trace_db=args.trace_db,
        private_dir=args.private_dir,
        safe_manifest=args.safe_manifest,
    )
    print(
        json.dumps(
            {
                "case_ids": manifest["case_ids"],
                "request_count": manifest["request_count"],
                "retry_count": manifest["retry_count"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
