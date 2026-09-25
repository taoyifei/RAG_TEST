#!/usr/bin/env python3
"""对隔离 A/B 运行同题原生流式问答，私存原始 SSE。"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

A_ORIGIN = "http://127.0.0.1:18389"
B_ORIGIN = "http://127.0.0.1:18390"
MAX_STREAM_SECONDS = 240


def _write_new(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)


def _request(  # noqa: PLR0912, PLR0913 - 流式协议需处理多种帧与会话参数。
    opener: urllib.request.OpenerDirector,
    origin: str,
    path: str,
    body: dict[str, object],
    headers: dict[str, str] | None = None,
    *,
    stream: bool = False,
    timings: dict[str, float] | None = None,
) -> bytes:
    """仅向本机回环实例发 POST，带必要会话凭据。"""
    if origin not in {A_ORIGIN, B_ORIGIN} or not path.startswith("/"):
        raise ValueError("只允许访问隔离 A/B 的回环 API")
    request = urllib.request.Request(  # noqa: S310 - 固定回环 Origin。
        origin + path,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    request_started = time.monotonic()
    try:
        with opener.open(request, timeout=120) as response:
            if not stream:
                return response.read()
            parts: list[bytes] = []
            started = time.monotonic()
            terminal_event = False
            while time.monotonic() - started < MAX_STREAM_SECONDS:
                line = response.readline()
                if not line:
                    break
                parts.append(line)
                if line.startswith(b"event:") and line[6:].strip() in {
                    b"complete",
                    b"error",
                }:
                    terminal_event = True
                if line.startswith(b"data:"):
                    try:
                        value = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if (
                        timings is not None
                        and "first_text_seconds" not in timings
                        and isinstance(value, dict)
                    ):
                        body_text = None
                        if value.get("type") == "answer_delta":
                            body_text = value.get("text")
                        elif value.get("response_type") == "answer":
                            body_text = value.get("content")
                        if isinstance(body_text, str) and body_text:
                            timings["first_text_seconds"] = round(
                                time.monotonic() - request_started, 3
                            )
                    if isinstance(value, dict) and value.get("type") in {
                        "complete",
                        "error",
                    }:
                        break
                    if terminal_event:
                        break
            return b"".join(parts)
    except urllib.error.HTTPError as error:
        detail = error.read(1024).decode("utf-8", "replace")
        raise RuntimeError(f"{path} HTTP {error.code}: {detail}") from None


def _summary(raw: bytes) -> dict[str, object]:
    """只打印协议结构与长度，原始答案留在受限文件。"""
    events: dict[str, int] = {}
    payload_types: dict[str, int] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
            events[event] = events.get(event, 0) + 1
        elif line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                kind = str(
                    payload.get("type")
                    or payload.get("response_type")
                    or payload.get("event")
                    or "?"
                )
                payload_types[kind] = payload_types.get(kind, 0) + 1
    return {
        "bytes": len(raw),
        "events": events,
        "payload_types": payload_types,
    }


def run_a(question: str) -> bytes:
    """走 A 当前公共自然问答协议。"""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    session = json.loads(_request(opener, A_ORIGIN, "/api/public/session", {}))
    csrf = session.get("csrf_token")
    if not isinstance(csrf, str):
        raise RuntimeError("A 公共会话缺少 CSRF")
    return _request(
        opener,
        A_ORIGIN,
        "/api/public/chat",
        {
            "query": question,
            "conversation_id": "ab-" + secrets.token_hex(16),
        },
        {
            "X-CSRF-Token": csrf,
            "X-Wanshitong-Stream-Protocol": "wanshitong-natural-sse-v1",
        },
        stream=True,
    )


def run_b(question: str, identity: Path, agent: Path | None) -> bytes:
    """走腾讯原版 KnowledgeQA，不启用 Agent。"""
    info = json.loads(identity.read_text(encoding="utf-8"))
    opener = urllib.request.build_opener()
    headers = {"Authorization": f"Bearer {info['token']}"}
    session = json.loads(
        _request(
            opener,
            B_ORIGIN,
            "/api/v1/sessions",
            {"title": "隔离AB冒烟"},
            headers,
        )
    )
    data = session.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise RuntimeError("B 会话缺少 ID")
    agent_id = None
    if agent is not None:
        config = json.loads(agent.read_text(encoding="utf-8"))
        agent_id = config["id"]
    return _request(
        opener,
        B_ORIGIN,
        f"/api/v1/knowledge-chat/{data['id']}",
        {
            "query": question,
            "knowledge_base_ids": [info["kb_id"]],
            "agent_enabled": False,
            "agent_id": agent_id,
            "disable_title": True,
        },
        headers,
        stream=True,
    )


def main() -> None:
    """执行指定一侧的同题流式冒烟并保存原始协议。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("side", choices=("A", "B"))
    parser.add_argument("--question", required=True)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--agent", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.side == "B" and args.identity is None:
        raise ValueError("B 需要独立账号身份文件")
    raw = (
        run_a(args.question)
        if args.side == "A"
        else run_b(args.question, args.identity, args.agent)
    )
    _write_new(args.output, raw)
    print(json.dumps({"side": args.side, **_summary(raw)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
