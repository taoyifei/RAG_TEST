#!/usr/bin/env python3
"""按冻结用例配对回放隔离 A/B，并私存每轮完整 SSE。"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import random
import secrets
import time
import urllib.request
from pathlib import Path
from typing import Any

from smoke_ab import A_ORIGIN, B_ORIGIN, _request, _summary, _write_new

EXPECTED_SCENARIOS = 60
EXPECTED_TURNS = 72
MAX_CASE_TURNS = 2
SEED = 20260925


def _load_cases(path: Path) -> tuple[list[dict[str, Any]], str]:
    """校验冻结题库规模、唯一性和必要会话形状。"""
    raw = path.read_bytes()
    cases = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if (
        len(cases) != EXPECTED_SCENARIOS
        or sum(len(c["turns"]) for c in cases) != EXPECTED_TURNS
    ):
        raise ValueError("冻结题库必须包含 60 场景、72 轮")
    if len({c["scenario_id"] for c in cases}) != EXPECTED_SCENARIOS:
        raise ValueError("场景 ID 重复")
    if {c["split"] for c in cases} != {"development", "holdout"}:
        raise ValueError("开发集或留出集缺失")
    for case in cases:
        if not 1 <= len(case["turns"]) <= MAX_CASE_TURNS:
            raise ValueError(f"轮数无效：{case['scenario_id']}")
        for turn in case["turns"]:
            if (
                not isinstance(turn["user_query"], str)
                or not turn["user_query"]
            ):
                raise ValueError(f"题面无效：{case['scenario_id']}")
    return cases, hashlib.sha256(raw).hexdigest()


def _event_objects(raw: bytes) -> list[dict[str, Any]]:
    """提取完整 JSON 数据帧，保留原文在受限 SSE 文件。"""
    objects = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            value = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _result_summary(side: str, raw: bytes, duration: float) -> dict[str, Any]:
    """仅输出不含问答正文的执行元数据。"""
    protocol = _summary(raw)
    objects = _event_objects(raw)
    types = protocol["payload_types"]
    terminal = "final" if side == "A" else "complete"
    result: dict[str, Any] = {
        "terminal": bool(types.get(terminal)),
        "protocol_error": bool(types.get("error")),
        "duration_seconds": round(duration, 3),
        "sse_bytes": len(raw),
    }
    if side == "A":
        final = next(
            (x for x in reversed(objects) if x.get("type") == "final"), {}
        )
        result.update(
            {
                "status": final.get("status"),
                "published": final.get("published"),
                "answer_chars": len(final.get("answer") or ""),
                "references": len(final.get("citations") or []),
            }
        )
    else:
        complete = next(
            (
                x
                for x in reversed(objects)
                if x.get("response_type") == "complete"
            ),
            {},
        )
        references = next(
            (x for x in objects if x.get("response_type") == "references"),
            {},
        )
        data = (
            complete.get("data")
            if isinstance(complete.get("data"), dict)
            else {}
        )
        result.update(
            {
                "answer_chars": len(data.get("final_content") or ""),
                "references": len(references.get("knowledge_references") or []),
            }
        )
    return result


class AClient:
    """在一个场景内复用 A 的公共会话和 conversation_id。"""

    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        session = json.loads(
            _request(self.opener, A_ORIGIN, "/api/public/session", {})
        )
        csrf = session.get("csrf_token")
        if not isinstance(csrf, str):
            raise RuntimeError("A 公共会话缺少 CSRF")
        self.headers = {
            "X-CSRF-Token": csrf,
            "X-Wanshitong-Stream-Protocol": "wanshitong-natural-sse-v1",
        }
        self.conversation_id = "ab-" + secrets.token_hex(16)

    def ask(self, question: str, timings: dict[str, float]) -> bytes:
        """在本场景既有历史下执行下一轮。"""
        return _request(
            self.opener,
            A_ORIGIN,
            "/api/public/chat",
            {"query": question, "conversation_id": self.conversation_id},
            self.headers,
            stream=True,
            timings=timings,
        )


class BClient:
    """在一个场景内复用腾讯原版 KnowledgeQA 会话。"""

    def __init__(self, identity: dict[str, Any], agent_id: str) -> None:
        self.opener = urllib.request.build_opener()
        self.headers = {"Authorization": f"Bearer {identity['token']}"}
        self.kb_id = identity["kb_id"]
        self.agent_id = agent_id
        response = json.loads(
            _request(
                self.opener,
                B_ORIGIN,
                "/api/v1/sessions",
                {"title": "隔离AB正式回放"},
                self.headers,
            )
        )
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise RuntimeError("B 会话缺少 ID")
        self.session_id = data["id"]

    def ask(self, question: str, timings: dict[str, float]) -> bytes:
        """使用原版标准问答入口，禁用 ReAct Agent。"""
        return _request(
            self.opener,
            B_ORIGIN,
            f"/api/v1/knowledge-chat/{self.session_id}",
            {
                "query": question,
                "knowledge_base_ids": [self.kb_id],
                "agent_enabled": False,
                "agent_id": self.agent_id,
                "disable_title": True,
            },
            self.headers,
            stream=True,
            timings=timings,
        )


def main() -> None:
    """执行指定集；场景内顺序随机且 A/B 用相同题面和模型入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument(
        "--split", choices=("development", "holdout"), required=True
    )
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    cases, case_sha = _load_cases(args.cases)
    identity = json.loads(args.identity.read_text(encoding="utf-8"))
    agent_id = json.loads(args.agent.read_text(encoding="utf-8"))["id"]
    args.outdir.mkdir(mode=0o700, exist_ok=False)
    generator = random.Random(SEED)  # noqa: S311 - 只用于可复现的执行顺序。
    schedule = {
        case["scenario_id"]: ["A", "B"]
        if generator.getrandbits(1)
        else ["B", "A"]
        for case in cases
    }
    selected = [case for case in cases if case["split"] == args.split]
    _write_new(
        args.outdir / "run.json",
        json.dumps(
            {
                "case_sha256": case_sha,
                "split": args.split,
                "seed": SEED,
                "schedule": {
                    case["scenario_id"]: schedule[case["scenario_id"]]
                    for case in selected
                },
            },
            ensure_ascii=False,
            indent=2,
        ).encode(),
    )
    results: list[dict[str, Any]] = []
    for case in selected:
        case_id = case["scenario_id"]
        for side in schedule[case_id]:
            try:
                client = (
                    AClient() if side == "A" else BClient(identity, agent_id)
                )
            except Exception as error:
                for turn in case["turns"]:
                    result = {
                        "scenario_id": case_id,
                        "turn_index": turn["turn_index"],
                        "side": side,
                        "setup_error": type(error).__name__,
                    }
                    results.append(result)
                    print(json.dumps(result), flush=True)
                continue
            for turn in case["turns"]:
                start = time.monotonic()
                timings: dict[str, float] = {}
                try:
                    raw = client.ask(turn["user_query"], timings)
                    duration = time.monotonic() - start
                    _write_new(
                        args.outdir
                        / f"{case_id}-T{turn['turn_index']}-{side}.sse",
                        raw,
                    )
                    summary = _result_summary(side, raw, duration)
                    summary.update(timings)
                except Exception as error:
                    summary = {
                        "request_error": type(error).__name__,
                        "duration_seconds": round(time.monotonic() - start, 3),
                    }
                result = {
                    "scenario_id": case_id,
                    "turn_index": turn["turn_index"],
                    "side": side,
                    **summary,
                }
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                # 每轮落盘，进程中断也能保留首次失败及完整前缀。
                (args.outdir / "summary.json").write_text(
                    json.dumps(results, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
    if len(results) != sum(len(case["turns"]) for case in selected) * 2:
        raise SystemExit("配对回放计数缺失")


if __name__ == "__main__":
    main()
