"""在已登录的专用 8289 候选上逐题捕获 Q1 当前问答。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any, TextIO

import httpx

_CANDIDATE_ORIGIN = "http://10.242.180.54:8289"
_CANDIDATE_BASE = _CANDIDATE_ORIGIN + "/kb"
_CANDIDATE_COOKIE_PREFIX = "kb_user_session_candidate_8289"
_PRIVATE_DIRECTORY_MODE = 0o700
_MAX_CASE_COUNT = 12
_HTTP_OK = 200


def _private_file(path: Path) -> TextIO:
    """独占创建仅所有者可读写的证据文件。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def _validate_output_dir(path: Path) -> Path:
    """拒绝仓库内、符号链接及权限开放的输出目录。"""
    resolved = path.resolve(strict=False)
    if path.is_symlink() or any(
        (parent / ".git").exists()
        for parent in (resolved, *resolved.parents)
    ):
        raise ValueError("Q1_OUTPUT_DIRECTORY_IN_GIT_OR_SYMLINK")
    resolved.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=False, exist_ok=False)
    if stat.S_IMODE(resolved.stat().st_mode) != _PRIVATE_DIRECTORY_MODE:
        raise ValueError("Q1_OUTPUT_DIRECTORY_PERMISSIONS")
    return resolved


def _candidate_cookie(path: Path) -> tuple[str, str]:
    """只取 8289 浏览器会话 Cookie，不传播 RDMS 或管理员 Cookie。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Q1_AUTH_STATE_INVALID")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Q1_AUTH_STATE_PERMISSIONS")
    state = json.loads(path.read_text(encoding="utf-8"))
    cookies = [
        cookie
        for cookie in state.get("cookies", [])
        if cookie.get("domain") == "10.242.180.54"
        and cookie.get("name", "").startswith(_CANDIDATE_COOKIE_PREFIX)
    ]
    if len(cookies) != 1:
        raise ValueError("Q1_CANDIDATE_COOKIE_MISSING_OR_DUPLICATE")
    cookie = cookies[0]
    if not isinstance(cookie.get("value"), str) or not cookie["value"]:
        raise ValueError("Q1_CANDIDATE_COOKIE_INVALID")
    return cookie["name"], cookie["value"]


def _cases(path: Path, manifest_path: Path) -> list[dict[str, Any]]:
    """核对固定题集摘要、身份和题面后才允许真实请求。"""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["cases_sha256"]:
        raise ValueError("Q1_CASES_HASH_MISMATCH")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    if (
        len(rows) != manifest["case_count"]
        or not 1 <= len(rows) <= _MAX_CASE_COUNT
        or len({row["case_id"] for row in rows}) != len(rows)
        or any(not row.get("question") for row in rows)
    ):
        raise ValueError("Q1_CASES_INVALID")
    return rows


def _session(client: httpx.Client) -> str:
    """沿已登录的 SSO 会话取得公共 CSRF，不建立匿名旁路。"""
    response = client.post(_CANDIDATE_BASE + "/api/public/session", json={})
    if response.status_code != _HTTP_OK:
        raise ValueError(f"Q1_PUBLIC_SESSION_HTTP_{response.status_code}")
    csrf = response.json().get("csrf_token")
    if not isinstance(csrf, str) or not csrf:
        raise ValueError("Q1_PUBLIC_CSRF_MISSING")
    return csrf


def _chat(
    client: httpx.Client,
    csrf: str,
    question: str,
    conversation_id: str,
) -> dict[str, Any]:
    """提交一次问答并记录完整公共终态，不重试模型请求。"""
    started = time.perf_counter()
    events: list[tuple[str, dict[str, Any]]] = []
    with client.stream(
        "POST",
        _CANDIDATE_BASE + "/api/public/chat",
        json={"conversation_id": conversation_id, "query": question},
        headers={
            "Accept": "text/event-stream",
            "X-CSRF-Token": csrf,
        },
    ) as response:
        if response.status_code != _HTTP_OK:
            raise ValueError(f"Q1_PUBLIC_CHAT_HTTP_{response.status_code}")
        trace_header = response.headers.get("X-Trace-Id")
        event_name = "message"
        data: list[str] = []
        for line in response.iter_lines():
            if line.startswith("event:"):
                event_name = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data.append(line.partition(":")[2].strip())
            elif not line and data:
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict):
                    raise ValueError("Q1_SSE_PAYLOAD_INVALID")
                events.append((event_name, payload))
                event_name = "message"
                data.clear()
    terminals = [
        (name, payload)
        for name, payload in events
        if name in {"final", "error", "cancelled"}
    ]
    finals = [payload for name, payload in terminals if name == "final"]
    final = finals[0] if len(finals) == 1 else {}
    return {
        "trace_id": final.get("trace_id") or trace_header,
        "status": final.get("status"),
        "reason_code": final.get("reason_code"),
        "answer": final.get("answer"),
        "citations": final.get("citations"),
        "terminal_types": [name for name, _payload in terminals],
        "terminal_event_count": len(terminals),
        "final_count": len(finals),
        "request_total_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def run(
    *,
    cases_path: Path,
    manifest_path: Path,
    auth_state_path: Path,
    output_dir: Path,
) -> None:
    """先冻结输入，再顺序执行 Q1 小集合并保存私有与安全摘要。"""
    rows = _cases(cases_path, manifest_path)
    cookie_name, cookie_value = _candidate_cookie(auth_state_path)
    output = _validate_output_dir(output_dir)
    safe_rows: list[dict[str, Any]] = []
    run_id = uuid.uuid4().hex[:12]
    with httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(180.0),
    ) as client:
        client.cookies.set(
            cookie_name,
            cookie_value,
            domain="10.242.180.54",
            path="/",
        )
        csrf = _session(client)
        with _private_file(output / "public-replay.private.ndjson") as stream:
            for case in rows:
                result = _chat(
                    client,
                    csrf,
                    case["question"],
                    f"q1.{run_id}.{case['case_id']}",
                )
                private = {
                    "case_id": case["case_id"],
                    "question": case["question"],
                    "observed": result,
                }
                stream.write(json.dumps(private, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                answer = (
                    result["answer"]
                    if isinstance(result["answer"], str)
                    else ""
                )
                citations = (
                    result["citations"]
                    if isinstance(result["citations"], list)
                    else []
                )
                safe = {
                    "case_id": case["case_id"],
                    "question_sha256": hashlib.sha256(
                        case["question"].encode("utf-8")
                    ).hexdigest(),
                    "trace_id": result["trace_id"],
                    "status": result["status"],
                    "reason_code": result["reason_code"],
                    "answer_sha256": hashlib.sha256(
                        answer.encode("utf-8")
                    ).hexdigest(),
                    "answer_chars": len(answer),
                    "citation_count": len(citations),
                    "terminal_types": result["terminal_types"],
                    "request_total_ms": result["request_total_ms"],
                }
                safe_rows.append(safe)
                print(json.dumps(safe, ensure_ascii=False), flush=True)
                if result["final_count"] != 1 or not isinstance(
                    result["trace_id"], str
                ):
                    raise ValueError(f"Q1_TERMINAL_INVALID:{case['case_id']}")
    with _private_file(output / "safe-summary.json") as stream:
        json.dump(
            {
                "run_id": run_id,
                "case_count": len(rows),
                "observations": safe_rows,
            },
            stream,
            ensure_ascii=False,
            indent=2,
        )
        stream.write("\n")


def main() -> None:
    """解析固定候选输入和仓库外私有输出路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--auth-state", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    run(
        cases_path=arguments.cases,
        manifest_path=arguments.manifest,
        auth_state_path=arguments.auth_state,
        output_dir=arguments.output_dir,
    )


if __name__ == "__main__":
    main()
