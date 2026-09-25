"""使用冻结合成正负例核查 CQ1 单次审核模型的实际误接受。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from rag_app.application.answering.natural_support import _SYSTEM


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/cq1_support_qualification.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, default=2680)
    parser.add_argument("--disable-thinking", action="store_true")
    return parser.parse_args()


def _response_row(
    case: dict[str, str],
    body: dict[str, Any],
    *,
    elapsed_ms: int,
) -> dict[str, object]:
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message", {})
    content = message.get("content", "")
    actual_verdict: str | None = None
    actual_coverage: str | None = None
    parse_ok = False
    if isinstance(content, str):
        try:
            review = json.loads(content)
            units = review.get("units") if isinstance(review, dict) else None
            if isinstance(units, list) and len(units) == 1:
                actual_verdict = units[0].get("verdict")
                actual_coverage = review.get("coverage")
                parse_ok = isinstance(actual_verdict, str) and isinstance(
                    actual_coverage, str
                )
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    expected = case["expected_verdict"]
    correct = (
        parse_ok
        and actual_verdict == "supported"
        and actual_coverage == "complete"
        if expected == "supported"
        else parse_ok and actual_verdict != "supported"
    )
    return {
        "case_id": case["case_id"],
        "expected_verdict": expected,
        "expected_coverage": case["expected_coverage"],
        "actual_verdict": actual_verdict,
        "actual_coverage": actual_coverage,
        "parse_ok": parse_ok,
        "correct": correct,
        "false_accept": (
            expected != "supported" and actual_verdict == "supported"
        ),
        "finish_reason": choice.get("finish_reason"),
        "elapsed_ms": elapsed_ms,
        "usage": body.get("usage"),
        "response_sha256": hashlib.sha256(str(content).encode()).hexdigest(),
        "raw_response": content,
    }


def main() -> int:
    """顺序请求已冻结的合成题，并保留原始响应供受控复核。"""
    args = _arguments()
    cases = json.loads(args.fixture.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("资格集必须是 JSON 列表。")
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("RAG_CQ1_MODEL_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    with httpx.Client(timeout=120.0, headers=headers) as client:
        for raw_case in cases:
            if not isinstance(raw_case, dict):
                raise ValueError("资格集条目必须为对象。")
            case = raw_case
            prompt = json.dumps(
                {
                    "question": case["question"],
                    "scope_digest": "synthetic-qualification",
                    "units": [{"unit_id": "u1", "text": case["draft"]}],
                    "sources": [
                        {"source_handle": "c1", "text": case["source"]}
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            started = time.monotonic()
            payload: dict[str, object] = {
                "model": args.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": args.max_tokens,
                "stream": False,
            }
            if args.disable_thinking:
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            response = client.post(
                args.base_url.rstrip("/") + "/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            rows.append(
                _response_row(
                    case,
                    response.json(),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary = {
        "cases": len(rows),
        "false_accepts": sum(bool(row["false_accept"]) for row in rows),
        "correct": sum(bool(row["correct"]) for row in rows),
        "format_failures": sum(not bool(row["parse_ok"]) for row in rows),
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary["false_accepts"] or summary["format_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
