#!/usr/bin/env python3
"""现场验证 Industry UI Session、Admin 边界与 Trace 明文合同。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path

_QUESTION = "计量器具的采购、验收和周期校准分别由谁负责？"
_HTTP_OK = 200
_HTTP_CREATED = 201
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_TRACE_ID_LENGTH = 32


class UiContractError(RuntimeError):
    """表示浏览器授权、Trace 明文或静态资产合同失败。"""


def verify_ui_contract(base_url: str) -> dict[str, object]:  # noqa: PLR0912, PLR0915
    """对真实 App 执行 UI、Admin、Trace 与安全头验收。

    Args:
        base_url: Industry App 本机 HTTP origin。
    Returns:
        不含 token 或问题正文的验收摘要。

    Raises:
        UiContractError: 任一授权、Cookie、Trace 或泄漏检查失败。

    """
    query_token = _required_environment("RAG_RUNTIME_CHECK_TOKEN")
    admin_token = _required_environment("RAG_RUNTIME_ADMIN_TOKEN")
    origin = base_url.rstrip("/")
    root_status, root_headers, root_body = _request("GET", f"{origin}/")
    if root_status != _HTTP_OK:
        raise UiContractError("FRONTEND_UNAVAILABLE")
    _verify_security_headers(root_headers)
    root_text = root_body.decode("utf-8")
    if "query-token" in root_text or query_token in root_text:
        raise UiContractError("QUERY_TOKEN_EXPOSED_IN_FRONTEND")
    asset_status, _, asset_body = _request("GET", f"{origin}/assets/app.js")
    if asset_status != _HTTP_OK:
        raise UiContractError("FRONTEND_ASSET_UNAVAILABLE")
    asset_text = asset_body.decode("utf-8")
    if query_token in asset_text or "queryToken" in asset_text:
        raise UiContractError("QUERY_TOKEN_EXPOSED_IN_ASSET")
    request_body = {
        "conversation_id": f"industry-serving-{uuid.uuid4().hex}",
        "question": _QUESTION,
    }
    unauthenticated, _, _ = _request("POST", f"{origin}/api/chat", request_body)
    if unauthenticated != _HTTP_UNAUTHORIZED:
        raise UiContractError("CHAT_WITHOUT_BEARER_NOT_REJECTED")
    bearer_status, _, bearer_body = _request(
        "POST",
        f"{origin}/api/chat",
        request_body,
        headers={"Authorization": f"Bearer {query_token}"},
        timeout=240,
    )
    if bearer_status != _HTTP_OK or query_token.encode("utf-8") in bearer_body:
        raise UiContractError("CHAT_BEARER_CONTRACT_FAILED")
    wrong_host, _, _ = _request(
        "POST",
        f"{origin}/api/ui/session",
        headers={"Host": "invalid.example", "Origin": origin},
    )
    if wrong_host != _HTTP_FORBIDDEN:
        raise UiContractError("UI_SESSION_WRONG_HOST_NOT_REJECTED")
    session_status, session_headers, session_body = _request(
        "POST",
        f"{origin}/api/ui/session",
        headers={"Origin": origin},
    )
    if session_status != _HTTP_CREATED:
        raise UiContractError("UI_SESSION_CREATE_FAILED")
    session = _json_object(session_body, "session")
    csrf = session.get("csrf_token")
    if not isinstance(csrf, str) or not csrf:
        raise UiContractError("UI_CSRF_INVALID")
    cookie = session_headers.get("set-cookie")
    if cookie is None:
        raise UiContractError("UI_COOKIE_MISSING")
    lower_cookie = cookie.lower()
    required_cookie = ("httponly", "samesite=strict", "path=/api/ui/")
    if any(item not in lower_cookie for item in required_cookie):
        raise UiContractError("UI_COOKIE_ATTRIBUTES_INVALID")
    if "secure" in lower_cookie or query_token in cookie:
        raise UiContractError("UI_COOKIE_SECRET_OR_SECURE_INVALID")
    cookie_pair = cookie.split(";", 1)[0]
    wrong_csrf, _, _ = _request(
        "POST",
        f"{origin}/api/ui/chat",
        request_body,
        headers={
            "Cookie": cookie_pair,
            "Origin": origin,
            "X-CSRF-Token": "wrong",
        },
    )
    if wrong_csrf != _HTTP_FORBIDDEN:
        raise UiContractError("WRONG_CSRF_NOT_REJECTED")
    forged_cookie, _, _ = _request(
        "POST",
        f"{origin}/api/ui/chat",
        request_body,
        headers={
            "Cookie": "rag_ui_session=forged",
            "Origin": origin,
            "X-CSRF-Token": csrf,
        },
    )
    if forged_cookie != _HTTP_UNAUTHORIZED:
        raise UiContractError("FORGED_COOKIE_NOT_REJECTED")
    expired_cookie, expired_csrf = _expired_cookie(query_token)
    expired_status, _, _ = _request(
        "POST",
        f"{origin}/api/ui/chat",
        request_body,
        headers={
            "Cookie": f"rag_ui_session={expired_cookie}",
            "Origin": origin,
            "X-CSRF-Token": expired_csrf,
        },
    )
    if expired_status != _HTTP_UNAUTHORIZED:
        raise UiContractError("EXPIRED_COOKIE_NOT_REJECTED")
    chat_status, _, chat_body = _request(
        "POST",
        f"{origin}/api/ui/chat",
        request_body,
        headers={
            "Cookie": cookie_pair,
            "Origin": origin,
            "X-CSRF-Token": csrf,
        },
        timeout=240,
    )
    if chat_status != _HTTP_OK:
        raise UiContractError("UI_CHAT_FAILED")
    trace_id = _trace_id(chat_body)
    query_admin, _, _ = _request(
        "GET",
        f"{origin}/api/admin/traces",
        headers={"Authorization": f"Bearer {query_token}"},
    )
    if query_admin != _HTTP_UNAUTHORIZED:
        raise UiContractError("QUERY_TOKEN_ACCESSED_ADMIN")
    session_admin, _, _ = _request(
        "GET",
        f"{origin}/api/admin/traces",
        headers={"Cookie": cookie_pair, "Origin": origin},
    )
    if session_admin != _HTTP_UNAUTHORIZED:
        raise UiContractError("UI_SESSION_ACCESSED_ADMIN")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    list_status, _, list_body = _request(
        "GET",
        f"{origin}/api/admin/traces?trace_id={trace_id}",
        headers=admin_headers,
    )
    if list_status != _HTTP_OK:
        raise UiContractError("ADMIN_TRACE_LIST_FAILED")
    trace_list = _json_object(list_body, "trace list")
    items = trace_list.get("items")
    if not isinstance(items, list) or len(items) != 1:
        raise UiContractError("TRACE_LIST_IDENTITY_INVALID")
    preview = (
        items[0].get("question_preview") if isinstance(items[0], dict) else None
    )
    if not isinstance(preview, str) or not _QUESTION.startswith(preview):
        raise UiContractError("TRACE_QUESTION_PREVIEW_MISSING")
    detail_status, _, detail_body = _request(
        "GET", f"{origin}/api/admin/traces/{trace_id}", headers=admin_headers
    )
    if detail_status != _HTTP_OK:
        raise UiContractError("ADMIN_TRACE_DETAIL_FAILED")
    detail = _json_object(detail_body, "trace detail")
    trace = detail.get("trace")
    if not isinstance(trace, dict) or trace.get("question_text") != _QUESTION:
        raise UiContractError("TRACE_QUESTION_TEXT_MISMATCH")
    expected_sha = hashlib.sha256(_QUESTION.encode("utf-8")).hexdigest()
    if trace.get("question_sha256") != expected_sha:
        raise UiContractError("TRACE_QUESTION_SHA256_MISMATCH")
    export_status, _, export_body = _request(
        "POST",
        f"{origin}/api/admin/traces/export",
        {"trace_ids": [trace_id]},
        headers=admin_headers,
    )
    if export_status != _HTTP_OK:
        raise UiContractError("TRACE_EXPORT_FAILED")
    _verify_export(export_body, trace_id, expected_sha)
    return {
        "admin_boundary": "verified",
        "question_sha256": expected_sha,
        "trace_id": trace_id,
        "ui_session": "verified",
    }


def verify_log(log_path: Path) -> dict[str, object]:
    """反查本轮请求后的日志增量是否包含问题或 token。

    Args:
        log_path: UI/Trace 请求完成后捕获的 App 日志增量。

    Returns:
        不含问题正文或 token 的日志验收摘要。

    Raises:
        UiContractError: 日志不存在、不是普通文件或包含敏感值。

    """
    if not log_path.is_file() or log_path.is_symlink():
        raise UiContractError("APP_LOG_PATH_INVALID")
    query_token = _required_environment("RAG_RUNTIME_CHECK_TOKEN")
    admin_token = _required_environment("RAG_RUNTIME_ADMIN_TOKEN")
    content = log_path.read_text(encoding="utf-8", errors="replace")
    if _QUESTION in content:
        raise UiContractError("QUESTION_TEXT_FOUND_IN_LOG")
    if query_token in content:
        raise UiContractError("QUERY_TOKEN_FOUND_IN_LOG")
    if admin_token in content:
        raise UiContractError("ADMIN_TOKEN_FOUND_IN_LOG")
    return {"log_redaction": "verified"}


def _verify_security_headers(headers: dict[str, str]) -> None:
    csp = headers.get("content-security-policy", "")
    required = (
        "base-uri 'none'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
    )
    if any(item not in csp for item in required):
        raise UiContractError("CONTENT_SECURITY_POLICY_INVALID")
    if headers.get("referrer-policy") != "no-referrer":
        raise UiContractError("REFERRER_POLICY_INVALID")
    if headers.get("x-content-type-options") != "nosniff":
        raise UiContractError("CONTENT_TYPE_OPTIONS_INVALID")


def _trace_id(payload: bytes) -> str:
    trace_ids: set[str] = set()
    final_trace_ids: list[str] = []
    event_types: list[object] = []
    for line in payload.decode("utf-8").splitlines():
        if not line:
            continue
        value = _json_object(line.encode("utf-8"), "NDJSON event")
        event_types.append(value.get("type"))
        trace_id = value.get("trace_id")
        if isinstance(trace_id, str):
            trace_ids.add(trace_id)
        if value.get("type") == "final":
            if not isinstance(trace_id, str):
                raise UiContractError("UI_CHAT_FINAL_EVENT_INVALID")
            final_trace_ids.append(trace_id)
    if len(trace_ids) != 1:
        raise UiContractError("UI_CHAT_TRACE_ID_INVALID")
    if len(final_trace_ids) != 1 or event_types[-1:] != ["final"]:
        raise UiContractError("UI_CHAT_FINAL_EVENT_INVALID")
    trace_id = trace_ids.pop()
    if final_trace_ids[0] != trace_id:
        raise UiContractError("UI_CHAT_FINAL_EVENT_INVALID")
    if len(trace_id) != _TRACE_ID_LENGTH or any(
        char not in "0123456789abcdef" for char in trace_id
    ):
        raise UiContractError("UI_CHAT_TRACE_ID_INVALID")
    return trace_id


def _verify_export(
    payload: bytes,
    trace_id: str,
    question_sha256: str,
) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
            expected = {"TRACE_EXPORT_MANIFEST.json", f"{trace_id}.json"}
            if names != expected:
                raise UiContractError("TRACE_EXPORT_EXACT_SET_INVALID")
            manifest = archive.read("TRACE_EXPORT_MANIFEST.json")
            if _QUESTION.encode("utf-8") in manifest:
                raise UiContractError("QUESTION_TEXT_IN_TRACE_MANIFEST")
            value = _json_object(manifest, "trace export manifest")
            traces = value.get("traces")
            if not isinstance(traces, list) or len(traces) != 1:
                raise UiContractError("TRACE_EXPORT_MANIFEST_INVALID")
            row = traces[0]
            trace_payload = archive.read(f"{trace_id}.json")
            if (
                not isinstance(row, dict)
                or set(row)
                != {
                    "created_at",
                    "json_file",
                    "json_sha256",
                    "question_sha256",
                    "status",
                    "trace_id",
                }
                or row.get("trace_id") != trace_id
                or row.get("json_file") != f"{trace_id}.json"
                or row.get("json_sha256")
                != hashlib.sha256(trace_payload).hexdigest()
                or row.get("question_sha256") != question_sha256
            ):
                raise UiContractError("TRACE_EXPORT_MANIFEST_INVALID")
            serialized = json.dumps(value, ensure_ascii=False)
            if (
                "question_text" in serialized
                or "question_preview" in serialized
            ):
                raise UiContractError("QUESTION_FIELD_IN_TRACE_MANIFEST")
    except zipfile.BadZipFile as error:
        raise UiContractError("TRACE_EXPORT_ZIP_INVALID") from error


def _expired_cookie(query_token: str) -> tuple[str, str]:
    csrf_proof = hashlib.sha256(b"expired-csrf-proof").hexdigest()
    payload = {
        "csrf_sha256": hashlib.sha256(csrf_proof.encode("utf-8")).hexdigest(),
        "exp": 1,
        "sid": "expired-session",
        "v": 1,
    }
    serialized = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(serialized).rstrip(b"=").decode("ascii")
    signing_key = hmac.new(
        query_token.encode("utf-8"),
        b"rag-ui-session-v1",
        hashlib.sha256,
    ).digest()
    signature = hmac.new(
        signing_key, encoded.encode("ascii"), hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{encoded}.{encoded_signature.decode('ascii')}", csrf_proof


def _request(
    method: str,
    url: str,
    body: Mapping[str, object] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            {key.lower(): value for key, value in error.headers.items()},
            error.read(),
        )
    except OSError as error:
        raise UiContractError("HTTP_REQUEST_FAILED") from error


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UiContractError(f"{label} JSON invalid") from error
    if not isinstance(value, dict):
        raise UiContractError(f"{label} must be object")
    return value


def _required_environment(key: str) -> str:
    value = os.environ.get(key)
    if value is None or not value:
        raise UiContractError(f"{key}_MISSING")
    return value


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    ui = commands.add_parser("verify-ui-trace")
    ui.add_argument("base_url")
    log = commands.add_parser("verify-log")
    log.add_argument("--log-path", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    """执行 UI Session、Admin、Trace 与泄漏现场检查。

    Returns:
        全部合同成立返回 0，否则返回 1 与稳定错误码。

    """
    arguments = _arguments()
    try:
        if arguments.command == "verify-ui-trace":
            report = verify_ui_contract(arguments.base_url)
        else:
            report = verify_log(arguments.log_path)
    except (OSError, UiContractError) as error:
        print(f"RAG_INDUSTRY_UI_CONTRACT_FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
