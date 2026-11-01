"""将 WeKnora 的 Cohere 重排请求转换为现网的 ``texts`` 协议。"""

# HTTP JSON 消息在边界逐字段校验，因此保留动态类型。
# ruff: noqa: ANN401

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class ProtocolError(ValueError):
    """请求或上游响应不符合重排协议。"""


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"非法数值：{value}")


def translate_request(body: Any) -> tuple[dict[str, Any], list[str]]:
    """保留输入顺序，把 ``documents`` 原样映射为 ``texts``。"""
    if not isinstance(body, dict):
        raise ProtocolError("请求必须是对象")
    query = body.get("query")
    documents = body.get("documents")
    if not isinstance(query, str) or not isinstance(documents, list):
        raise ProtocolError("缺少 query 或 documents")
    if not all(isinstance(document, str) for document in documents):
        raise ProtocolError("documents 必须是字符串数组")
    top_n = body.get("top_n")
    if top_n is not None and (
        isinstance(top_n, bool)
        or not isinstance(top_n, int)
        or top_n != len(documents)
    ):
        raise ProtocolError("只支持完整结果集的 top_n")
    return {"query": query, "texts": documents, "truncate": False}, documents


def translate_response(body: Any, documents: list[str]) -> dict[str, Any]:
    """核对全量下标和有限分数，不改变上游分数与排序。"""
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ProtocolError("上游缺少 results")
    results = body["results"]
    if len(results) != len(documents):
        raise ProtocolError("上游结果数量与输入不符")
    converted = []
    seen: set[int] = set()
    for result in results:
        if not isinstance(result, dict):
            raise ProtocolError("上游结果不是对象")
        index = result.get("index")
        score = result.get("score")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(documents)
            or index in seen
        ):
            raise ProtocolError("上游返回无效或重复下标")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise ProtocolError("上游返回非有限分数")
        seen.add(index)
        converted.append(
            {
                "index": index,
                "score": score,
                "document": {"text": documents[index]},
            }
        )
    return {"results": converted}


def _post_upstream(url: str, payload: dict[str, Any], timeout: float) -> Any:
    if not url.startswith("http://"):
        raise ProtocolError("只允许受控内网 HTTP 上游")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response, parse_constant=_reject_constant)


class RerankHandler(BaseHTTPRequestHandler):
    """只暴露重排和健康检查，不记录业务正文。"""

    upstream_url: str
    upstream_timeout: float

    def do_GET(self) -> None:
        """响应容器健康检查。"""
        if self.path != "/health":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._reply(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:
        """转换一次请求并核对上游完整结果。"""
        if self.path != "/rerank":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4 * 1024 * 1024:
                raise ProtocolError("请求大小超出允许范围")
            body = json.loads(
                self.rfile.read(length), parse_constant=_reject_constant
            )
            upstream_request, documents = translate_request(body)
        except (ValueError, json.JSONDecodeError, ProtocolError) as error:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        try:
            upstream_response = _post_upstream(
                self.upstream_url, upstream_request, self.upstream_timeout
            )
            response = translate_response(upstream_response, documents)
        except TimeoutError:
            self._reply(
                HTTPStatus.GATEWAY_TIMEOUT, {"error": "upstream_timeout"}
            )
            return
        except (urllib.error.URLError, urllib.error.HTTPError, ProtocolError):
            self._reply(HTTPStatus.BAD_GATEWAY, {"error": "upstream_invalid"})
            return
        self._reply(HTTPStatus.OK, response)

    def _reply(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    """启动仅供隔离实验网络访问的协议适配服务。"""
    upstream = os.environ["RERANK_UPSTREAM_URL"]
    if not upstream.startswith("http://"):
        raise SystemExit("RERANK_UPSTREAM_URL 必须是受控内网 HTTP 地址")
    RerankHandler.upstream_url = upstream
    RerankHandler.upstream_timeout = float(
        os.environ.get("RERANK_UPSTREAM_TIMEOUT_SECONDS", "30")
    )
    port = int(os.environ.get("RERANK_ADAPTER_PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), RerankHandler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
