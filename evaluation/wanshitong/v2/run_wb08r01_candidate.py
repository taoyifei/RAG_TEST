"""仅在本地通过候选端口执行 WB08R-01 的 44 条冻结题。"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import statistics
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_CASES_COUNT = 44
_NATURAL_IDS = {
    *(f"WB08R-N-{index:03d}" for index in range(1, 19)),
    "WB08R-N-037",
    "WB08R-N-038",
}


def _cases() -> tuple[tuple[str, dict[str, Any]], ...]:
    """读取冻结题目并复核每条问题摘要。"""
    selected: list[tuple[str, dict[str, Any]]] = []
    for group, filename in (
        ("latency24", "latency-24.ndjson"),
        ("natural20", "natural-60.ndjson"),
    ):
        rows = (
            json.loads(line)
            for line in (_ROOT / filename).read_text().splitlines()
            if line.strip()
        )
        for row in rows:
            if group == "natural20" and row["case_id"] not in _NATURAL_IDS:
                continue
            actual = hashlib.sha256(row["question"].encode()).hexdigest()
            if actual != row["question_sha256"]:
                raise ValueError(f"问题摘要失配：{row['case_id']}")
            selected.append((group, row))
    if len(selected) != _CASES_COUNT:
        raise ValueError("冻结子集必须为 latency-24 与 Natural-20。")
    return tuple(selected)


def _session(base_url: str) -> tuple[urllib.request.OpenerDirector, str]:
    """为每条题创建独立 Cookie/CSRF 会话。"""
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
    """逐帧计时，仅提取状态、引用身份和 Trace，不保存回答正文。"""
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
        event = "message"
        data: list[str] = []
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data.append(line.partition(":")[2].strip())
            elif not line and data:
                payload = json.loads("\n".join(data))
                events.append((event, payload, time.perf_counter() - started))
                event = "message"
                data.clear()
    total_ms = round((time.perf_counter() - started) * 1000, 2)
    finals = [item for name, item, _elapsed in events if name == "final"]
    if len(finals) != 1:
        errors = [
            {
                "code": item.get("code"),
                "stage": item.get("stage"),
                "message": item.get("message"),
                "trace_id": item.get("trace_id") or trace_header,
            }
            for name, item, _elapsed in events
            if name == "error"
        ]
        raise RuntimeError(f"公共问答未产生唯一 Final：{errors}")
    final = finals[0]
    citations = final.get("citations")
    citations = citations if isinstance(citations, list) else []
    stages = [
        elapsed for name, _item, elapsed in events if name == "stage"
    ]
    claims = [
        elapsed for name, _item, elapsed in events if name == "claim"
    ]
    answer = final.get("answer")
    return {
        "trace_id": final.get("trace_id") or trace_header,
        "status": final.get("status"),
        "reason_code": final.get("reason_code"),
        "request_total_ms": total_ms,
        "stage_status_first_ms": round(stages[0] * 1000, 2)
        if stages else None,
        "first_validated_claim_ms": round(claims[0] * 1000, 2)
        if claims else None,
        "answer_chars": len(answer) if isinstance(answer, str) else 0,
        "citation_count": len(citations),
        "catalog_citation_count": sum(
            isinstance(item, dict)
            and item.get("source_kind") == "catalog_metadata"
            for item in citations
        ),
        "citation_titles": tuple(
            str(item.get("document_title") or item.get("document_name") or "")
            for item in citations if isinstance(item, dict)
        ),
    }


def _normalized(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold()
        if char.isalnum()
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def run(base_url: str, output: Path) -> None:
    """顺序执行且每例独立会话，失败时保留已完成的有限指标。"""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("真实问答只能通过本地候选端口转发执行。")
    if output.resolve().parent != (_ROOT / "results").resolve():
        raise ValueError("结果只能写入 evaluation/wanshitong/v2/results。")
    output.parent.mkdir(exist_ok=True)
    completed = {
        json.loads(line)["run_id"] for line in output.read_text().splitlines()
        if line.strip()
    } if output.exists() else set()
    cases = _cases()
    for group, row in cases:
        run_id = f"{group}/{row['case_id']}"
        if run_id in completed:
            continue
        opener, csrf = _session(base_url)
        conversation_id = "wb08r01-" + row["case_id"].lower()
        context = row.get("context_question")
        if isinstance(context, str) and context:
            _chat(opener, csrf, base_url, conversation_id, context)
        observed = _chat(
            opener, csrf, base_url, conversation_id, row["question"]
        )
        expected = row.get("expected_source_document")
        source_match = (
            any(
                _normalized(title) == _normalized(expected)
                for title in observed["citation_titles"]
            )
            if isinstance(expected, str) and expected else None
        )
        observed.pop("citation_titles")
        result = {
            "run_id": run_id,
            "question_sha256": row["question_sha256"],
            "group": group,
            "latency_bucket": row.get("latency_bucket"),
            "question_style": row["question_style"],
            "expected_behavior": row["expected_behavior"],
            "expected_source_match": source_match,
            **observed,
        }
        with output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps({
            "run_id": run_id,
            "status": result["status"],
            "total_ms": result["request_total_ms"],
            "citations": result["citation_count"],
        }, ensure_ascii=False), flush=True)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    for group in ("latency24", "natural20"):
        samples = [
            row["request_total_ms"] for row in rows if row["group"] == group
        ]
        print(json.dumps({
            "group": group,
            "count": len(samples),
            "p50_ms": statistics.median(samples) if samples else None,
            "p95_ms": _percentile(samples, 0.95),
        }, ensure_ascii=False))


def main() -> None:
    """解析命令行并运行冻结子集。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8289")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    run(arguments.base_url.rstrip("/"), arguments.output)


if __name__ == "__main__":
    main()
