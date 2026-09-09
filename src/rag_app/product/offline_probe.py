"""在隔离 Product 容器内部执行离线 HTTP 验收。"""

from __future__ import annotations

import argparse
import io
import ipaddress
import json
import sys
import time
import zipfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_SMOKE_MARKER = "V305-OFFLINE-ALPHA-739"
_MIN_BOOTSTRAP_TOKEN_LENGTH = 20
_MAX_BOOTSTRAP_TOKEN_LENGTH = 1024


class OfflineProductProbeError(RuntimeError):
    """表示容器内 Product 离线验收未满足合同。"""


@dataclass(frozen=True, slots=True)
class OfflineProductProbeResult:
    """记录离线检索产生的非敏感运行身份。"""

    active_revision_id: str
    trace_id: str


def run_offline_product_probe(
    *,
    base_url: str,
    request_origin: str,
    bootstrap_token_file: Path,
    transport: httpx.BaseTransport | None = None,
) -> OfflineProductProbeResult:
    """在容器回环地址完成登录、DOCX 入库和检索。

    Args:
        base_url: 容器内 Product HTTP 回环地址。
        request_origin: 必须已列入 Product 白名单的回环 Origin。
        bootstrap_token_file: 容器内 Bootstrap Token 文件。
        transport: 测试时可注入的 HTTPX transport。

    Returns:
        活动索引 Revision 与查询 Trace 的非敏感身份。

    Raises:
        OfflineProductProbeError: 地址、凭据或 HTTP 结果不满足合同。

    """
    _validate_loopback_url(base_url, label="base URL")
    _validate_loopback_url(request_origin, label="Origin")
    bootstrap_token = _read_bootstrap_token(bootstrap_token_file)
    try:
        with httpx.Client(
            base_url=base_url,
            timeout=10,
            transport=transport,
        ) as client:
            _wait_for_live(client)
            index_response = client.get("/")
            index_response.raise_for_status()
            if 'id="root"' not in index_response.text:
                raise OfflineProductProbeError(
                    "clean-room 登录页未返回 React 根节点。"
                )
            login = client.post(
                "/api/v1/console/session",
                json={"bootstrap_token": bootstrap_token},
                headers={"Origin": request_origin},
            )
            login.raise_for_status()
            session = _json_object(login, "登录")
            csrf_token = session.get("csrf_token")
            if not isinstance(csrf_token, str):
                raise OfflineProductProbeError(
                    "clean-room Product 登录未返回会话身份。"
                )
            components = client.get("/api/v1/system/components")
            components.raise_for_status()
            component_items = components.json()
            if (
                not isinstance(component_items, (dict, list))
                or not component_items
            ):
                raise OfflineProductProbeError(
                    "clean-room Product 离线 smoke 无组件结果。"
                )
            write_headers = {
                "Origin": request_origin,
                "X-CSRF-Token": csrf_token,
            }
            project = client.post(
                "/api/v1/projects",
                json={"name": "V3-05 clean-room 项目"},
                headers={
                    **write_headers,
                    "Idempotency-Key": "v305-clean-room-project",
                },
            )
            project.raise_for_status()
            project_id = _required_json_id(project, "project_id", prefix="prj_")
            knowledge_base = client.post(
                f"/api/v1/projects/{project_id}/knowledge-bases",
                json={"name": "V3-05 clean-room 知识库"},
                headers={
                    **write_headers,
                    "Idempotency-Key": "v305-clean-room-kb",
                },
            )
            knowledge_base.raise_for_status()
            knowledge_base_id = _required_json_id(
                knowledge_base,
                "knowledge_base_id",
                prefix="kb_",
            )
            base = (
                f"/api/v1/projects/{project_id}/knowledge-bases/"
                f"{knowledge_base_id}"
            )
            upload = client.post(
                f"{base}/documents",
                params={"display_name": "v305-clean-room.docx"},
                content=_minimal_smoke_docx(),
                headers={
                    **write_headers,
                    "Content-Type": _DOCX_MEDIA_TYPE,
                    "Idempotency-Key": "v305-clean-room-document",
                },
            )
            upload.raise_for_status()
            job_id = _required_json_id(upload, "job_id", prefix="job_")
            completed = _wait_for_product_job(client, job_id)
            revision_id = completed.get("revision_id")
            if (
                completed.get("state") != "succeeded"
                or not isinstance(revision_id, str)
                or not revision_id.startswith("irev_")
            ):
                raise OfflineProductProbeError(
                    "clean-room Product DOCX 入库未成功。"
                )
            query = client.post(
                f"{base}:search",
                json={
                    "query": _SMOKE_MARKER,
                    "limit": 5,
                    "include_related_content": True,
                },
                headers=write_headers,
            )
            query.raise_for_status()
            result = _json_object(query, "离线检索")
            trace_id = result.get("trace_id")
            if (
                result.get("project_id") != project_id
                or result.get("knowledge_base_id") != knowledge_base_id
                or result.get("active_index_revision_id") != revision_id
                or not isinstance(trace_id, str)
                or not trace_id.startswith("trace_")
            ):
                raise OfflineProductProbeError(
                    "clean-room Product 离线检索身份不一致。"
                )
            evidence = result.get("evidence")
            related = result.get("related_contents")
            if not (
                (isinstance(evidence, list) and evidence)
                or (isinstance(related, list) and related)
            ):
                raise OfflineProductProbeError(
                    "clean-room Product 离线检索未命中合成 DOCX。"
                )
            return OfflineProductProbeResult(
                active_revision_id=revision_id,
                trace_id=trace_id,
            )
    except (httpx.HTTPError, json.JSONDecodeError) as error:
        raise OfflineProductProbeError(
            "clean-room Product HTTP smoke 失败。"
        ) from error


def _validate_loopback_url(value: str, *, label: str) -> None:
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as error:
        raise OfflineProductProbeError(
            f"clean-room {label} 必须是有效回环 HTTP 地址。"
        ) from error
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise OfflineProductProbeError(
            f"clean-room {label} 必须是有效回环 HTTP 地址。"
        )


def _read_bootstrap_token(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise OfflineProductProbeError("clean-room Bootstrap Token 文件无效。")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise OfflineProductProbeError(
            "clean-room Bootstrap Token 文件无法读取。"
        ) from error
    if not (
        _MIN_BOOTSTRAP_TOKEN_LENGTH <= len(token) <= _MAX_BOOTSTRAP_TOKEN_LENGTH
    ) or any(character.isspace() for character in token):
        raise OfflineProductProbeError("clean-room Bootstrap Token 格式无效。")
    return token


def _required_json_id(
    response: httpx.Response,
    field: str,
    *,
    prefix: str,
) -> str:
    value = _json_object(response, field).get(field)
    if not isinstance(value, str) or not value.startswith(prefix):
        raise OfflineProductProbeError(f"clean-room Product 缺少 {field}。")
    return value


def _json_object(response: httpx.Response, label: str) -> dict[str, object]:
    value = response.json()
    if not isinstance(value, dict):
        raise OfflineProductProbeError(f"clean-room Product {label} 响应无效。")
    return cast(dict[str, object], value)


def _wait_for_product_job(
    client: httpx.Client,
    job_id: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        response.raise_for_status()
        job = _json_object(response, "入库任务")
        if job.get("state") not in {"queued", "running"}:
            return job
        time.sleep(0.1)
    raise OfflineProductProbeError(
        "clean-room Product 入库任务未在时限内结束。"
    )


def _minimal_smoke_docx() -> bytes:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.'
        'openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "\n</Types>\n"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships">\n'
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>\n'
        "</Relationships>\n"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main">\n'
        "  <w:body><w:p><w:r><w:t>离线验收标记是 "
        f"{_SMOKE_MARKER}。"
        "</w:t></w:r></w:p><w:sectPr/></w:body>\n"
        "</w:document>\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", relationships),
            ("word/document.xml", document),
        ):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return buffer.getvalue()


def _wait_for_live(client: httpx.Client) -> None:
    deadline = time.monotonic() + 120
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = client.get("/live", timeout=2)
            response.raise_for_status()
            if response.json() == {"status": "live"}:
                return
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.25)
    raise OfflineProductProbeError(
        "clean-room Product /live 未在时限内通过。"
    ) from last_error


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8088",
    )
    parser.add_argument("--request-origin", required=True)
    parser.add_argument(
        "--bootstrap-token-file",
        type=Path,
        default=Path("/run/rag-secrets/admin-bootstrap-token"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行容器内离线 Product 验收并输出规范 JSON。

    Args:
        argv: 可选命令行参数。

    Returns:
        成功返回 0，合同失败返回 1。

    """
    arguments = _arguments(argv)
    try:
        result = run_offline_product_probe(
            base_url=arguments.base_url,
            request_origin=arguments.request_origin,
            bootstrap_token_file=arguments.bootstrap_token_file,
        )
    except OfflineProductProbeError as error:
        print(f"PRODUCT_OFFLINE_PROBE_FAILED: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            asdict(result),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
