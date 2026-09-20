"""在专用 8289 候选上执行 WB08R-03 V14 五组真实回放。"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import sqlite3
import stat
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, TextIO

_CANDIDATE_PORT = 8289
_TRACE_SETTLE_ATTEMPTS = 20
_TRACE_SETTLE_INTERVAL_SECONDS = 0.25
_TERMINAL_EVENTS = frozenset(
    {"retrieval.claim_publication", "retrieval.complete"}
)
_CASES = (
    (
        "F024",
        "《2-需求阶段-需求变更评审会议纪要模板（模板）》的占位或示例内容能否直接当作正式要求？",
    ),
    ("N031", "做快验前到底得备齐啥？"),
    (
        "EXPLICIT_SOURCE",
        "《开发中心三种工作模式》中，需求快验的输入项是什么？",
    ),
    (
        "EXPLICIT_COLUMN",
        "《开发中心三种工作模式》中，需求快验“输入”列包含什么？",
    ),
    (
        "UNSAFE_TEMPORAL",
        "《开发中心三种工作模式》中，需求快验之前必须准备哪些材料？",
    ),
)
_SAFE_TRACE_FIELDS = {
    "retrieval.atom_grounding": frozenset(
        {
            "answer_plan_id",
            "answer_plan_revision",
            "answer_plan_records",
            "answer_plan_coverage",
            "atom_coverage",
            "claim_rejection_codes",
            "generation_calls",
            "relation_review_calls",
            "relation_review_skip_reason",
            "repair_calls",
        }
    ),
    "retrieval.claim_publication": frozenset(
        {
            "answer_path",
            "generation_called",
            "publication_path",
            "final_coverage_by_atom",
            "final_atom_coverage",
            "generated_claim_count",
            "accepted_claim_count",
            "published_claim_count",
            "generation_gap_count",
            "extractive_fallback_used",
        }
    ),
    "retrieval.generate": frozenset(
        {
            "answer_path",
            "generation_called",
            "reason_code",
            "provider_call_count_by_operation",
            "provider_call_count_observation_status",
        }
    ),
}


def _sha256_text(value: str) -> str:
    """计算 UTF-8 文本摘要。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _private_file(path: Path) -> TextIO:
    """以只读于所有者的独占方式创建私有证据文件。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _session(base_url: str) -> tuple[urllib.request.OpenerDirector, str]:
    """为每道题创建独立 Cookie 和 CSRF 会话。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    request = urllib.request.Request(  # noqa: S310
        base_url + "/api/public/session",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=10) as response:
        payload = json.load(response)
    csrf = payload.get("csrf_token")
    if not isinstance(csrf, str) or not csrf:
        raise ValueError("CANDIDATE_CSRF_MISSING")
    return opener, csrf


def _chat(
    opener: urllib.request.OpenerDirector,
    csrf: str,
    base_url: str,
    conversation_id: str,
    question: str,
) -> dict[str, Any]:
    """请求公共 SSE，并保留人工复核所需的私有响应。"""
    request = urllib.request.Request(  # noqa: S310
        base_url + "/api/public/chat",
        data=json.dumps(
            {"conversation_id": conversation_id, "query": question},
            ensure_ascii=False,
        ).encode(),
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
        },
        method="POST",
    )
    started = time.perf_counter()
    events: list[tuple[str, dict[str, Any], float]] = []
    with opener.open(request, timeout=180) as response:
        trace_header = response.headers.get("X-Trace-Id")
        event_name = "message"
        data: list[str] = []
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if line.startswith("event:"):
                event_name = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data.append(line.partition(":")[2].strip())
            elif not line and data:
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict):
                    raise ValueError("SSE_PAYLOAD_INVALID")
                events.append(
                    (event_name, payload, time.perf_counter() - started)
                )
                event_name = "message"
                data.clear()
    finals = [payload for name, payload, _elapsed in events if name == "final"]
    terminals = [
        (name, payload)
        for name, payload, _elapsed in events
        if name in {"final", "error", "cancelled"}
    ]
    final = finals[0] if len(finals) == 1 else {}
    citations = final.get("citations")
    answer = final.get("answer")
    return {
        "trace_id": final.get("trace_id") or trace_header,
        "status": final.get("status"),
        "reason_code": final.get("reason_code"),
        "answer": answer if isinstance(answer, str) else "",
        "citations": citations if isinstance(citations, list) else [],
        "terminal_event_count": len(terminals),
        "terminal_types": tuple(name for name, _payload in terminals),
        "final_count": len(finals),
        "request_total_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def _readonly_database(path: Path) -> sqlite3.Connection:
    """以只读 URI 打开 Trace 数据库。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("TRACE_DATABASE_NOT_FOUND")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _trace_snapshot(
    path: Path, trace_id: str
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """读取一个 Trace 的事件及 History 元数据。"""
    with _readonly_database(path) as database:
        rows = database.execute(
            "SELECT event_name, payload_json FROM query_trace_events "
            "WHERE trace_id=? ORDER BY sequence",
            (trace_id,),
        ).fetchall()
        history = database.execute(
            "SELECT status, question_sha256, duration_ms, metadata_json "
            "FROM query_history WHERE trace_id=?",
            (trace_id,),
        ).fetchone()
    events: list[tuple[str, dict[str, Any]]] = []
    for event_name, payload_json in rows:
        payload = json.loads(str(payload_json))
        if not isinstance(payload, dict):
            raise ValueError("TRACE_PAYLOAD_INVALID")
        raw_attributes = payload.get("attributes", payload)
        try:
            attributes = dict(raw_attributes)
        except (TypeError, ValueError) as error:
            raise ValueError("TRACE_ATTRIBUTES_INVALID") from error
        if not all(isinstance(key, str) for key in attributes):
            raise ValueError("TRACE_ATTRIBUTES_INVALID")
        events.append((str(event_name), attributes))
    if history is None:
        return events, {}
    metadata = json.loads(str(history[3]))
    if not isinstance(metadata, dict):
        raise ValueError("HISTORY_METADATA_INVALID")
    return events, {
        "status": str(history[0]),
        "question_sha256": str(history[1]),
        "duration_ms": history[2],
        "metadata": metadata,
    }


def _settled_trace(
    path: Path, trace_id: str
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """有限等待 Trace 和 History 进入终态。"""
    for attempt in range(_TRACE_SETTLE_ATTEMPTS):
        events, history = _trace_snapshot(path, trace_id)
        event_names = {name for name, _payload in events}
        if (
            history.get("status") not in {None, "PENDING", "STARTED", "RUNNING"}
            and event_names >= _TERMINAL_EVENTS
        ):
            return events, history
        if attempt + 1 < _TRACE_SETTLE_ATTEMPTS:
            time.sleep(_TRACE_SETTLE_INTERVAL_SECONDS)
    raise ValueError("TRACE_NOT_SETTLED")


def _provider_counts(metadata: dict[str, Any]) -> dict[str, int]:
    """提取 History 中实际记录的逐操作调用次数。"""
    usage = metadata.get("provider_usage")
    if not isinstance(usage, list):
        return {}
    return {
        str(item["operation"]): int(item["call_count"])
        for item in usage
        if isinstance(item, dict)
        and isinstance(item.get("operation"), str)
        and isinstance(item.get("call_count"), int)
        and not isinstance(item["call_count"], bool)
    }


def _stage_timings(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """提取不含正文的阶段耗时记录。"""
    diagnostics = metadata.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return []
    timings = diagnostics.get("stage_timings")
    return (
        [item for item in timings if isinstance(item, dict)]
        if isinstance(timings, list)
        else []
    )


def _safe_events(
    events: list[tuple[str, dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """仅保留 V14 结构身份、覆盖和实际调用诊断。"""
    safe: dict[str, list[dict[str, Any]]] = {}
    for event_name, payload in events:
        allowed = _SAFE_TRACE_FIELDS.get(event_name)
        if allowed is None:
            continue
        safe.setdefault(event_name, []).append(
            {key: value for key, value in payload.items() if key in allowed}
        )
    return safe


def _validate_output_dir(path: Path) -> Path:
    """独占创建所有者可读写的私有证据目录。"""
    resolved = path.resolve()
    if resolved == Path("/") or resolved.parent == Path("/"):
        raise ValueError("OUTPUT_DIRECTORY_TOO_BROAD")
    resolved.mkdir(mode=0o700, parents=False, exist_ok=False)
    resolved.chmod(0o700)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode & 0o077:
        raise ValueError("OUTPUT_DIRECTORY_PERMISSIONS")
    return resolved


def run(
    base_url: str,
    trace_db: Path,
    output_dir: Path,
    *,
    candidate_id: str,
) -> dict[str, Any]:
    """逐题执行一次真实请求并生成私有证据与 SAFE 摘要。"""
    if re.fullmatch(r"c[1-3]", candidate_id) is None:
        raise ValueError("CANDIDATE_ID_INVALID")
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != _CANDIDATE_PORT
    ):
        raise ValueError("ONLY_8289_LOOPBACK_CANDIDATE_ALLOWED")
    output = _validate_output_dir(output_dir)
    private_path = output / "private-replay.ndjson"
    safe_path = output / "safe-replay.json"
    safe_rows: list[dict[str, Any]] = []
    with _private_file(private_path) as private_stream:
        for case_id, question in _CASES:
            opener, csrf = _session(base_url)
            observed = _chat(
                opener,
                csrf,
                base_url,
                "wb08r03-v14-"
                + candidate_id
                + "-"
                + case_id.lower().replace("_", "-"),
                question,
            )
            trace_id = observed.get("trace_id")
            if not isinstance(trace_id, str) or not trace_id:
                raise ValueError(f"TRACE_ID_MISSING:{case_id}")
            events, history = _settled_trace(trace_db, trace_id)
            private_stream.write(
                json.dumps(
                    {
                        "case_id": case_id,
                        "question": question,
                        "observed": observed,
                        "trace_events": events,
                        "history": history,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            private_stream.flush()
            os.fsync(private_stream.fileno())
            answer = str(observed.get("answer") or "")
            citations = observed.get("citations")
            citation_rows = citations if isinstance(citations, list) else []
            metadata = history.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            safe_row = {
                "case_id": case_id,
                "question_sha256": _sha256_text(question),
                "trace_id": trace_id,
                "status": observed.get("status"),
                "reason_code": observed.get("reason_code"),
                "terminal_event_count": observed["terminal_event_count"],
                "terminal_types": observed["terminal_types"],
                "final_count": observed["final_count"],
                "request_total_ms": observed["request_total_ms"],
                "answer_sha256": _sha256_text(answer),
                "answer_chars": len(answer),
                "citation_count": len(citation_rows),
                "citation_title_sha256s": [
                    _sha256_text(
                        str(
                            item.get("document_title")
                            or item.get("document_name")
                            or ""
                        )
                    )
                    for item in citation_rows
                    if isinstance(item, dict)
                ],
                "history_status": history.get("status"),
                "history_question_sha256": history.get("question_sha256"),
                "history_duration_ms": history.get("duration_ms"),
                "provider_call_counts": _provider_counts(metadata),
                "stage_timings": _stage_timings(metadata),
                "trace_events": _safe_events(events),
            }
            safe_rows.append(safe_row)
            print(
                json.dumps(
                    {
                        "case_id": case_id,
                        "status": safe_row["status"],
                        "reason_code": safe_row["reason_code"],
                        "citation_count": safe_row["citation_count"],
                        "provider_call_counts": safe_row[
                            "provider_call_counts"
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
    manifest = {
        "schema_version": "wb08r03-v14-live-replay-v1",
        "candidate_id": candidate_id,
        "candidate_url": base_url,
        "request_count": len(safe_rows),
        "retry_count": 0,
        "private_replay_sha256": hashlib.sha256(
            private_path.read_bytes()
        ).hexdigest(),
        "observations": safe_rows,
    }
    with _private_file(safe_path) as stream:
        json.dump(
            manifest,
            stream,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")
    print(
        json.dumps(
            {
                "request_count": manifest["request_count"],
                "retry_count": manifest["retry_count"],
                "private_replay_sha256": manifest["private_replay_sha256"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return manifest


def main() -> None:
    """解析候选地址、只读 Trace 和独占输出目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8289")
    parser.add_argument("--trace-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    args = parser.parse_args()
    run(
        args.base_url.rstrip("/"),
        args.trace_db,
        args.output_dir,
        candidate_id=args.candidate_id,
    )


if __name__ == "__main__":
    main()
