"""只在 8289 候选服务执行 WB08R-04 部门影子路由验收。"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any

_CANDIDATE_URL = "http://127.0.0.1:8289"
_SHADOW_EVENT = "retrieval.department_route_shadow"
_ROUTE_REVISION = "wanshitong-department-shadow-v1"
_TRACE_ID = re.compile(r"^trace_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_REQUIRED_SCENARIOS = frozenset(
    {
        "explicit_department",
        "title_only",
        "cross_department",
        "no_department_term",
        "short_follow_up",
        "real_multi_turn",
        "no_evidence",
        "explicit_document",
    }
)
_REQUIRED_ATTRIBUTES = frozenset(
    {
        "route_revision",
        "profile_revision",
        "resolved_root_query_sha256",
        "context_mode",
        "scope_digest",
        "actual_scope_kind",
        "top1_department_key",
        "top1_score_bucket",
        "top2_department_key",
        "top2_score_bucket",
        "confidence",
        "recommended_scope",
        "final_cited_department_keys",
        "department_filter_applied",
        "embedding_reused",
        "extra_provider_calls",
        "status",
        "reason_codes",
        "elapsed_ms",
    }
)
_FORBIDDEN_ATTRIBUTE_PARTS = (
    "query_text",
    "question",
    "vector",
    "endpoint",
    "secret",
    "token",
)


def _session(
    base_url: str,
) -> tuple[urllib.request.OpenerDirector, str]:
    """为每个场景创建独立 Cookie/CSRF 会话。"""
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
        raise ValueError("候选服务没有返回 CSRF Token。")
    return opener, csrf


def _chat(
    opener: urllib.request.OpenerDirector,
    csrf: str,
    base_url: str,
    conversation_id: str,
    question: str,
) -> dict[str, Any]:
    """读取公共 SSE，并保留唯一终态及验收所需内容。"""
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
    events: list[tuple[str, dict[str, Any]]] = []
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
                    raise ValueError("公共 SSE payload 不是 JSON object。")
                events.append((event_name, payload))
                event_name = "message"
                data.clear()
    finals = [payload for name, payload in events if name == "final"]
    terminals = [
        (name, payload)
        for name, payload in events
        if name in {"final", "error", "cancelled"}
    ]
    final = finals[0] if len(finals) == 1 else {}
    citations = final.get("citations")
    answer = final.get("answer")
    return {
        "trace_id": final.get("trace_id") or trace_header,
        "status": final.get("status"),
        "reason_code": final.get("reason_code"),
        "terminal_event_count": len(terminals),
        "terminal_type": (
            terminals[0][0].upper()
            if len(terminals) == 1
            else "MISSING"
            if not terminals
            else "MULTIPLE"
        ),
        "final_count": len(finals),
        "claim_event_count": sum(name == "claim" for name, _ in events),
        "request_total_ms": round((time.perf_counter() - started) * 1000, 2),
        "answer": answer if isinstance(answer, str) else "",
        "citations": citations if isinstance(citations, list) else [],
    }


def _sha256(value: str) -> str:
    """计算验收输入或输出的稳定摘要。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_cases(path: Path) -> tuple[dict[str, Any], ...]:
    """读取并校验八类冻结场景。"""
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"第 {line_number} 行不是 JSON object。")
        scenario_id = value.get("scenario_id")
        question = value.get("question")
        question_sha256 = value.get("question_sha256")
        if not isinstance(scenario_id, str) or not isinstance(question, str):
            raise ValueError(f"第 {line_number} 行缺少场景或问题。")
        if _sha256(question) != question_sha256:
            raise ValueError(f"场景 {scenario_id} 的问题摘要不一致。")
        setups = value.get("setup", [])
        if not isinstance(setups, list):
            raise ValueError(f"场景 {scenario_id} 的 setup 必须是数组。")
        for setup in setups:
            if not isinstance(setup, dict):
                raise ValueError(f"场景 {scenario_id} 的 setup 格式错误。")
            setup_question = setup.get("question")
            if not isinstance(setup_question, str) or _sha256(
                setup_question
            ) != setup.get("question_sha256"):
                raise ValueError(f"场景 {scenario_id} 的 setup 摘要不一致。")
        cases.append(value)
    scenario_ids = tuple(str(item["scenario_id"]) for item in cases)
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("验收场景 ID 不允许重复。")
    if frozenset(scenario_ids) != _REQUIRED_SCENARIOS:
        raise ValueError("验收文件必须恰好覆盖阶段 04 的八类场景。")
    return tuple(cases)


def _profile_departments(path: Path) -> dict[str, str]:
    """读取 Profile 中的文档到部门映射。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("Profile 缺少 profiles。")
    mapping: dict[str, str] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError("Profile 项格式错误。")
        department_key = profile.get("department_key")
        document_ids = profile.get("document_ids")
        if not isinstance(department_key, str) or not isinstance(
            document_ids, list
        ):
            raise ValueError("Profile 部门或文档字段格式错误。")
        for document_id in document_ids:
            if not isinstance(document_id, str):
                raise ValueError("Profile 文档 ID 格式错误。")
            if document_id in mapping:
                raise ValueError("同一文档不能归属多个部门。")
            mapping[document_id] = department_key
    return mapping


def _trace_snapshot(
    trace_db: Path,
    trace_id: str,
    *,
    timeout_seconds: float = 20.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """等待异步 Trace 落盘，并返回根记录与 Shadow 属性。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        connection = sqlite3.connect(
            f"file:{trace_db}?mode=ro", uri=True, timeout=2.0
        )
        try:
            trace_row = connection.execute(
                """
                SELECT status, capture_complete, project_id, knowledge_base_id,
                       release_revision
                FROM traces
                WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
            shadow_row = connection.execute(
                """
                SELECT attributes_json
                FROM spans
                WHERE trace_id = ? AND name = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (trace_id, _SHADOW_EVENT),
            ).fetchone()
        finally:
            connection.close()
        if trace_row is not None and shadow_row is not None:
            trace = {
                "status": trace_row[0],
                "capture_complete": bool(trace_row[1]),
                "project_id": trace_row[2],
                "knowledge_base_id": trace_row[3],
                "release_revision": trace_row[4],
            }
            attributes = json.loads(shadow_row[0])
            if not isinstance(attributes, dict):
                raise ValueError("Shadow Trace 属性不是 JSON object。")
            if trace["capture_complete"]:
                return trace, attributes
        time.sleep(0.2)
    raise TimeoutError(f"Trace 未在时限内完整落盘：{trace_id}")


def _citation_departments(citations: object) -> tuple[str, ...]:
    """提取公共 DTO 允许暴露的引用部门名称。"""
    if not isinstance(citations, list):
        return ()
    return tuple(
        dict.fromkeys(
            department
            for item in citations
            if isinstance(item, dict)
            and isinstance(
                department := (
                    item.get("department_name") or item.get("department")
                ),
                str,
            )
        )
    )


def _hard_failures(  # noqa: PLR0912
    case: dict[str, Any],
    result: dict[str, Any],
    attributes: dict[str, Any],
    valid_department_keys: frozenset[str],
) -> list[str]:
    """只判定阶段 04 的硬合同，不给路由准确率设门槛。"""
    failures: list[str] = []
    if result.get("terminal_event_count") != 1:
        failures.append("PUBLIC_TERMINAL_COUNT")
    if result.get("terminal_type") != "FINAL" or result.get("final_count") != 1:
        failures.append("PUBLIC_FINAL_MISSING")
    if not _TRACE_ID.fullmatch(str(result.get("trace_id") or "")):
        failures.append("TRACE_ID_INVALID")
    missing = sorted(_REQUIRED_ATTRIBUTES.difference(attributes))
    if missing:
        failures.append("SHADOW_FIELDS_MISSING:" + ",".join(missing))
    forbidden = sorted(
        key
        for key in attributes
        if any(part in key.casefold() for part in _FORBIDDEN_ATTRIBUTE_PARTS)
    )
    if forbidden:
        failures.append("SHADOW_FIELDS_FORBIDDEN:" + ",".join(forbidden))
    if attributes.get("route_revision") != _ROUTE_REVISION:
        failures.append("ROUTE_REVISION_MISMATCH")
    if not _SHA256.fullmatch(str(attributes.get("profile_revision") or "")):
        failures.append("PROFILE_REVISION_INVALID")
    if not _SHA256.fullmatch(
        str(attributes.get("resolved_root_query_sha256") or "")
    ):
        failures.append("ROOT_QUERY_DIGEST_INVALID")
    if not _SHA256.fullmatch(str(attributes.get("scope_digest") or "")):
        failures.append("SCOPE_DIGEST_INVALID")
    if attributes.get("department_filter_applied") is not False:
        failures.append("DEPARTMENT_FILTER_APPLIED")
    if attributes.get("extra_provider_calls") != 0:
        failures.append("EXTRA_PROVIDER_CALLS")
    if attributes.get("status") not in {"COMPUTED", "FALLBACK"}:
        failures.append("SHADOW_NOT_COMPUTED")
    observed_cited = attributes.get("final_cited_department_keys")
    if (
        not isinstance(observed_cited, list)
        or any(
            not isinstance(item, str) or item not in valid_department_keys
            for item in observed_cited
        )
        or len(observed_cited) != len(set(observed_cited))
    ):
        failures.append("CITED_DEPARTMENT_INVALID")
    expected_scope = case.get("expected_actual_scope_kind")
    if (
        isinstance(expected_scope, str)
        and attributes.get("actual_scope_kind") != expected_scope
    ):
        failures.append("ACTUAL_SCOPE_MISMATCH")
    expected_reason = case.get("expected_reason_code")
    observed_reasons = attributes.get("reason_codes")
    if isinstance(expected_reason, str) and (
        not isinstance(observed_reasons, list)
        or expected_reason not in observed_reasons
    ):
        failures.append("EXPECTED_ROUTE_REASON_MISSING")
    return failures


def _run_turn(
    opener: urllib.request.OpenerDirector,
    csrf: str,
    conversation_id: str,
    question: str,
    trace_db: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """执行一轮公共问答，并等待对应 Trace 完整落盘。"""
    result = _chat(
        opener,
        csrf,
        _CANDIDATE_URL,
        conversation_id,
        question,
    )
    trace_id = result.get("trace_id")
    if not isinstance(trace_id, str):
        raise ValueError("公共终态没有 Trace ID。")
    trace, attributes = _trace_snapshot(trace_db, trace_id)
    return result, trace, attributes


def _safe_result(result: dict[str, Any]) -> dict[str, Any]:
    """裁剪正文和引用原文，只保留可审计摘要。"""
    answer = result.get("answer")
    citations = result.get("citations")
    return {
        "trace_id": result.get("trace_id"),
        "status": result.get("status"),
        "reason_code": result.get("reason_code"),
        "terminal_event_count": result.get("terminal_event_count"),
        "terminal_type": result.get("terminal_type"),
        "final_count": result.get("final_count"),
        "claim_event_count": result.get("claim_event_count"),
        "request_total_ms": result.get("request_total_ms"),
        "answer_chars": len(answer) if isinstance(answer, str) else 0,
        "answer_sha256": _sha256(answer) if isinstance(answer, str) else None,
        "citation_count": len(citations) if isinstance(citations, list) else 0,
        "citation_department_names": _citation_departments(citations),
    }


def _resume_records(path: Path | None) -> dict[str, dict[str, Any]]:
    """读取先前私有报告，供单场景复验时原位替换。"""
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("cases")
    if not isinstance(records, list):
        raise ValueError("续跑报告缺少 cases。")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(
            record.get("scenario_id"), str
        ):
            raise ValueError("续跑报告场景格式错误。")
        result[record["scenario_id"]] = record
    return result


def _write_private_report(path: Path, payload: dict[str, Any]) -> None:
    """以 0600 原子写入候选机私有证据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def run(  # noqa: PLR0913
    cases_path: Path,
    trace_db: Path,
    profile_path: Path,
    output_path: Path,
    *,
    only: frozenset[str] | None = None,
    resume_from: Path | None = None,
) -> int:
    """执行八类场景，输出不含问题与回答正文的私有报告。"""
    cases = _load_cases(cases_path)
    document_departments = _profile_departments(profile_path)
    valid_department_keys = frozenset(document_departments.values())
    if only is not None and not only.issubset(_REQUIRED_SCENARIOS):
        raise ValueError("--only 包含未知场景。")
    records_by_id = _resume_records(resume_from)
    for case in cases:
        scenario_id = str(case["scenario_id"])
        if only is not None and scenario_id not in only:
            continue
        conversation_id = "wb08r04-shadow-" + scenario_id.replace("_", "-")
        opener, csrf = _session(_CANDIDATE_URL)
        setup_records: list[dict[str, Any]] = []
        for setup in case.get("setup", []):
            setup_result, setup_trace, setup_attributes = _run_turn(
                opener,
                csrf,
                conversation_id,
                str(setup["question"]),
                trace_db,
            )
            setup_failures = _hard_failures(
                {},
                setup_result,
                setup_attributes,
                valid_department_keys,
            )
            setup_records.append(
                {
                    "question_sha256": setup["question_sha256"],
                    "public": _safe_result(setup_result),
                    "trace": setup_trace,
                    "shadow": setup_attributes,
                    "hard_failures": setup_failures,
                }
            )
        result, trace, attributes = _run_turn(
            opener,
            csrf,
            conversation_id,
            str(case["question"]),
            trace_db,
        )
        case_failures = _hard_failures(
            case,
            result,
            attributes,
            valid_department_keys,
        )
        record = {
            "scenario_id": scenario_id,
            "question_sha256": case["question_sha256"],
            "setup": setup_records,
            "public": _safe_result(result),
            "trace": trace,
            "shadow": attributes,
            "hard_failures": case_failures,
        }
        records_by_id[scenario_id] = record
        print(
            json.dumps(
                {
                    "scenario_id": scenario_id,
                    "trace_id": result.get("trace_id"),
                    "public_status": result.get("status"),
                    "shadow_status": attributes.get("status"),
                    "recommended_scope": attributes.get("recommended_scope"),
                    "actual_scope_kind": attributes.get("actual_scope_kind"),
                    "embedding_reused": attributes.get("embedding_reused"),
                    "hard_failures": case_failures,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    report_cases = [
        records_by_id[str(case["scenario_id"])]
        for case in cases
        if str(case["scenario_id"]) in records_by_id
    ]
    hard_failures: list[str] = []
    embedding_reused = False
    case_definitions = {str(case["scenario_id"]): case for case in cases}
    for record in report_cases:
        scenario_id = str(record["scenario_id"])
        for setup in record.get("setup", []):
            setup_failures = _hard_failures(
                {},
                setup["public"],
                setup["shadow"],
                valid_department_keys,
            )
            setup["hard_failures"] = setup_failures
            hard_failures.extend(
                f"{scenario_id}/setup:{failure}" for failure in setup_failures
            )
            embedding_reused = embedding_reused or (
                setup["shadow"].get("embedding_reused") is True
            )
        case_failures = _hard_failures(
            case_definitions[scenario_id],
            record["public"],
            record["shadow"],
            valid_department_keys,
        )
        record["hard_failures"] = case_failures
        hard_failures.extend(
            f"{scenario_id}:{failure}" for failure in case_failures
        )
        embedding_reused = embedding_reused or (
            record["shadow"].get("embedding_reused") is True
        )
    complete = len(report_cases) == len(_REQUIRED_SCENARIOS)
    if complete and not embedding_reused:
        hard_failures.append("SUITE:NO_ROOT_VECTOR_REUSED")
    status = (
        "FAILED"
        if hard_failures
        else "SHADOW_FUNCTIONAL_PASS"
        if complete
        else "PARTIAL_PASS"
    )
    payload = {
        "schema_revision": "wb08r04-shadow-acceptance-v2",
        "candidate_url": _CANDIDATE_URL,
        "case_count": len(report_cases),
        "embedding_reused_observed": embedding_reused,
        "hard_failures": hard_failures,
        "status": status,
        "cases": report_cases,
    }
    _write_private_report(output_path, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "case_count": len(report_cases),
                "hard_failure_count": len(hard_failures),
                "output_sha256": hashlib.sha256(
                    output_path.read_bytes()
                ).hexdigest(),
            },
            ensure_ascii=False,
        )
    )
    return 0 if not hard_failures else 1


def main() -> int:
    """解析命令行并限制运行目标为本机 8289。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--trace-db", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--only", action="append", choices=sorted(_REQUIRED_SCENARIOS)
    )
    parser.add_argument("--resume-from", type=Path)
    args = parser.parse_args()
    return run(
        args.cases,
        args.trace_db,
        args.profile,
        args.output,
        only=frozenset(args.only) if args.only else None,
        resume_from=args.resume_from,
    )


if __name__ == "__main__":
    raise SystemExit(main())
