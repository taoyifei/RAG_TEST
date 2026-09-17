"""只在 8289 候选服务运行 WB08R-03 的冻结功能小样本。"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from evaluation.wanshitong.v2.run_wb08r01_candidate import (
    _normalized,
    _session,
)

_ROOT = Path(__file__).resolve().parent
_RESULTS = _ROOT / "results"
_CASE_COUNT = 96
_CANDIDATE_PORT = 8289
_SUPPORT_ID = re.compile(r"\[S\d+\]")


def _cases() -> tuple[tuple[str, dict[str, Any]], ...]:
    """读取 Formal-54、Natural 复合/多轮与 Latency-24。"""
    selected: list[tuple[str, dict[str, Any]]] = []
    for group, filename in (
        ("formal54", "formal-54.ndjson"),
        ("natural_complex18", "natural-60.ndjson"),
        ("latency24", "latency-24.ndjson"),
    ):
        for line in (_ROOT / filename).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if group == "natural_complex18" and row["question_style"] not in {
                "compound",
                "multi_turn",
            }:
                continue
            if (
                hashlib.sha256(row["question"].encode()).hexdigest()
                != row["question_sha256"]
            ):
                raise ValueError(f"冻结问题摘要不一致：{row['case_id']}")
            selected.append((group, row))
    if len(selected) != _CASE_COUNT:
        raise ValueError("WB08R-03 冻结候选集必须恰好为 96 条。")
    return tuple(selected)


def _chat(
    opener: urllib.request.OpenerDirector,
    csrf: str,
    base_url: str,
    conversation_id: str,
    question: str,
) -> dict[str, Any]:
    """解析公共 SSE 的唯一 Final，保留独立候选的真实墙钟时间。"""
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
                events.append(
                    (
                        event,
                        json.loads("\n".join(data)),
                        time.perf_counter() - started,
                    )
                )
                event = "message"
                data.clear()
    finals = [payload for name, payload, _ in events if name == "final"]
    if len(finals) != 1:
        errors = [
            {"code": payload.get("code"), "stage": payload.get("stage")}
            for name, payload, _ in events
            if name == "error"
        ]
        raise RuntimeError(f"候选查询没有唯一 Final：{errors}")
    final = finals[0]
    citations = final.get("citations")
    citations = citations if isinstance(citations, list) else []
    answer = final.get("answer")
    answer = answer if isinstance(answer, str) else ""
    return {
        "trace_id": final.get("trace_id") or trace_header,
        "status": final.get("status"),
        "reason_code": final.get("reason_code"),
        "request_total_ms": round((time.perf_counter() - started) * 1000, 2),
        "stage_status_first_ms": next(
            (
                round(elapsed * 1000, 2)
                for name, _, elapsed in events
                if name == "stage"
            ),
            None,
        ),
        "claim_event_count": sum(name == "claim" for name, _, _ in events),
        "answer": answer,
        "citations": citations,
    }


def _verbatim_ratio(
    answer: str, citations: list[dict[str, Any]]
) -> float | None:
    """以各事实句与引用原文的最长连续重合估算复制比例。"""
    quotes = tuple(
        _normalized(str(item.get("quote") or "")) for item in citations
    )
    quotes = tuple(value for value in quotes if value)
    sentences = tuple(
        _normalized(part)
        for part in re.split(r"[。；;\n]", _SUPPORT_ID.sub("", answer))
    )
    sentences = tuple(value for value in sentences if value)
    if not quotes or not sentences:
        return None
    matched = sum(
        max(
            difflib.SequenceMatcher(None, sentence, quote, autojunk=False)
            .find_longest_match(0, len(sentence), 0, len(quote))
            .size
            for quote in quotes
        )
        for sentence in sentences
    )
    return round(min(1.0, matched / sum(map(len, sentences))), 4)


def _append_private(path: Path, row: dict[str, Any]) -> None:
    """只把人工抽查正文写入本地受限文件，不提交评测原文。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]


def run(base_url: str, output: Path, review_output: Path) -> None:
    """逐题创建会话，可从已写入结果的下一题继续。"""
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != _CANDIDATE_PORT
    ):
        raise ValueError("只能查询本机 8289 候选端口。")
    if (
        output.resolve().parent != _RESULTS.resolve()
        or review_output.resolve().parent != _RESULTS.resolve()
    ):
        raise ValueError("结果只能写入 WB08R 评测 results 目录。")
    _RESULTS.mkdir(exist_ok=True)
    completed = (
        {
            json.loads(line)["run_id"]
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        if output.exists()
        else set()
    )
    for group, row in _cases():
        run_id = f"{group}/{row['case_id']}"
        if run_id in completed:
            continue
        opener, csrf = _session(base_url)
        conversation_id = "wb08r03-" + row["case_id"].lower()
        context = row.get("context_question")
        if isinstance(context, str) and context:
            _chat(opener, csrf, base_url, conversation_id, context)
        observed = _chat(
            opener, csrf, base_url, conversation_id, row["question"]
        )
        citations = observed.pop("citations")
        answer = observed.pop("answer")
        expected = row.get("expected_source_document")
        source_match = (
            any(
                _normalized(
                    str(
                        item.get("document_title")
                        or item.get("document_name")
                        or ""
                    )
                )
                == _normalized(expected)
                for item in citations
                if isinstance(item, dict)
            )
            if isinstance(expected, str) and expected
            else None
        )
        record = {
            "run_id": run_id,
            "case_id": row["case_id"],
            "group": group,
            "question_sha256": row["question_sha256"],
            "question_style": row["question_style"],
            "expected_behavior": row["expected_behavior"],
            "expected_source_match": source_match,
            "answer_chars": len(answer),
            "citation_count": len(citations),
            "public_citations_have_quotes": all(
                isinstance(item, dict)
                and isinstance(item.get("quote"), str)
                and bool(item["quote"])
                for item in citations
            ),
            "verbatim_copy_ratio": _verbatim_ratio(answer, citations),
            **observed,
        }
        _append_private(
            review_output,
            {
                "run_id": run_id,
                "question": row["question"],
                "answer": answer,
                "citations": citations,
            },
        )
        _append_private(output, record)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": record["status"],
                    "request_total_ms": record["request_total_ms"],
                    "citation_count": record["citation_count"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    rows = [
        json.loads(line)
        for line in output.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for group in ("formal54", "natural_complex18", "latency24"):
        group_rows = [row for row in rows if row["group"] == group]
        latencies = [row["request_total_ms"] for row in group_rows]
        print(
            json.dumps(
                {
                    "group": group,
                    "count": len(group_rows),
                    "answerable": sum(
                        row["status"] == "ANSWERABLE" for row in group_rows
                    ),
                    "source_hit": sum(
                        row["expected_source_match"] is True
                        for row in group_rows
                    ),
                    "p50_ms": statistics.median(latencies)
                    if latencies
                    else None,
                    "p95_ms": _p95(latencies),
                },
                ensure_ascii=False,
            )
        )


def main() -> None:
    """解析候选地址与受限结果路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8289")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    args = parser.parse_args()
    run(args.base_url.rstrip("/"), args.output, args.review_output)


if __name__ == "__main__":
    main()
