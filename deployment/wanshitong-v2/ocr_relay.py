"""将新版测试环境的 OCR 请求转发至已存在的固定模型端点。"""

from __future__ import annotations

import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_UPSTREAM = urlsplit(os.environ["WXD_OCR_UPSTREAM"])
_MAX_BODY_BYTES = 50 * 1024 * 1024
_ALLOWED_PATHS = frozenset({"/v1/models", "/v1/chat/completions"})


class OcrRelayHandler(BaseHTTPRequestHandler):
    """只连接启动配置指定的模型，不建立新的模型服务。"""

    def do_GET(self) -> None:
        """提供本地健康检查和模型列表。"""
        if self.path == "/health":
            self._respond(200, b'{"status":"ok"}', "application/json")
            return
        self._forward("GET")

    def do_POST(self) -> None:
        """转发 OCR 模型的非流式聊天请求。"""
        self._forward("POST")

    def _forward(self, method: str) -> None:
        if self.path not in _ALLOWED_PATHS:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 0 <= length <= _MAX_BODY_BYTES:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else None
        connection = http.client.HTTPConnection(
            _UPSTREAM.hostname, _UPSTREAM.port or 8080, timeout=180
        )
        try:
            connection.request(
                method,
                self.path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            data = response.read()
            self._respond(
                response.status,
                data,
                response.getheader("Content-Type", "application/json"),
            )
        except (OSError, http.client.HTTPException):
            self._respond(
                502, b'{"error":"OCR upstream unavailable"}', "application/json"
            )
        finally:
            connection.close()

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    """仅监听 Docker 宿主内部地址，入口由测试环境模型配置引用。"""
    if _UPSTREAM.scheme != "http" or not _UPSTREAM.hostname:
        raise ValueError("WXD_OCR_UPSTREAM 必须是固定 HTTP 模型地址。")
    bind_host = os.environ.get("WXD_OCR_BIND_HOST", "172.17.0.1")
    bind_port = int(os.environ.get("WXD_OCR_BIND_PORT", "18095"))
    with ThreadingHTTPServer((bind_host, bind_port), OcrRelayHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
