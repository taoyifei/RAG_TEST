#!/usr/bin/env python3
"""按受控 Manifest 经湾事通管理员 HTTP API 导入 DOCX。"""

from __future__ import annotations

import argparse
import hashlib
import html
import http.cookiejar
import json
import os
import stat
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
EXPECTED_DOCUMENT_COUNT = 46
EXPECTED_PILOT_COUNT = 4
EXPECTED_SPACES = frozenset(
    {"01 科管", "02 人力", "03 综合", "04 开发中心"}
)
CONTINUED_STATES = frozenset({"queued", "running"})
TERMINAL_STATES = frozenset(
    {"succeeded", "failed_retryable", "failed_terminal", "cancelled"}
)
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DOCX_BYTES = 32 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_RETRY_AFTER_SECONDS = 120
MAX_SECRET_CHARS = 4096
MIN_PRINTABLE_CODEPOINT = 32
HTTP_TOO_MANY_REQUESTS = 429
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_POLL_SECONDS = 2.0


class ImportContractError(ValueError):
    """表示本地输入或服务端合同不满足导入要求。"""


class ApiError(RuntimeError):
    """只保存可安全写入报告的 HTTP 错误摘要。"""

    def __init__(
        self, status: int, code: str, message: str, *, retry_after: int = 0
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.safe_message = message[:500]
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    """一个已验证、可安全上传的 Manifest 条目。"""

    control_id: str
    literal_path: str
    api_path: str
    display_name: str
    media_type: str
    pilot: bool
    file_path: Path
    sha256: str
    idempotency_key: str
    metadata: dict[str, object]


@dataclass(slots=True)
class ImportResult:
    """不含正文和 Secret 的单文档导入结果。"""

    control_id: str
    source_relative_path: str
    sha256: str
    action: str = "pending"
    document_id: str | None = None
    job_id: str | None = None
    final_job_state: str | None = None
    retrievable: bool = False
    retried: bool = False
    error_code: str | None = None
    error_message: str | None = None


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """阻止认证请求被重定向到不同目标。"""

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


class WanshitongAdminClient:
    """维护 Cookie 与 CSRF 的最小管理员 API 客户端。"""

    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        self._base_url = _validated_base_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._csrf_token: str | None = None
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), _RejectRedirects()
        )

    def login(self, bootstrap_token: str) -> None:
        """换取管理员 Cookie，并只在内存保存 CSRF。"""
        payload = self._request_json(
            "POST",
            "/api/v1/console/session",
            data=json.dumps(
                {"bootstrap_token": bootstrap_token},
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            retry_rate_limit=False,
        )
        csrf = payload.get("csrf_token")
        if not isinstance(csrf, str) or not csrf:
            raise ImportContractError("管理员登录响应缺少 csrf_token。")
        self._csrf_token = csrf

    def list_documents(self) -> list[dict[str, object]]:
        """分页读取固定 Scope 的全部文档。"""
        documents: list[dict[str, object]] = []
        offset = 0
        while True:
            payload = self._request_json(
                "GET",
                "/api/v1/admin/wanshitong/documents",
                query={"page_size": "20", "offset": str(offset)},
            )
            items = payload.get("items")
            if not isinstance(items, list) or any(
                not isinstance(item, dict) for item in items
            ):
                raise ImportContractError("文档列表响应结构无效。")
            documents.extend(items)
            next_offset = payload.get("next_offset")
            if next_offset is None:
                return documents
            if not isinstance(next_offset, int) or next_offset <= offset:
                raise ImportContractError("文档列表 next_offset 无效。")
            offset = next_offset

    def upload(self, document: PreparedDocument) -> dict[str, object]:
        """以原始 DOCX 请求体提交一个 Universal Lifecycle Job。"""
        return self._request_json(
            "POST",
            "/api/v1/admin/wanshitong/documents",
            query={
                "source_relative_path": document.api_path,
                "metadata": json.dumps(
                    document.metadata,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
            data=document.file_path.read_bytes(),
            headers={
                "Content-Type": document.media_type,
                "Idempotency-Key": document.idempotency_key,
            },
        )

    def upload_version(
        self,
        document_id: str,
        document: PreparedDocument,
        *,
        failed_job_id: str,
    ) -> dict[str, object]:
        """在人工修复终态故障后，以稳定幂等键创建恢复版本。"""
        recovery_digest = hashlib.sha256(
            (
                document.idempotency_key
                + "\0"
                + document_id
                + "\0"
                + failed_job_id
                + "\0terminal-recovery-v1"
            ).encode("utf-8")
        ).hexdigest()
        return self._request_json(
            "POST",
            f"/api/v1/admin/wanshitong/documents/{document_id}/versions",
            query={
                "source_relative_path": document.api_path,
                "metadata": json.dumps(
                    document.metadata,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
            data=document.file_path.read_bytes(),
            headers={
                "Content-Type": document.media_type,
                "Idempotency-Key": "wb07r-recovery-" + recovery_digest,
            },
        )

    def refresh_template_catalog(
        self, document_id: str, document: PreparedDocument
    ) -> dict[str, object]:
        """为既有模板提交新版本，由服务端只索引目录项。"""
        return self._request_json(
            "POST",
            f"/api/v1/admin/wanshitong/documents/{document_id}/versions",
            query={
                "source_relative_path": document.api_path,
                "metadata": json.dumps(
                    document.metadata,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
            data=document.file_path.read_bytes(),
            headers={
                "Content-Type": document.media_type,
                "Idempotency-Key": (
                    "wb07r-template-catalog-v1-"
                    + hashlib.sha256(
                        (document.api_path + "\0" + document.sha256).encode(
                            "utf-8"
                        )
                    ).hexdigest()
                ),
            },
        )

    def get_job(self, job_id: str) -> dict[str, object]:
        """读取单个固定 Scope Job。"""
        return self._request_json(
            "GET", f"/api/v1/admin/wanshitong/jobs/{job_id}"
        )

    def retry_job(self, job_id: str) -> dict[str, object]:
        """只对服务端标记 retryable 的 Job 重试一次。"""
        return self._request_json(
            "POST", f"/api/v1/admin/wanshitong/jobs/{job_id}:retry"
        )

    def get_document(self, document_id: str) -> dict[str, object]:
        """读取文档并确认 active revision 已可检索。"""
        return self._request_json(
            "GET", f"/api/v1/admin/wanshitong/documents/{document_id}"
        )

    def _request_json(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        retry_rate_limit: bool = True,
    ) -> dict[str, object]:
        url = self._base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request_headers = {
            "Accept": "application/json",
            **({} if headers is None else headers),
        }
        if method not in {"GET", "HEAD", "OPTIONS"} and self._csrf_token:
            request_headers["X-CSRF-Token"] = self._csrf_token
        attempts = 2 if retry_rate_limit else 1
        for attempt in range(attempts):
            request = urllib.request.Request(  # noqa: S310
                url, data=data, headers=request_headers, method=method
            )
            try:
                with self._opener.open(
                    request, timeout=self._timeout_seconds
                ) as response:
                    return _json_object(response.read(MAX_ERROR_BYTES))
            except urllib.error.HTTPError as error:
                api_error = _api_error(error)
                if (
                    api_error.status == HTTP_TOO_MANY_REQUESTS
                    and attempt == 0
                    and retry_rate_limit
                ):
                    time.sleep(api_error.retry_after or 60)
                    continue
                raise api_error from None
            except urllib.error.URLError as error:
                raise ApiError(0, "NETWORK_ERROR", str(error.reason)) from None
        raise AssertionError("HTTP 重试循环未返回。")


def _validated_base_url(value: str) -> str:
    stripped = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(stripped)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ImportContractError(
            "--base-url 必须是无凭据、无路径的 HTTP(S) Origin。"
        )
    return stripped


def _json_object(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportContractError("服务端响应不是有效 JSON。") from None
    if not isinstance(value, dict):
        raise ImportContractError("服务端 JSON 响应必须是对象。")
    return value


def _api_error(error: urllib.error.HTTPError) -> ApiError:
    body = error.read(MAX_ERROR_BYTES)
    code = f"HTTP_{error.code}"
    message = "管理员 API 请求失败。"
    try:
        payload = _json_object(body)
        detail = payload.get("error")
        if isinstance(detail, dict):
            candidate_code = detail.get("code")
            candidate_message = detail.get("message")
            if isinstance(candidate_code, str) and candidate_code:
                code = candidate_code
            if isinstance(candidate_message, str) and candidate_message:
                message = candidate_message
    except ImportContractError:
        pass
    retry_header = error.headers.get("Retry-After", "")
    retry_after = int(retry_header) if retry_header.isdigit() else 0
    if retry_after < 0 or retry_after > MAX_RETRY_AFTER_SECONDS:
        retry_after = 0
    return ApiError(error.code, code, message, retry_after=retry_after)


def _read_bootstrap_token(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ImportContractError("Bootstrap Token 必须是普通非 symlink 文件。")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ImportContractError(
            "Bootstrap Token 文件权限必须为 0600 或更严格。"
        )
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if (
        not value
        or len(value) > MAX_SECRET_CHARS
        or "\n" in value
        or "\r" in value
    ):
        raise ImportContractError("Bootstrap Token 必须是单行非空文本。")
    return value


def load_and_validate_manifest(
    manifest_path: Path, root: Path
) -> tuple[PreparedDocument, ...]:
    """完整验证 46 项 Manifest、字面磁盘路径与 OOXML 包。"""
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ImportContractError("Manifest 必须是普通非 symlink JSON 文件。")
    raw = manifest_path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ImportContractError("Manifest 超过 1 MiB 上限。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ImportContractError("Manifest 不是有效 UTF-8 JSON。") from None
    if not isinstance(payload, dict):
        raise ImportContractError("Manifest 顶层必须是对象。")
    _validate_manifest_header(payload)
    documents = payload.get("documents")
    if (
        not isinstance(documents, list)
        or len(documents) != EXPECTED_DOCUMENT_COUNT
    ):
        raise ImportContractError("Manifest 必须恰好包含 46 个文档。")
    if root.is_symlink() or not root.is_dir():
        raise ImportContractError("语料根必须是普通目录且不能是 symlink。")
    resolved_root = root.resolve(strict=True)
    prepared = tuple(
        _prepare_document(item, resolved_root) for item in documents
    )
    identifiers = {item.control_id for item in prepared}
    expected_identifiers = {
        f"DOCX-{index:03d}" for index in range(1, EXPECTED_DOCUMENT_COUNT + 1)
    }
    if identifiers != expected_identifiers:
        raise ImportContractError(
            "Manifest 必须完整包含 DOCX-001 到 DOCX-046。"
        )
    if len({item.literal_path for item in prepared}) != EXPECTED_DOCUMENT_COUNT:
        raise ImportContractError(
            "Manifest 字面 source_relative_path 必须唯一。"
        )
    if len({item.api_path for item in prepared}) != EXPECTED_DOCUMENT_COUNT:
        raise ImportContractError("Manifest 规范路径发生 NFKC 冲突。")
    if sum(item.pilot for item in prepared) != EXPECTED_PILOT_COUNT:
        raise ImportContractError("Manifest 必须恰好标记 4 个 Pilot 文档。")
    spaces = {
        str(item.metadata["department_name"]) for item in prepared
    }
    if spaces != EXPECTED_SPACES:
        raise ImportContractError("Manifest 必须覆盖四个固定空间。")
    return prepared


def _validate_manifest_header(payload: dict[str, object]) -> None:
    if payload.get("schema_version") != "wanshitong-docx-demo-v1":
        raise ImportContractError("Manifest schema_version 不受支持。")
    counts = payload.get("inventory_counts")
    if (
        not isinstance(counts, dict)
        or counts.get("docx_included") != EXPECTED_DOCUMENT_COUNT
    ):
        raise ImportContractError("Manifest inventory_counts 不一致。")
    scope = payload.get("default_scope")
    expected_scope = {
        "single_hidden_project": True,
        "single_hidden_knowledge_base": True,
        "visibility_scope": "all_internal",
        "departments": [],
        "shortcut_id": None,
        "department_selector_visible": False,
        "shortcut_selector_visible": False,
    }
    if not isinstance(scope, dict) or any(
        scope.get(key) != value for key, value in expected_scope.items()
    ):
        raise ImportContractError("Manifest default_scope 不符合固定 Scope。")


def _prepare_document(
    value: object, resolved_root: Path
) -> PreparedDocument:
    if not isinstance(value, dict):
        raise ImportContractError("Manifest documents 条目必须是对象。")
    control_id = _required_text(value, "document_id")
    literal_path = _required_text(value, "source_relative_path")
    display_name = _required_text(value, "display_name")
    space = _required_text(value, "space")
    media_type = _required_text(value, "media_type")
    if media_type != DOCX_MEDIA_TYPE:
        raise ImportContractError(f"{control_id} 不是允许的 DOCX media type。")
    if (
        value.get("visibility_scope") != "all_internal"
        or value.get("default_search") is not True
        or value.get("department_filter_enabled") is not False
        or value.get("shortcut_filter_enabled") is not False
        or not isinstance(value.get("pilot"), bool)
    ):
        raise ImportContractError(f"{control_id} 的固定查询边界无效。")
    segments = _literal_path_segments(literal_path)
    api_path = "/".join(
        unicodedata.normalize("NFKC", segment.strip()) for segment in segments
    )
    api_segments = PurePosixPath(api_path).parts
    normalized_display = unicodedata.normalize("NFKC", display_name.strip())
    if api_segments[0] != space:
        raise ImportContractError(f"{control_id} 的 space 与路径不一致。")
    if html.unescape(api_segments[-1]) != normalized_display:
        raise ImportContractError(
            f"{control_id} 的 display_name 与路径不一致。"
        )
    if not api_segments[-1].casefold().endswith(".docx"):
        raise ImportContractError(f"{control_id} 不是 DOCX 路径。")
    file_path = resolved_root.joinpath(*segments)
    _validate_physical_file(file_path, resolved_root, control_id)
    _validate_docx(file_path, control_id)
    digest = _sha256_file(file_path)
    idempotency_digest = hashlib.sha256(
        api_path.encode("utf-8") + b"\0" + digest.encode("ascii")
    ).hexdigest()
    category_path = list(api_segments[1:-1])
    metadata: dict[str, object] = {
        "source_relative_path": api_path,
        "department_name": space,
        "category_path": category_path,
        "document_title": normalized_display[:-5],
        "topic_keys": [],
    }
    return PreparedDocument(
        control_id=control_id,
        literal_path=literal_path,
        api_path=api_path,
        display_name=normalized_display,
        media_type=media_type,
        pilot=bool(value["pilot"]),
        file_path=file_path,
        sha256=digest,
        idempotency_key="wb07r-" + idempotency_digest,
        metadata=metadata,
    )


def _required_text(value: dict[str, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ImportContractError(f"Manifest {key} 必须是非空字符串。")
    return candidate


def _literal_path_segments(value: str) -> tuple[str, ...]:
    if "\\" in value or value.startswith("/") or "\x00" in value:
        raise ImportContractError("Manifest 包含不安全的字面路径。")
    parts = PurePosixPath(value).parts
    if (
        not parts
        or "/".join(parts) != value
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            any(ord(char) < MIN_PRINTABLE_CODEPOINT for char in part)
            for part in parts
        )
    ):
        raise ImportContractError("Manifest 包含不安全的字面路径。")
    return parts


def _validate_physical_file(
    file_path: Path, resolved_root: Path, control_id: str
) -> None:
    current = resolved_root
    relative_parts = file_path.relative_to(resolved_root).parts
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise ImportContractError(f"{control_id} 的路径不能包含 symlink。")
    if not file_path.is_file():
        raise ImportContractError(f"{control_id} 的字面路径文件不存在。")
    resolved = file_path.resolve(strict=True)
    if resolved_root not in resolved.parents:
        raise ImportContractError(f"{control_id} 越出语料根目录。")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_DOCX_BYTES:
        raise ImportContractError(f"{control_id} 的文件大小无效。")


def _validate_docx(file_path: Path, control_id: str) -> None:
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(names) or archive.testzip() is not None:
                raise ImportContractError(f"{control_id} 不是完整 DOCX 包。")
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise ImportContractError(f"{control_id} 不是有效 DOCX ZIP。") from None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_by_path(
    documents: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for document in documents:
        path = document.get("source_relative_path")
        if path is None:
            continue
        if not isinstance(path, str):
            raise ImportContractError("服务端文档 source_relative_path 无效。")
        normalized = _normalize_api_path(path)
        if normalized in result:
            raise ImportContractError(
                "服务端存在重复的规范 source_relative_path。"
            )
        result[normalized] = document
    return result


def _normalize_api_path(value: str) -> str:
    parts = _literal_path_segments(value)
    return "/".join(
        unicodedata.normalize("NFKC", part.strip()) for part in parts
    )


def _wait_for_document(  # noqa: PLR0913
    client: WanshitongAdminClient,
    *,
    document_id: str,
    job_id: str,
    timeout_seconds: int,
    poll_seconds: float,
    allow_retry: bool,
) -> tuple[dict[str, object], dict[str, object], bool]:
    deadline = time.monotonic() + timeout_seconds
    retried = False
    while time.monotonic() < deadline:
        job = client.get_job(job_id)
        state = job.get("state")
        if not isinstance(state, str):
            raise ImportContractError("Job 响应缺少 state。")
        if state in CONTINUED_STATES:
            time.sleep(poll_seconds)
            continue
        if state not in TERMINAL_STATES:
            raise ImportContractError(f"Job 返回未知状态：{state}。")
        if state == "failed_retryable" and allow_retry and not retried:
            job = client.retry_job(job_id)
            retried = True
            time.sleep(poll_seconds)
            continue
        if state != "succeeded":
            return job, client.get_document(document_id), retried
        document = client.get_document(document_id)
        if document.get("retrievable") is True:
            return job, document, retried
        time.sleep(poll_seconds)
    raise ImportContractError("等待 Job 或可检索状态超时。")


def _resume_existing(  # noqa: PLR0913
    client: WanshitongAdminClient,
    existing: dict[str, object],
    document: PreparedDocument,
    result: ImportResult,
    *,
    wait: bool,
    recover_terminal: bool,
    timeout_seconds: int,
    poll_seconds: float,
) -> None:
    document_id = existing.get("document_id")
    if not isinstance(document_id, str):
        raise ImportContractError("既有文档缺少 document_id。")
    result.document_id = document_id
    if existing.get("retrievable") is True:
        result.action = "skipped_retrievable"
        result.final_job_state = "succeeded"
        result.retrievable = True
        latest = existing.get("latest_job")
        if isinstance(latest, dict) and isinstance(latest.get("job_id"), str):
            result.job_id = str(latest["job_id"])
        return
    latest_job = existing.get("latest_job")
    if not isinstance(latest_job, dict):
        raise ImportContractError("不可检索的既有文档缺少 latest_job。")
    job_id = latest_job.get("job_id")
    state = latest_job.get("state")
    if not isinstance(job_id, str) or not isinstance(state, str):
        raise ImportContractError("既有文档 latest_job 结构无效。")
    if state == "failed_terminal" and recover_terminal:
        receipt = client.upload_version(
            document_id,
            document,
            failed_job_id=job_id,
        )
        server_document = receipt.get("document")
        recovered_job = receipt.get("job")
        if not isinstance(server_document, dict) or not isinstance(
            recovered_job, dict
        ):
            raise ImportContractError("恢复版本回执缺少 document 或 job。")
        recovered_document_id = server_document.get("document_id")
        recovered_job_id = recovered_job.get("job_id")
        recovered_state = recovered_job.get("state")
        if (
            recovered_document_id != document_id
            or not isinstance(recovered_job_id, str)
            or not isinstance(recovered_state, str)
        ):
            raise ImportContractError("恢复版本回执身份字段无效。")
        result.action = "recovered_terminal"
        result.job_id = recovered_job_id
        result.final_job_state = recovered_state
    else:
        result.action = "resumed"
        result.job_id = job_id
        result.final_job_state = state
    if not wait:
        return
    job, final_document, retried = _wait_for_document(
        client,
        document_id=document_id,
        job_id=str(result.job_id),
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        allow_retry=True,
    )
    result.final_job_state = str(job.get("state"))
    result.retrievable = final_document.get("retrievable") is True
    result.retried = retried


def _upload_one(  # noqa: PLR0913
    client: WanshitongAdminClient,
    document: PreparedDocument,
    result: ImportResult,
    *,
    wait: bool,
    timeout_seconds: int,
    poll_seconds: float,
) -> None:
    receipt = client.upload(document)
    server_document = receipt.get("document")
    job = receipt.get("job")
    if not isinstance(server_document, dict) or not isinstance(job, dict):
        raise ImportContractError("上传回执缺少 document 或 job。")
    document_id = server_document.get("document_id")
    job_id = job.get("job_id")
    state = job.get("state")
    if not all(
        isinstance(value, str) for value in (document_id, job_id, state)
    ):
        raise ImportContractError("上传回执身份字段无效。")
    result.action = "uploaded"
    result.document_id = str(document_id)
    result.job_id = str(job_id)
    result.final_job_state = str(state)
    result.retrievable = server_document.get("retrievable") is True
    if not wait:
        return
    final_job, final_document, retried = _wait_for_document(
        client,
        document_id=str(document_id),
        job_id=str(job_id),
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        allow_retry=True,
    )
    result.final_job_state = str(final_job.get("state"))
    result.retrievable = final_document.get("retrievable") is True
    result.retried = retried


def _refresh_template(  # noqa: PLR0913
    client: WanshitongAdminClient,
    existing: dict[str, object],
    document: PreparedDocument,
    result: ImportResult,
    *,
    wait: bool,
    timeout_seconds: int,
    poll_seconds: float,
) -> None:
    """不删除逻辑文档，将既有模板切到目录项版本。"""
    document_id = existing.get("document_id")
    if not isinstance(document_id, str):
        raise ImportContractError("既有模板缺少 document_id。")
    receipt = client.refresh_template_catalog(document_id, document)
    job = receipt.get("job")
    if not isinstance(job, dict):
        raise ImportContractError("模板目录项版本回执缺少 Job。")
    job_id = job.get("job_id")
    state = job.get("state")
    if not isinstance(job_id, str) or not isinstance(state, str):
        raise ImportContractError("模板目录项版本 Job 身份无效。")
    result.document_id = document_id
    result.job_id = job_id
    result.final_job_state = state
    result.action = "refreshed_template_catalog"
    if wait:
        final_job, final_document, retried = _wait_for_document(
            client,
            document_id=document_id,
            job_id=job_id,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            allow_retry=True,
        )
        result.final_job_state = str(final_job.get("state"))
        result.retrievable = final_document.get("retrievable") is True
        result.retried = retried


def _is_template_path(path: str) -> bool:
    normalized = unicodedata.normalize("NFKC", path)
    return any("模板" in segment for segment in normalized.split("/"))


def run_import(arguments: argparse.Namespace) -> dict[str, object]:
    """执行一次 Pilot 或全量可恢复导入并返回安全报告。"""
    prepared = load_and_validate_manifest(arguments.manifest, arguments.root)
    refresh_templates = bool(getattr(arguments, "refresh_templates", False))
    selected = tuple(
        item
        for item in prepared
        if (
            _is_template_path(item.api_path)
            if refresh_templates
            else not arguments.pilot_only or item.pilot
        )
    )
    token = _read_bootstrap_token(arguments.bootstrap_token_file)
    client = WanshitongAdminClient(arguments.base_url)
    client.login(token)
    existing = _existing_by_path(client.list_documents())
    results: list[ImportResult] = []
    for document in selected:
        result = ImportResult(
            control_id=document.control_id,
            source_relative_path=document.api_path,
            sha256=document.sha256,
        )
        try:
            current = existing.get(document.api_path)
            if refresh_templates:
                if current is None:
                    raise ImportContractError(
                        "模板目录项刷新要求既有文档；先完成全量导入。"
                    )
                _refresh_template(
                    client,
                    current,
                    document,
                    result,
                    wait=arguments.wait,
                    timeout_seconds=arguments.timeout_seconds,
                    poll_seconds=arguments.poll_seconds,
                )
            elif current is not None:
                if not arguments.resume:
                    raise ImportContractError(
                        "文档已存在；请显式使用 --resume。"
                    )
                _resume_existing(
                    client,
                    current,
                    document,
                    result,
                    wait=arguments.wait,
                    recover_terminal=arguments.recover_terminal,
                    timeout_seconds=arguments.timeout_seconds,
                    poll_seconds=arguments.poll_seconds,
                )
            else:
                _upload_one(
                    client,
                    document,
                    result,
                    wait=arguments.wait,
                    timeout_seconds=arguments.timeout_seconds,
                    poll_seconds=arguments.poll_seconds,
                )
                existing[document.api_path] = {
                    "document_id": result.document_id,
                    "retrievable": result.retrievable,
                }
            if arguments.wait and (
                result.final_job_state != "succeeded"
                or not result.retrievable
            ):
                result.error_code = "INGESTION_NOT_RETRIEVABLE"
                result.error_message = "Job 未成功或文档仍不可检索。"
        except ApiError as error:
            result.action = "failed"
            result.error_code = error.code
            result.error_message = error.safe_message
        except ImportContractError as error:
            result.action = "failed"
            result.error_code = "IMPORT_CONTRACT_ERROR"
            result.error_message = str(error)[:500]
        results.append(result)
        print(
            json.dumps(
                asdict(result),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            flush=True,
        )
    report = _build_report(arguments, prepared, selected, results)
    _write_report(arguments.report, report)
    return report


def _build_report(
    arguments: argparse.Namespace,
    prepared: tuple[PreparedDocument, ...],
    selected: tuple[PreparedDocument, ...],
    results: list[ImportResult],
) -> dict[str, object]:
    failures = [item for item in results if item.error_code is not None]
    return {
        "schema_version": "wanshitong-docx-import-report-v1",
        "manifest_count": len(prepared),
        "selected_count": len(selected),
        "pilot_only": bool(arguments.pilot_only),
        "refresh_templates": bool(
            getattr(arguments, "refresh_templates", False)
        ),
        "resume": bool(arguments.resume),
        "wait": bool(arguments.wait),
        "registered": sum(item.document_id is not None for item in results),
        "uploaded": sum(item.action == "uploaded" for item in results),
        "resumed": sum(item.action == "resumed" for item in results),
        "recovered_terminal": sum(
            item.action == "recovered_terminal" for item in results
        ),
        "refreshed_template_catalog": sum(
            item.action == "refreshed_template_catalog" for item in results
        ),
        "skipped_retrievable": sum(
            item.action == "skipped_retrievable" for item in results
        ),
        "ingestion_succeeded": sum(
            item.final_job_state == "succeeded" for item in results
        ),
        "retrievable": sum(item.retrievable for item in results),
        "failed": len(failures),
        "items": [asdict(item) for item in results],
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    if path.is_symlink():
        raise ImportContractError("报告目标不能是 symlink。")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ImportContractError("报告父目录无效。")
    payload = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, payload.encode("utf-8") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 DOCX_ONLY_MANIFEST_46.json 导入湾事通语料。"
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--bootstrap-token-file", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pilot-only", action="store_true")
    parser.add_argument(
        "--refresh-templates",
        action="store_true",
        help="只给既有模板创建目录项版本；不删除文档。",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--recover-terminal",
        action="store_true",
        help="仅在故障已修复后，为 failed_terminal 文档创建新版本。",
    )
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """解析命令行、写出报告并返回自动化友好的退出码。"""
    arguments = _parser().parse_args(argv)
    if (
        arguments.timeout_seconds <= 0
        or arguments.poll_seconds <= 0
        or (arguments.recover_terminal and not arguments.resume)
        or (arguments.refresh_templates and arguments.pilot_only)
    ):
        print("import=failed reason=invalid-wait-settings", file=sys.stderr)
        return 2
    try:
        report = run_import(arguments)
    except (ImportContractError, ApiError, OSError) as error:
        print(
            "import=failed "
            f"reason={type(error).__name__} detail={str(error)[:500]}",
            file=sys.stderr,
        )
        return 2
    print(
        "import=completed "
        f"selected={report['selected_count']} "
        f"retrievable={report['retrievable']} failed={report['failed']}"
    )
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
