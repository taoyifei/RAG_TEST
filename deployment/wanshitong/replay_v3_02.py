#!/usr/bin/env python3
"""在 8289 隔离候选入口串行复跑同一批私有问答用例。"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from pathlib import Path
from typing import Any, cast

_MAX_CASES = 32
_MAX_HISTORY_ITEMS = 8
_CANDIDATE_PORT = 8289
_TIMEOUT_SECONDS = 300


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """认证与问答请求不能跟随跨站跳转。"""

    def redirect_request(  # noqa: PLR0913, PLR0917
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port != _CANDIDATE_PORT
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("复跑仅允许服务器本机的 8289 HTTP Origin。")
    return value.rstrip("/")


def _cases(path: Path) -> list[dict[str, Any]]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_CASES:
        raise ValueError("用例须为 1–32 条 JSON 数组。")
    cases: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("用例项必须为对象。")
        identity = item.get("id")
        query = item.get("query")
        context = item.get("conversation_context", [])
        if (
            not isinstance(identity, str)
            or not identity
            or not isinstance(query, str)
            or not query.strip()
            or not isinstance(context, list)
            or len(context) > _MAX_HISTORY_ITEMS
            or any(not isinstance(value, str) for value in context)
        ):
            raise ValueError("用例 id、query 或会话上下文无效。")
        cases.append(
            {"id": identity, "query": query, "conversation_context": context}
        )
    if len({item["id"] for item in cases}) != len(cases):
        raise ValueError("用例 id 不得重复。")
    return cases


def _post(
    opener: urllib.request.OpenerDirector,
    url: str,
    payload: dict[str, object],
    *,
    csrf: str | None = None,
) -> urllib.response.addinfourl:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return cast(
        urllib.response.addinfourl,
        opener.open(request, timeout=_TIMEOUT_SECONDS),
    )


def _replay_one(  # noqa: PLR0913
    opener: urllib.request.OpenerDirector,
    origin: str,
    csrf: str,
    case: dict[str, Any],
    *,
    engine: str,
    rewrite_enabled: bool,
) -> dict[str, object]:
    started = time.monotonic()
    answer: list[str] = []
    final: dict[str, object] | None = None
    errors: list[dict[str, object]] = []
    references: list[dict[str, object]] = []
    with _post(
        opener,
        origin + "/api/v1/admin/wanshitong/candidate/chat",
        {
            "query": case["query"],
            "conversation_context": case["conversation_context"],
            "engine_id": engine,
            "rewrite_enabled": rewrite_enabled,
        },
        csrf=csrf,
    ) as response:
        event = ""
        for raw in response:
            line = raw.decode("utf-8").strip()
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if not isinstance(payload, dict):
                    raise ValueError("候选 SSE 事件结构无效。")
                if event == "answer_delta":
                    answer.append(str(payload.get("text", "")))
                elif event == "references":
                    items = payload.get("items")
                    if isinstance(items, list):
                        references = items
                elif event == "final":
                    if final is not None:
                        raise ValueError("候选 SSE 存在重复终态。")
                    final = payload
                elif event == "error":
                    errors.append(payload)
                event = ""
    return {
        "id": case["id"],
        "engine_id": engine,
        "rewrite_enabled": rewrite_enabled,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "answer_stream": "".join(answer),
        "references": references,
        "final": final,
        "errors": errors,
    }


def main() -> None:
    """管理员 Cookie 和私有问答正文只进入本机权限受控结果文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8289")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--engine",
        choices=("wk-standard-v1", "wk-standard-pc-v1"),
        default="wk-standard-pc-v1",
    )
    parser.add_argument("--rewrite-enabled", action="store_true")
    args = parser.parse_args()
    origin = _origin(args.origin)
    cases = _cases(args.cases)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        _RejectRedirects(),
    )
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("管理员 Token 文件为空。")
    with _post(
        opener,
        origin + "/api/v1/console/session",
        {"bootstrap_token": token},
    ) as response:
        login = json.load(response)
    csrf = login.get("csrf_token")
    if not isinstance(csrf, str) or not csrf:
        raise ValueError("管理员会话未返回 CSRF Token。")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(args.output, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        for case in cases:
            try:
                result = _replay_one(
                    opener,
                    origin,
                    csrf,
                    case,
                    engine=args.engine,
                    rewrite_enabled=args.rewrite_enabled,
                )
            except (
                ValueError,
                urllib.error.HTTPError,
                urllib.error.URLError,
            ) as error:
                result = {
                    "id": case["id"],
                    "client_error": type(error).__name__,
                }
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            errors = result.get("errors")
            error_count = len(errors) if isinstance(errors, list) else 0
            print(
                f"case={case['id']} final={result.get('final') is not None} "
                f"errors={error_count}"
            )


if __name__ == "__main__":
    main()
