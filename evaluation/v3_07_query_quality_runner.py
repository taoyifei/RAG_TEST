"""V3-07 公开查询质量 Product API 评测与 B/C 对比。

运行器在独立数据目录中生成公开合成 DOCX，通过正式
Product API 完成入库与问答。目标源树只在命令行解析后动态
导入，因此同一运行器可比较修复前 worktree 与当前候选。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import httpx
from fastapi.testclient import TestClient

# 允许既使用 ``python -m``，也直接执行本文件。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.v3_07_query_quality_dataset import (
    CASES,
    DOCUMENTS,
    GATES,
    BlockSpec,
    DocumentSpec,
    QueryCase,
    dataset_sha256,
    validate_dataset,
)

_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_BOOTSTRAP_TOKEN = "-".join(
    ("v3", "07", "public", "synthetic", "bootstrap", "credential")
)
_QDRANT_API_KEY = "-".join(
    ("v3", "07", "public", "synthetic", "qdrant", "credential")
)
_ANSWERABLE_STATUSES = {"ANSWERABLE"}
_TERMINAL_ANSWERED = "ANSWERED"
_TERMINAL_REFUSED = "REFUSED"
_REPORT_SCHEMA_VERSION = "1"
_HTTP_OK = 200
_HTTP_ACCEPTED = 202
_HTTP_NO_CONTENT = 204
_HTTP_NOT_FOUND = 404
_MIN_PARAPHRASE_CASES = 2
_MIN_PERFORMANCE_REPLICATES = 3
_P95_REGRESSION_LIMIT = 1.20
_SOURCE_SPAN_REQUIRED_KEYS = {
    "chunk_start_char",
    "chunk_end_char",
    "is_citable",
    "span_type",
}
_QUALITY_LIMIT = 10
_TRACE_DETAIL_TIMEOUT_SECONDS = 3.0

_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_RELATIONSHIPS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-package.relationships+xml"
)
_DOCUMENT_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document.main+xml"
)
_STYLES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
)
_NUMBERING_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.numbering+xml"
)
_PACKAGE_RELATIONSHIPS_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_OFFICE_DOCUMENT_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/"
    "relationships/officeDocument"
)
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<Types xmlns="{_CONTENT_TYPES_NAMESPACE}">\n'
    '  <Default Extension="rels" '
    f'ContentType="{_RELATIONSHIPS_CONTENT_TYPE}"/>\n'
    '  <Default Extension="xml" ContentType="application/xml"/>\n'
    '  <Override PartName="/word/document.xml" '
    f'ContentType="{_DOCUMENT_CONTENT_TYPE}"/>\n'
    '  <Override PartName="/word/styles.xml" '
    f'ContentType="{_STYLES_CONTENT_TYPE}"/>\n'
    '  <Override PartName="/word/numbering.xml" '
    f'ContentType="{_NUMBERING_CONTENT_TYPE}"/>\n'
    "</Types>\n"
)
_ROOT_RELATIONSHIPS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<Relationships xmlns="{_PACKAGE_RELATIONSHIPS_NAMESPACE}">\n'
    '  <Relationship Id="rIdOfficeDocument" '
    f'Type="{_OFFICE_DOCUMENT_RELATIONSHIP}" '
    'Target="word/document.xml"/>\n'
    "</Relationships>\n"
)
_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{blocks}<w:sectPr/></w:body>
</w:document>
"""
_STYLES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:pPr><w:outlineLvl w:val="1"/></w:pPr>
  </w:style>
</w:styles>
"""
_NUMBERING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="7">
    <w:multiLevelType w:val="singleLevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="decimal"/>
      <w:lvlText w:val="%1."/>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="7"><w:abstractNumId w:val="7"/></w:num>
</w:numbering>
"""


@dataclass(frozen=True, slots=True)
class _TargetBindings:
    """动态导入的目标源树组合入口。"""

    create_product_app: object
    product_runtime_settings: object
    build_product_runtime: object
    build_offline_mock_transport: object
    initialize_master_key: object


@dataclass(frozen=True, slots=True)
class _OpenRuntime:
    """目标 Product Runtime 与已登录的同源客户端。"""

    runtime: object
    app: object
    client: TestClient
    csrf: str


@dataclass(frozen=True, slots=True)
class _StreamOutcome:
    """一次真流式查询的 final 与时延。"""

    payload: dict[str, object]
    header_ms: float
    first_protocol_ms: float | None
    first_content_ms: float | None
    total_ms: float


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    """以排他方式写入新 JSON 证据，避免覆盖既有运行。"""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _read_json(path: Path) -> dict[str, object]:
    """读取并验证 JSON object。"""
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根必须是 object：{path}")
    return cast(dict[str, object], value)


def _git_output(target_root: Path, *arguments: str) -> str:
    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("当前环境缺少 git 可执行文件。")
    result = subprocess.run(  # noqa: S603 - 参数仅来自固定评测调用。
        (git_executable, "-C", str(target_root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _target_identity(target_root: Path) -> dict[str, object]:
    return {
        "commit_sha": _git_output(target_root, "rev-parse", "HEAD"),
        "dirty": bool(_git_output(target_root, "status", "--porcelain")),
    }


def _activate_target(target_root: Path) -> _TargetBindings:
    """仅导入指定 worktree 的 ``rag_app``。

    Args:
        target_root: 带 ``src/rag_app`` 的仓库根目录。

    Returns:
        目标版本的组合入口集合。

    Raises:
        ValueError: 目标根无效，或当前进程已加载 ``rag_app``。

    """
    target_root = target_root.resolve(strict=True)
    target_src = (target_root / "src").resolve(strict=True)
    if not (target_src / "rag_app").is_dir():
        raise ValueError(f"目标源树缺少 src/rag_app：{target_root}")
    if any(
        name == "rag_app" or name.startswith("rag_app.") for name in sys.modules
    ):
        raise ValueError("同一进程禁止加载多个 rag_app 目标。")
    sys.path.insert(0, str(target_src))
    package = importlib.import_module("rag_app")
    package_path = Path(cast(str, package.__file__)).resolve(strict=True)
    if target_src not in package_path.parents:
        raise ValueError(f"rag_app 导入了错误源树：{package_path}")
    product = importlib.import_module("rag_app.api.product")
    runtime = importlib.import_module("rag_app.composition.product_runtime")
    provider = importlib.import_module("rag_app.product.provider_runtime")
    crypto = importlib.import_module("rag_app.product.crypto")
    return _TargetBindings(
        create_product_app=product.create_product_app,
        product_runtime_settings=runtime.ProductRuntimeSettings,
        build_product_runtime=runtime.build_product_runtime,
        build_offline_mock_transport=provider.build_offline_mock_transport,
        initialize_master_key=crypto.initialize_master_key,
    )


def _require_loopback_qdrant(qdrant_url: str) -> str:
    parsed = urlparse(qdrant_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Qdrant URL 必须使用 http/https。")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Lane L 只允许回环 Qdrant URL。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Qdrant URL 禁止携带凭据、query 或 fragment。")
    return qdrant_url.rstrip("/")


def _initialize_workspace(workspace: Path, bindings: _TargetBindings) -> None:
    """创建一个新的公开评测工作区。"""
    workspace.mkdir(parents=True, exist_ok=False)
    frontend = workspace / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text(
        "<!doctype html><title>V3-07 public evaluation</title>",
        encoding="utf-8",
    )
    bootstrap = workspace / "bootstrap-token"
    bootstrap.write_text(_BOOTSTRAP_TOKEN, encoding="utf-8")
    bootstrap.chmod(0o600)
    qdrant_key = workspace / "qdrant-api-key"
    qdrant_key.write_text(_QDRANT_API_KEY, encoding="utf-8")
    qdrant_key.chmod(0o600)
    initialize = cast(Callable[[Path], object], bindings.initialize_master_key)
    initialize(workspace / "master-key")


def _open_runtime(
    workspace: Path,
    qdrant_url: str,
    bindings: _TargetBindings,
) -> _OpenRuntime:
    """用离线 Transport 打开一个目标 Product Runtime。"""
    settings_type = cast(
        Callable[..., object],
        bindings.product_runtime_settings,
    )
    settings = settings_type(
        data_dir=workspace / "data",
        frontend_dir=workspace / "frontend",
        bootstrap_token_file=workspace / "bootstrap-token",
        master_key_file=workspace / "master-key",
        qdrant_mode="url",
        qdrant_url=qdrant_url,
        qdrant_api_key_file=workspace / "qdrant-api-key",
        history_save_body=False,
        debug_enabled=False,
    )
    builder = cast(Callable[..., object], bindings.build_product_runtime)
    runtime = builder(
        settings,
        transport_factory=bindings.build_offline_mock_transport,
    )
    app, client, csrf = _open_client(runtime, bindings)
    return _OpenRuntime(runtime=runtime, app=app, client=client, csrf=csrf)


def _open_client(
    runtime: object,
    bindings: _TargetBindings,
) -> tuple[object, TestClient, str]:
    """为同一 Runtime 创建带独立 HTTP 限流器的会话。"""
    app_factory = cast(Callable[[object], object], bindings.create_product_app)
    app = app_factory(runtime)
    client = TestClient(cast(Any, app))
    login = client.post(
        "/api/v1/console/session",
        json={"bootstrap_token": _BOOTSTRAP_TOKEN},
    )
    login.raise_for_status()
    csrf = login.json().get("csrf_token")
    if not isinstance(csrf, str):
        raise AssertionError("Product Session 未返回 CSRF Token。")
    return app, client, csrf


def _close_runtime(opened: _OpenRuntime) -> None:
    opened.client.close()
    cast(Any, opened.runtime).close()


def _paragraph_xml(text: str, *, style: str | None = None) -> str:
    properties = (
        "" if style is None else f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
    )
    return f"<w:p>{properties}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def _list_xml(items: Sequence[str]) -> str:
    return "".join(
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/>'
        '<w:numId w:val="7"/></w:numPr></w:pPr>'
        f"<w:r><w:t>{escape(item)}</w:t></w:r></w:p>"
        for item in items
    )


def _cell_xml(value: str) -> str:
    paragraphs = "".join(_paragraph_xml(part) for part in value.split("\n"))
    return f"<w:tc>{paragraphs}</w:tc>"


def _table_xml(rows: Sequence[Sequence[str]]) -> str:
    body: list[str] = ["<w:tbl><w:tblGrid/>"]
    for index, row in enumerate(rows):
        properties = "<w:trPr><w:tblHeader/></w:trPr>" if index == 0 else ""
        cells = "".join(_cell_xml(value) for value in row)
        body.append(f"<w:tr>{properties}{cells}</w:tr>")
    body.append("</w:tbl>")
    return "".join(body)


def _block_xml(block: BlockSpec) -> str:
    if block.kind == "heading":
        return _paragraph_xml(block.text, style=f"Heading{block.level}")
    if block.kind == "paragraph":
        return _paragraph_xml(block.text)
    if block.kind == "list":
        return _list_xml(block.items)
    if block.kind == "table":
        return _table_xml(block.rows)
    raise AssertionError(f"未支持的公开块类型：{block.kind}")


def _write_zip_entry(
    archive: zipfile.ZipFile,
    name: str,
    content: str,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    archive.writestr(info, content.encode("utf-8"))


def _document_bytes(document: DocumentSpec) -> bytes:
    """生成时间戳和 ZIP 顺序都固定的最小 DOCX。"""
    blocks = "".join(_block_xml(block) for block in document.blocks)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        _write_zip_entry(archive, "[Content_Types].xml", _CONTENT_TYPES)
        _write_zip_entry(archive, "_rels/.rels", _ROOT_RELATIONSHIPS)
        _write_zip_entry(
            archive,
            "word/document.xml",
            _DOCUMENT_XML.format(blocks=blocks),
        )
        _write_zip_entry(archive, "word/styles.xml", _STYLES_XML)
        _write_zip_entry(archive, "word/numbering.xml", _NUMBERING_XML)
    return output.getvalue()


def _wait_job(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AssertionError("Product Job 响应不是 object。")
        if payload.get("state") not in {"queued", "running"}:
            if payload.get("state") != "succeeded":
                raise AssertionError(f"Product Job 失败：{payload}")
            return cast(dict[str, object], payload)
        time.sleep(0.01)
    raise TimeoutError(f"Product Job 超时：{job_id}")


def _create_scope(opened: _OpenRuntime, split: str) -> tuple[str, str]:
    headers = {
        "X-CSRF-Token": opened.csrf,
        "Idempotency-Key": f"v3-07-project-{split}",
    }
    project = opened.client.post(
        "/api/v1/projects",
        headers=headers,
        json={"name": f"V3-07 {split} public project"},
    )
    project.raise_for_status()
    project_id = project.json().get("project_id")
    if not isinstance(project_id, str):
        raise AssertionError("Product Project 响应缺少 ID。")
    knowledge_base = opened.client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases",
        headers={
            "X-CSRF-Token": opened.csrf,
            "Idempotency-Key": f"v3-07-kb-{split}",
        },
        json={
            "name": f"V3-07 {split} public knowledge base",
            "description": "公开合成查询质量评测",
        },
    )
    knowledge_base.raise_for_status()
    knowledge_base_id = knowledge_base.json().get("knowledge_base_id")
    if not isinstance(knowledge_base_id, str):
        raise AssertionError("Product Knowledge Base 响应缺少 ID。")
    return project_id, knowledge_base_id


def _upload_document(
    opened: _OpenRuntime,
    project_id: str,
    knowledge_base_id: str,
    document: DocumentSpec,
) -> dict[str, object]:
    content = _document_bytes(document)
    response = opened.client.post(
        f"/api/v1/projects/{project_id}/knowledge-bases/"
        f"{knowledge_base_id}/documents",
        params={"display_name": document.display_name},
        content=content,
        headers={
            "X-CSRF-Token": opened.csrf,
            "Idempotency-Key": f"v3-07-document-{document.document_key}",
            "Content-Type": _MEDIA_TYPE,
        },
    )
    if response.status_code != _HTTP_ACCEPTED:
        raise AssertionError(
            f"{document.document_key}: 上传失败 "
            f"HTTP {response.status_code}: {response.text[:500]}"
        )
    job_id = response.json().get("job_id")
    if not isinstance(job_id, str):
        raise AssertionError(f"{document.document_key}: 缺少 job_id。")
    job = _wait_job(opened.client, job_id)
    document_id = job.get("document_id")
    revision_id = job.get("revision_id")
    if not isinstance(document_id, str) or not isinstance(revision_id, str):
        raise AssertionError(
            f"{document.document_key}: Job 缺少文档或 Revision。"
        )
    return {
        "document_id": document_id,
        "display_name": document.display_name,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "revision_id_after_upload": revision_id,
    }


def _canonical_span_record(span: object, chunk_text: str) -> dict[str, object]:
    """保存可验证的 canonical span 身份及其逐字片段。"""
    raw = cast(Any, span).model_dump(mode="json")
    start = int(raw["chunk_start_char"])
    end = int(raw["chunk_end_char"])
    return {
        "span": raw,
        "text": chunk_text[start:end],
    }


def _scope_identity(
    runtime: object,
    knowledge_base_id: str,
    documents: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    control = cast(Any, runtime).p09.control
    revision_id = control.active_revision_id(knowledge_base_id)
    if not isinstance(revision_id, str):
        raise AssertionError(f"{knowledge_base_id}: 没有活动 Revision。")
    document_key_by_id = {
        str(item["document_id"]): key for key, item in documents.items()
    }
    chunks = control.chunk_rows(revision_id)
    chunk_rows: list[dict[str, object]] = []
    for chunk in chunks:
        document_id = str(chunk.version.document_id)
        document_key = document_key_by_id.get(document_id)
        if document_key is None:
            raise AssertionError(f"Chunk 指向未知文档：{chunk.chunk_id}")
        chunk_rows.append(
            {
                "chunk_id": chunk.chunk_id,
                "content_sha256": chunk.content_sha256,
                "document_key": document_key,
                "role": str(chunk.role),
                "heading_path": list(chunk.heading_path),
                "source_spans": [
                    _canonical_span_record(span, chunk.citation_text)
                    for span in chunk.source_spans
                ],
            }
        )
    parse_rows = control.parse_rows(revision_id)
    parser_identities = sorted(
        {
            (
                str(parse_report.parser_id),
                str(parse_report.parser_version),
                str(document_ir.schema_version),
            )
            for document_ir, parse_report, _ in parse_rows
        }
    )
    chunker_fingerprints = sorted(
        {str(chunk.chunker_fingerprint) for chunk in chunks}
    )
    return {
        "active_revision_id": revision_id,
        "chunk_count": len(chunk_rows),
        "chunk_set_sha256": _canonical_sha256(chunk_rows),
        "chunks": chunk_rows,
        "parser_identities": [list(item) for item in parser_identities],
        "chunker_fingerprints": chunker_fingerprints,
    }


def prepare_workspace(
    target_root: Path,
    workspace: Path,
    qdrant_url: str,
    output: Path,
) -> dict[str, object]:
    """通过目标 Product API 建立公开评测语料。

    Args:
        target_root: 待加载实现的仓库根目录。
        workspace: 存放关闭态 Product 数据面的独占目录。
        qdrant_url: 仅允许回环地址的 Qdrant 端点。
        output: 写入去敏 seed manifest 的新文件路径。

    Returns:
        固定数据集、Parser、Chunk 与 Revision 身份的 seed manifest。

    """
    validate_dataset()
    qdrant_url = _require_loopback_qdrant(qdrant_url)
    bindings = _activate_target(target_root)
    _initialize_workspace(workspace, bindings)
    opened = _open_runtime(workspace, qdrant_url, bindings)
    scopes: dict[str, object] = {}
    try:
        for split in ("tuning", "holdout"):
            if split == "holdout":
                opened.client.close()
                app, client, csrf = _open_client(opened.runtime, bindings)
                opened = _OpenRuntime(
                    runtime=opened.runtime,
                    app=app,
                    client=client,
                    csrf=csrf,
                )
            project_id, knowledge_base_id = _create_scope(opened, split)
            documents: dict[str, dict[str, object]] = {}
            for document in DOCUMENTS:
                if document.split != split:
                    continue
                documents[document.document_key] = _upload_document(
                    opened,
                    project_id,
                    knowledge_base_id,
                    document,
                )
            scopes[split] = {
                "project_id": project_id,
                "knowledge_base_id": knowledge_base_id,
                "documents": documents,
                "identity": _scope_identity(
                    opened.runtime,
                    knowledge_base_id,
                    documents,
                ),
            }
    finally:
        _close_runtime(opened)
    manifest: dict[str, object] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "kind": "v3_07_public_product_seed",
        "dataset_sha256": dataset_sha256(),
        "target": _target_identity(target_root),
        "qdrant_endpoint_class": "loopback",
        "scopes": scopes,
    }
    _write_json(output, manifest)
    return manifest


def clone_workspace(source: Path, destination: Path) -> None:
    """复制已关闭的 seed，供单个目标与捕获模式独占。

    Args:
        source: 已完成关闭并通过完整性检查的 seed 工作区。
        destination: 不得预先存在的独占运行目录。

    Returns:
        无返回值；成功时创建字节一致的工作区副本。

    """
    source = source.resolve(strict=True)
    if destination.exists():
        raise FileExistsError(f"目标工作区已存在：{destination}")
    required = {
        "bootstrap-token",
        "master-key",
        "qdrant-api-key",
        "frontend",
        "data",
    }
    missing = sorted(name for name in required if not (source / name).exists())
    if missing:
        raise ValueError(f"seed 工作区不完整：{missing}")
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def _parse_sse_events(
    lines: Sequence[str],
) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    event_name: str | None = None
    for line in lines:
        if line.startswith("event:"):
            event_name = line.partition(":")[2].strip()
            continue
        if not line.startswith("data:") or event_name is None:
            continue
        payload = json.loads(line.partition(":")[2].strip())
        if not isinstance(payload, dict):
            raise AssertionError("SSE data 必须是 object。")
        events.append((event_name, cast(dict[str, object], payload)))
        event_name = None
    return events


def _stream_query(
    client: TestClient,
    endpoint: str,
    headers: Mapping[str, str],
    case: QueryCase,
    trace_mode: str,
) -> _StreamOutcome:
    started = time.perf_counter()
    first_protocol_ms: float | None = None
    first_content_ms: float | None = None
    lines: list[str] = []
    event_name: str | None = None
    with client.stream(
        "POST",
        endpoint,
        headers=dict(headers),
        json={
            "query": case.query,
            "limit": _QUALITY_LIMIT,
            "history_mode": "metadata_only",
            "trace_mode": trace_mode,
            "stream": True,
            "stream_protocol": "rag-answer-sse-v1",
        },
    ) as response:
        header_ms = (time.perf_counter() - started) * 1_000
        response_trace_id = response.headers.get("X-Trace-Id")
        if response.status_code != _HTTP_OK:
            response.read()
            raise AssertionError(
                f"{case.case_id}: 流式查询 HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )
        for line in response.iter_lines():
            elapsed_ms = (time.perf_counter() - started) * 1_000
            lines.append(line)
            if line.startswith("event:"):
                event_name = line.partition(":")[2].strip()
                if first_protocol_ms is None:
                    first_protocol_ms = elapsed_ms
            elif line.startswith("data:") and event_name in {"claim", "final"}:
                if first_content_ms is None:
                    first_content_ms = elapsed_ms
    total_ms = (time.perf_counter() - started) * 1_000
    events = _parse_sse_events(lines)
    final_events = [payload for name, payload in events if name == "final"]
    error_events = [payload for name, payload in events if name == "error"]
    if len(final_events) == 1 and not error_events:
        payload = final_events[0]
    elif not final_events and len(error_events) == 1:
        error = error_events[0]
        trace_id = error.get("trace_id") or response_trace_id
        if not isinstance(trace_id, str):
            raise AssertionError(f"{case.case_id}: SSE error 缺少 trace_id。")
        payload = {
            "trace_id": trace_id,
            "status": "EVALUATION_ERROR",
            "reason_code": error.get("code", "STREAM_ERROR"),
            "answer": None,
            "evidence": [],
            "_evaluation_error": True,
        }
    else:
        raise AssertionError(
            f"{case.case_id}: 预期唯一 final 或 error，"
            f"实际 final={len(final_events)}, error={len(error_events)}。"
        )
    return _StreamOutcome(
        payload=payload,
        header_ms=header_ms,
        first_protocol_ms=first_protocol_ms,
        first_content_ms=first_content_ms,
        total_ms=total_ms,
    )


def _wait_trace_detail(client: TestClient, trace_id: str) -> dict[str, object]:
    deadline = time.monotonic() + _TRACE_DETAIL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/admin/operational-traces/{trace_id}")
        if response.status_code == _HTTP_OK:
            payload = response.json()
            if isinstance(payload, dict):
                trace = payload.get("trace")
                if not isinstance(trace, dict) or trace.get(
                    "capture_complete",
                    True,
                ):
                    return cast(dict[str, object], payload)
        elif response.status_code != _HTTP_NOT_FOUND:
            response.raise_for_status()
        time.sleep(0.01)
    raise TimeoutError(f"Operational Trace 未在时限内结算：{trace_id}")


def _normalize_text(value: str) -> str:
    return "".join(
        character.casefold() for character in value if character.isalnum()
    )


def _evidence_items(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw = payload.get("evidence")
    if not isinstance(raw, list):
        return []
    return [
        cast(dict[str, object], item) for item in raw if isinstance(item, dict)
    ]


def _span_is_valid(  # noqa: PLR0911
    span: Mapping[str, object],
    citation_text: str,
    canonical_records: Sequence[Mapping[str, object]],
) -> bool:
    if not span.keys() >= _SOURCE_SPAN_REQUIRED_KEYS:
        return False
    start = span.get("chunk_start_char")
    end = span.get("chunk_end_char")
    if type(start) is not int or type(end) is not int:
        return False
    if start < 0 or end <= start or end > len(citation_text):
        return False
    cited_text = citation_text[start:end]
    if not cited_text or span.get("is_citable") is not True:
        return False
    stable_keys = (
        "schema_version",
        "span_type",
        "node_id",
        "source_anchor",
        "structural_path",
        "is_repeated",
        "is_citable",
        "metadata",
    )
    for record in canonical_records:
        canonical = record.get("span")
        canonical_text = record.get("text")
        if not isinstance(canonical, dict) or not isinstance(
            canonical_text, str
        ):
            continue
        if any(span.get(key) != canonical.get(key) for key in stable_keys):
            continue
        source_start = span.get("source_start_char")
        source_end = span.get("source_end_char")
        canonical_start = canonical.get("source_start_char")
        canonical_end = canonical.get("source_end_char")
        if canonical_start is None or canonical_end is None:
            if (
                source_start is None
                and source_end is None
                and cited_text == canonical_text.strip()
            ):
                return True
            continue
        if not all(
            type(value) is int
            for value in (
                source_start,
                source_end,
                canonical_start,
                canonical_end,
            )
        ):
            continue
        source_start_int = cast(int, source_start)
        source_end_int = cast(int, source_end)
        canonical_start_int = cast(int, canonical_start)
        canonical_end_int = cast(int, canonical_end)
        if not (
            canonical_start_int
            <= source_start_int
            < source_end_int
            <= canonical_end_int
            and source_end_int - source_start_int == len(cited_text)
        ):
            continue
        offset = source_start_int - canonical_start_int
        if canonical_text[offset : offset + len(cited_text)] == cited_text:
            return True
    return False


def _evidence_is_supported(item: Mapping[str, object]) -> bool:
    """读取 API JSON 中冻结 metadata 的直接支持状态。"""
    raw_metadata = item.get("metadata")
    try:
        metadata = dict(cast(Any, raw_metadata))
    except (TypeError, ValueError):
        return False
    raw_support = metadata.get("answer_support")
    try:
        support = dict(cast(Any, raw_support))
    except (TypeError, ValueError):
        return False
    return support.get("status") == "SUPPORTED"


def _score_case(  # noqa: PLR0913, PLR0915, PLR0917
    case: QueryCase,
    payload: Mapping[str, object],
    history: Mapping[str, object],
    trace: Mapping[str, object],
    document_key_by_id: Mapping[str, str],
    chunk_by_id: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    answer = payload.get("answer")
    answer_text = answer if isinstance(answer, str) else ""
    status = str(payload.get("status", ""))
    predicted_answerable = bool(answer_text) and status in _ANSWERABLE_STATUSES
    evidence = _evidence_items(payload)
    expected_documents = set(case.expected_document_keys)
    evidence_document_keys = [
        document_key_by_id.get(str(item.get("document_id")), "UNKNOWN")
        for item in evidence
    ]
    evidence_chunk_ids = {str(item.get("chunk_id")) for item in evidence}
    normalized_answer = _normalize_text(answer_text)
    actual_answer_type = str(payload.get("requested_answer_type", ""))
    answer_type_correct = actual_answer_type == case.expected_answer_type
    fragment_hits = {
        fragment: _normalize_text(fragment) in normalized_answer
        for fragment in case.expected_answer_fragments
    }
    forbidden_hits = [
        fragment
        for fragment in case.forbidden_fragments
        if _normalize_text(fragment) in normalized_answer
    ]
    answer_correct = (
        predicted_answerable
        and answer_type_correct
        and all(fragment_hits.values())
        and not forbidden_hits
        if case.answerable
        else not predicted_answerable
    )
    citation_source_correct = (
        bool(evidence)
        and all(key in expected_documents for key in evidence_document_keys)
        if case.answerable
        else not evidence
    )
    citation_spans_valid = True
    for item in evidence:
        chunk = chunk_by_id.get(str(item.get("chunk_id")))
        spans = item.get("source_spans")
        citation_text = item.get("citation_text")
        if (
            chunk is None
            or not isinstance(spans, list)
            or not spans
            or not isinstance(citation_text, str)
        ):
            citation_spans_valid = False
            break
        raw_records = chunk.get("source_spans")
        canonical_records = (
            [
                cast(dict[str, object], value)
                for value in raw_records
                if isinstance(value, dict)
            ]
            if isinstance(raw_records, list)
            else []
        )
        if not all(
            isinstance(span, dict)
            and _span_is_valid(span, citation_text, canonical_records)
            for span in spans
        ):
            citation_spans_valid = False
            break
    trace_record = trace.get("trace")
    trace_record = trace_record if isinstance(trace_record, dict) else {}
    history_status = str(history.get("status", ""))
    trace_status = str(trace_record.get("status", ""))
    expected_terminal = (
        _TERMINAL_ANSWERED
        if predicted_answerable
        else (
            "FAILED"
            if payload.get("_evaluation_error") is True
            else _TERMINAL_REFUSED
        )
    )
    status_parity = history_status == trace_status == expected_terminal
    decisions = trace.get("candidate_decisions")
    decisions = decisions if isinstance(decisions, list) else []
    retrieval_rows = [
        item
        for item in decisions
        if isinstance(item, dict)
        and item.get("stage") in {"channel", "fusion", "rerank"}
        and isinstance(item.get("chunk_id"), str)
    ]
    evidence_retrieval_path = [
        {
            key: item.get(key)
            for key in ("stage", "channel", "selected", "reason_code", "rank")
        }
        for item in retrieval_rows
        if str(item.get("chunk_id")) in evidence_chunk_ids
    ]
    raw_confidence = payload.get("confidence")
    confidence = raw_confidence if isinstance(raw_confidence, dict) else {}
    retrieved_document_keys = {
        str(chunk_by_id[str(item["chunk_id"])]["document_key"])
        for item in retrieval_rows
        if str(item["chunk_id"]) in chunk_by_id
    }
    target_retrieved = (
        bool(expected_documents & retrieved_document_keys)
        if case.answerable
        else True
    )
    ranked = [
        item
        for item in retrieval_rows
        if item.get("stage") == "rerank" and type(item.get("rank")) is int
    ]
    recall_at_10: bool | None = None
    if ranked and case.answerable:
        top = sorted(ranked, key=lambda item: int(cast(int, item["rank"])))[:10]
        top_documents = {
            str(chunk_by_id[str(item["chunk_id"])]["document_key"])
            for item in top
            if str(item["chunk_id"]) in chunk_by_id
        }
        recall_at_10 = bool(expected_documents & top_documents)
    combined_evidence = _normalize_text(
        " ".join(str(item.get("citation_text", "")) for item in evidence)
    )
    support_fragment_recall = (
        sum(
            _normalize_text(fragment) in combined_evidence
            for fragment in case.expected_source_fragments
        )
        / len(case.expected_source_fragments)
        if case.expected_source_fragments
        else 1.0
    )
    relevant_evidence = sum(
        key in expected_documents and _evidence_is_supported(item)
        for key, item in zip(evidence_document_keys, evidence, strict=True)
    )
    support_precision = (
        relevant_evidence / len(evidence)
        if evidence
        else (0.0 if case.answerable else 1.0)
    )
    hard_violation = case.slice_name == "hard_constraint" and not (
        answer_correct
        and citation_source_correct
        and citation_spans_valid
        and status_parity
    )
    return {
        "case_id": case.case_id,
        "slice": case.slice_name,
        "paraphrase_group": case.paraphrase_group,
        "expected_answer_type": case.expected_answer_type,
        "actual_answer_type": actual_answer_type,
        "answer_type_correct": answer_type_correct,
        "answerable": case.answerable,
        "status": status,
        "reason_code": payload.get("reason_code"),
        "evaluation_error": payload.get("_evaluation_error") is True,
        "generation_mode": payload.get("generation_mode"),
        "query_semantic_source": payload.get("query_semantic_source"),
        "predicted_answerable": predicted_answerable,
        "answer_correct": answer_correct,
        "fragment_hits": fragment_hits,
        "forbidden_hits": forbidden_hits,
        "citation_source_correct": citation_source_correct,
        "citation_spans_valid": citation_spans_valid,
        "evidence_count": len(evidence),
        "evidence_document_keys": evidence_document_keys,
        "evidence_retrieval_path": evidence_retrieval_path,
        "target_retrieved": target_retrieved,
        "retrieval_recall_at_10": recall_at_10,
        "support_fragment_recall": round(support_fragment_recall, 6),
        "support_precision": round(support_precision, 6),
        "confidence_reason_codes": confidence.get("reason_codes", []),
        "confidence_feature_values": confidence.get("feature_values", []),
        "history_status": history_status,
        "trace_status": trace_status,
        "status_parity": status_parity,
        "hard_constraint_violation": hard_violation,
        "provider_call_count": _provider_call_count(history),
        "cache_hit": bool(payload.get("cache_hit")),
        "result_origin": payload.get("result_origin"),
    }


def _provider_call_count(history: Mapping[str, object]) -> int:
    usage = history.get("provider_usage")
    if not isinstance(usage, list):
        return 0
    return sum(
        int(item.get("call_count", 0))
        for item in usage
        if isinstance(item, dict) and type(item.get("call_count", 0)) is int
    )


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 3)


def _resource_snapshot() -> dict[str, object]:
    status: dict[str, str] = {}
    status_path = Path("/proc/self/status")
    if status_path.exists():
        for line in status_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                status[key] = value.strip()
    task_path = Path("/proc/self/task")
    fd_path = Path("/proc/self/fd")
    return {
        "process_cpu_seconds": round(time.process_time(), 6),
        "rss": status.get("VmRSS"),
        "rss_peak": status.get("VmHWM"),
        "threads": (
            len(tuple(task_path.iterdir())) if task_path.is_dir() else None
        ),
        "fds": len(tuple(fd_path.iterdir())) if fd_path.is_dir() else None,
    }


def _hardware_identity() -> dict[str, object]:
    cpu_model = "unknown"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(encoding="utf-8").splitlines():
            if line.casefold().startswith("model name"):
                cpu_model = line.partition(":")[2].strip()
                break
    raw = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "logical_cpu_count": os.cpu_count(),
        "cpu_model": cpu_model,
    }
    return {**raw, "fingerprint": _canonical_sha256(raw)}


def _metrics(cases: Sequence[Mapping[str, object]]) -> dict[str, object]:
    answerable = [item for item in cases if item["answerable"] is True]
    unanswerable = [item for item in cases if item["answerable"] is False]
    groups: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    slices: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for item in cases:
        groups[str(item["paraphrase_group"])].append(item)
        slices[str(item["slice"])].append(item)
    multi_groups = [
        items
        for items in groups.values()
        if len(items) >= _MIN_PARAPHRASE_CASES
    ]

    def _rate(items: Sequence[Mapping[str, object]], key: str) -> float:
        if not items:
            return 1.0
        return round(sum(bool(item[key]) for item in items) / len(items), 6)

    slice_metrics = {
        name: {
            "case_count": len(items),
            "answer_correct_rate": _rate(items, "answer_correct"),
            "citation_source_precision": _rate(
                items,
                "citation_source_correct",
            ),
            "status_parity": _rate(items, "status_parity"),
        }
        for name, items in sorted(slices.items())
    }
    retrieval_values = [
        bool(item["retrieval_recall_at_10"])
        for item in answerable
        if item["retrieval_recall_at_10"] is not None
    ]
    return {
        "case_count": len(cases),
        "answerable_count": len(answerable),
        "unanswerable_count": len(unanswerable),
        "answerable_accuracy": _rate(answerable, "answer_correct"),
        "false_refusal_rate": round(
            sum(not bool(item["predicted_answerable"]) for item in answerable)
            / max(1, len(answerable)),
            6,
        ),
        "false_answer_rate": round(
            sum(bool(item["predicted_answerable"]) for item in unanswerable)
            / max(1, len(unanswerable)),
            6,
        ),
        "citation_source_precision": _rate(
            answerable,
            "citation_source_correct",
        ),
        "citation_span_validity": _rate(
            answerable,
            "citation_spans_valid",
        ),
        "paraphrase_consistency": round(
            sum(
                all(
                    bool(item["answer_correct"])
                    and bool(item["citation_source_correct"])
                    for item in items
                )
                for items in multi_groups
            )
            / max(1, len(multi_groups)),
            6,
        ),
        "paraphrase_group_count": len(multi_groups),
        "hard_constraint_violations": sum(
            bool(item["hard_constraint_violation"]) for item in cases
        ),
        "status_parity": _rate(cases, "status_parity"),
        "retrieval_recall_at_10": (
            round(sum(retrieval_values) / len(retrieval_values), 6)
            if retrieval_values
            else None
        ),
        "target_document_recall": _rate(answerable, "target_retrieved"),
        "support_set_recall": round(
            sum(
                float(cast(float | int, item["support_fragment_recall"]))
                for item in answerable
            )
            / max(1, len(answerable)),
            6,
        ),
        "support_set_precision": round(
            sum(
                float(cast(float | int, item["support_precision"]))
                for item in answerable
            )
            / max(1, len(answerable)),
            6,
        ),
        "provider_call_count": sum(
            int(cast(int, item["provider_call_count"])) for item in cases
        ),
        "slice_metrics": slice_metrics,
    }


def _gate_results(metrics: Mapping[str, object]) -> dict[str, object]:
    checks = {
        "answerable_accuracy": float(
            cast(float | int, metrics["answerable_accuracy"])
        )
        >= float(GATES["answerable_accuracy_min"]),
        "false_refusal_rate": float(
            cast(float | int, metrics["false_refusal_rate"])
        )
        <= float(GATES["false_refusal_rate_max"]),
        "false_answer_rate": float(
            cast(float | int, metrics["false_answer_rate"])
        )
        <= float(GATES["false_answer_rate_max"]),
        "citation_source_precision": float(
            cast(float | int, metrics["citation_source_precision"])
        )
        >= float(GATES["citation_source_precision_min"]),
        "citation_span_validity": float(
            cast(float | int, metrics["citation_span_validity"])
        )
        >= float(GATES["citation_span_validity_min"]),
        "paraphrase_consistency": float(
            cast(float | int, metrics["paraphrase_consistency"])
        )
        >= float(GATES["paraphrase_consistency_min"]),
        "hard_constraint_violations": int(
            cast(int, metrics["hard_constraint_violations"])
        )
        <= int(GATES["hard_constraint_violations_max"]),
        "status_parity": float(cast(float | int, metrics["status_parity"]))
        >= float(GATES["status_parity_min"]),
    }
    return {"passed": all(checks.values()), "checks": checks, "gates": GATES}


async def _singleflight_wave(
    app: object,
    project_id: str,
    knowledge_base_id: str,
    query: str,
) -> dict[str, object]:
    transport = httpx.ASGITransport(
        app=cast(Any, app),
        client=("127.0.0.1", 57_307),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        timeout=30,
    ) as client:
        login = await client.post(
            "/api/v1/console/session",
            json={"bootstrap_token": _BOOTSTRAP_TOKEN},
        )
        login.raise_for_status()
        csrf = login.json().get("csrf_token")
        if not isinstance(csrf, str):
            raise AssertionError("Singleflight Session 缺少 CSRF。")
        start = asyncio.Event()

        async def _request() -> dict[str, object]:
            await start.wait()
            response = await client.post(
                f"/api/v1/projects/{project_id}/knowledge-bases/"
                f"{knowledge_base_id}:answer",
                headers={"X-CSRF-Token": csrf},
                json={
                    "query": query,
                    "limit": _QUALITY_LIMIT,
                    "history_mode": "metadata_only",
                    "trace_mode": "SAFE",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise AssertionError("Singleflight 响应不是 object。")
            return cast(dict[str, object], payload)

        tasks = [asyncio.create_task(_request()) for _ in range(8)]
        await asyncio.sleep(0)
        start.set()
        payloads = await asyncio.gather(*tasks)
        revoked = await client.delete(
            "/api/v1/console/session",
            headers={"X-CSRF-Token": csrf},
        )
        if revoked.status_code != _HTTP_NO_CONTENT:
            raise AssertionError("Singleflight Session 撤销失败。")
    return {
        "request_count": len(payloads),
        "origins": dict(
            sorted(
                Counter(
                    str(item.get("result_origin")) for item in payloads
                ).items()
            )
        ),
        "roles": dict(
            sorted(
                Counter(
                    str(item.get("singleflight_role")) for item in payloads
                ).items()
            )
        ),
        "provider_call_count": sum(
            int(
                cast(
                    int,
                    cast(
                        dict[str, object],
                        item.get("diagnostics_summary", {}),
                    ).get("provider_call_count", 0),
                )
            )
            for item in payloads
            if isinstance(item.get("diagnostics_summary"), dict)
        ),
    }


def _run_case(  # noqa: PLR0913, PLR0917
    opened: _OpenRuntime,
    endpoint: str,
    headers: Mapping[str, str],
    case: QueryCase,
    trace_mode: str,
    document_key_by_id: Mapping[str, str],
    chunk_by_id: Mapping[str, Mapping[str, object]],
    *,
    measure_warm: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    streamed = _stream_query(opened.client, endpoint, headers, case, trace_mode)
    trace_id = streamed.payload.get("trace_id")
    if not isinstance(trace_id, str):
        raise AssertionError(f"{case.case_id}: final 缺少 trace_id。")
    history_response = opened.client.get(f"/api/v1/history/{trace_id}")
    history_response.raise_for_status()
    history = history_response.json()
    if not isinstance(history, dict):
        raise AssertionError(f"{case.case_id}: History 不是 object。")
    trace = _wait_trace_detail(opened.client, trace_id)
    score = _score_case(
        case,
        streamed.payload,
        cast(dict[str, object], history),
        trace,
        document_key_by_id,
        chunk_by_id,
    )
    warm_ms: float | None = None
    warm_cache_hit: bool | None = None
    if measure_warm:
        warm_started = time.perf_counter()
        warm = opened.client.post(
            endpoint,
            headers=dict(headers),
            json={
                "query": case.query,
                "limit": _QUALITY_LIMIT,
                "history_mode": "metadata_only",
                "trace_mode": trace_mode,
            },
        )
        warm_ms = (time.perf_counter() - warm_started) * 1_000
        warm_payload = warm.json()
        if not isinstance(warm_payload, dict):
            raise AssertionError(f"{case.case_id}: warm 响应不是 object。")
        warm_cache_hit = (
            bool(warm_payload.get("cache_hit"))
            if warm.status_code == _HTTP_OK
            else False
        )
    timing: dict[str, object] = {
        "header_ms": round(streamed.header_ms, 3),
        "first_protocol_ms": round(streamed.first_protocol_ms, 3)
        if streamed.first_protocol_ms is not None
        else -1.0,
        "first_content_ms": round(streamed.first_content_ms, 3)
        if streamed.first_content_ms is not None
        else -1.0,
        "cold_total_ms": round(streamed.total_ms, 3),
        "warm_total_ms": round(warm_ms, 3) if warm_ms is not None else None,
        "warm_cache_hit": warm_cache_hit,
    }
    return score, timing


def _run_metrics(
    case_scores: Sequence[Mapping[str, object]],
    timings: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    quality = _metrics(case_scores)
    cold = [float(cast(float | int, item["cold_total_ms"])) for item in timings]
    warm = [
        float(value)
        for item in timings
        for value in [item["warm_total_ms"]]
        if isinstance(value, (int, float))
    ]
    ttfc = [
        float(cast(float | int, item["first_content_ms"]))
        for item in timings
        if float(cast(float | int, item["first_content_ms"])) >= 0
    ]
    performance: dict[str, object] = {
        "sample_count": len(timings),
        "cold_p50_ms": _percentile(cold, 0.50),
        "cold_p95_ms": _percentile(cold, 0.95),
        "warm_p50_ms": _percentile(warm, 0.50),
        "warm_p95_ms": _percentile(warm, 0.95),
        "ttfc_p50_ms": _percentile(ttfc, 0.50),
        "ttfc_p95_ms": _percentile(ttfc, 0.95),
        "warm_cache_hit_rate": round(
            sum(item["warm_cache_hit"] is True for item in timings)
            / max(1, len(warm)),
            6,
        ),
        "warm_sample_count": len(warm),
    }
    return quality, performance


def run_evaluation(  # noqa: PLR0913, PLR0915, PLR0917
    target_root: Path,
    workspace: Path,
    seed_manifest_path: Path,
    qdrant_url: str,
    split: Literal["tuning", "holdout"],
    trace_mode: Literal["SAFE", "DIAGNOSTIC"],
    output: Path,
) -> dict[str, object]:
    """在独占 seed 副本上执行一个 split 的真实 Product API。

    Args:
        target_root: 待评实现的仓库根目录。
        workspace: 从固定 seed 克隆出的独占关闭态工作区。
        seed_manifest_path: 与工作区身份一致的 seed manifest 路径。
        qdrant_url: 仅允许回环地址的 Qdrant 端点。
        split: 本次只能执行 ``tuning`` 或 ``holdout`` 之一。
        trace_mode: Product API 的 ``SAFE`` 或 ``DIAGNOSTIC`` 捕获模式。
        output: 写入去敏评测报告的新文件路径。

    Returns:
        包含质量、性能、资源、缓存、并发与终态对账的报告。

    """
    validate_dataset()
    qdrant_url = _require_loopback_qdrant(qdrant_url)
    seed = _read_json(seed_manifest_path)
    if seed.get("dataset_sha256") != dataset_sha256():
        raise ValueError("seed 的数据集摘要与当前运行器不一致。")
    scopes = seed.get("scopes")
    if not isinstance(scopes, dict) or not isinstance(scopes.get(split), dict):
        raise ValueError(f"seed 缺少 split：{split}")
    scope = cast(dict[str, object], scopes[split])
    project_id = str(scope["project_id"])
    knowledge_base_id = str(scope["knowledge_base_id"])
    documents = scope.get("documents")
    identity = scope.get("identity")
    if not isinstance(documents, dict) or not isinstance(identity, dict):
        raise ValueError(f"seed split 不完整：{split}")
    bindings = _activate_target(target_root)
    opened = _open_runtime(workspace, qdrant_url, bindings)
    resource_before = _resource_snapshot()
    started_cpu = time.process_time()
    try:
        actual_identity = _scope_identity(
            opened.runtime,
            knowledge_base_id,
            cast(dict[str, dict[str, object]], documents),
        )
        if actual_identity != identity:
            raise AssertionError(
                "目标 Runtime 未保持固定 Parser/Chunk/Revision 身份。"
            )
        document_key_by_id = {
            str(item["document_id"]): key
            for key, item in documents.items()
            if isinstance(item, dict)
        }
        chunks = identity.get("chunks")
        if not isinstance(chunks, list):
            raise ValueError("seed identity 缺少 chunks。")
        chunk_by_id = {
            str(item["chunk_id"]): cast(dict[str, object], item)
            for item in chunks
            if isinstance(item, dict)
        }
        endpoint = (
            f"/api/v1/projects/{project_id}/knowledge-bases/"
            f"{knowledge_base_id}:answer"
        )
        headers = {"X-CSRF-Token": opened.csrf}
        split_cases = [case for case in CASES if case.split == split]
        probe = asyncio.run(
            _singleflight_wave(
                opened.app,
                project_id,
                knowledge_base_id,
                split_cases[0].query.rstrip("？?") + "，请仅依据原文回答。",
            )
        )
        case_scores: list[dict[str, object]] = []
        timings: list[dict[str, object]] = []
        warmed_slices: set[str] = set()
        for case in split_cases:
            measure_warm = case.slice_name not in warmed_slices
            score, timing = _run_case(
                opened,
                endpoint,
                headers,
                case,
                trace_mode,
                document_key_by_id,
                chunk_by_id,
                measure_warm=measure_warm,
            )
            case_scores.append(score)
            timings.append(timing)
            if measure_warm:
                warmed_slices.add(case.slice_name)
        quality, performance = _run_metrics(case_scores, timings)
        performance["singleflight"] = probe
        performance["process_cpu_seconds"] = round(
            time.process_time() - started_cpu,
            6,
        )
        performance["resource_before"] = resource_before
        performance["resource_after"] = _resource_snapshot()
    finally:
        _close_runtime(opened)
    report: dict[str, object] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "kind": "v3_07_public_product_evaluation",
        "dataset_sha256": dataset_sha256(),
        "seed_manifest_sha256": _canonical_sha256(seed),
        "target": _target_identity(target_root),
        "hardware": _hardware_identity(),
        "lane": "local_offline",
        "split": split,
        "trace_mode": trace_mode,
        "corpus_identity": identity,
        "quality": quality,
        "gates": _gate_results(quality),
        "performance": performance,
        "cases": case_scores,
        "timings": timings,
    }
    _write_json(output, report)
    return report


def _median_numeric(
    reports: Sequence[Mapping[str, object]],
    section: str,
    key: str,
) -> float | None:
    """读取多个报告中的数值并返回中位数。"""
    values: list[float] = []
    for report in reports:
        raw_section = report.get(section)
        if not isinstance(raw_section, dict):
            return None
        value = raw_section.get(key)
        if not isinstance(value, (int, float)):
            return None
        values.append(float(value))
    return round(statistics.median(values), 6)


def compare_reports(
    baseline_paths: Sequence[Path],
    candidate_paths: Sequence[Path],
    output: Path,
) -> dict[str, object]:
    """验证至少三次独立 B/C 运行，并生成不含正文的差异。

    Args:
        baseline_paths: 同一固定身份下的独立基线报告路径。
        candidate_paths: 与基线逐次对应的候选报告路径。
        output: 写入去敏比较摘要的新文件路径。

    Returns:
        包含身份校验、质量差异、性能中位数与门禁结论的摘要。

    """
    if len(baseline_paths) != len(candidate_paths):
        raise ValueError("B/C 独立运行次数必须相同。")
    if len(baseline_paths) < _MIN_PERFORMANCE_REPLICATES:
        raise ValueError("B/C 性能比较至少需要三次独立工作区运行。")
    baseline_reports = [_read_json(path) for path in baseline_paths]
    candidate_reports = [_read_json(path) for path in candidate_paths]
    baseline = baseline_reports[0]
    candidate = candidate_reports[0]
    identity_keys = (
        "dataset_sha256",
        "seed_manifest_sha256",
        "split",
        "trace_mode",
        "corpus_identity",
    )
    all_reports = (*baseline_reports, *candidate_reports)
    mismatches = sorted(
        {
            key
            for report in all_reports[1:]
            for key in identity_keys
            if baseline.get(key) != report.get(key)
        }
    )
    if mismatches:
        raise ValueError(f"B/C 固定身份不一致：{mismatches}")
    hardware_fingerprints = {
        cast(dict[str, object], report.get("hardware", {})).get("fingerprint")
        for report in all_reports
    }
    if len(hardware_fingerprints) != 1 or None in hardware_fingerprints:
        raise ValueError("B/C 运行硬件身份不一致或缺失。")
    for label, reports in (
        ("baseline", baseline_reports),
        ("candidate", candidate_reports),
    ):
        targets = {
            json.dumps(report.get("target"), sort_keys=True)
            for report in reports
        }
        if len(targets) != 1:
            raise ValueError(f"{label} 的目标源码身份在重复运行间发生变化。")
    quality_keys = (
        "answerable_accuracy",
        "false_refusal_rate",
        "false_answer_rate",
        "citation_source_precision",
        "citation_span_validity",
        "paraphrase_consistency",
        "hard_constraint_violations",
        "status_parity",
        "retrieval_recall_at_10",
        "target_document_recall",
        "support_set_recall",
        "support_set_precision",
        "provider_call_count",
    )
    quality_delta: dict[str, object] = {}
    for key in quality_keys:
        before = _median_numeric(baseline_reports, "quality", key)
        after = _median_numeric(candidate_reports, "quality", key)
        quality_delta[key] = {
            "baseline": before,
            "candidate": after,
            "delta": (
                round(float(after) - float(before), 6)
                if before is not None and after is not None
                else None
            ),
        }
    baseline_p95 = _median_numeric(
        baseline_reports,
        "performance",
        "cold_p95_ms",
    )
    candidate_p95 = _median_numeric(
        candidate_reports,
        "performance",
        "cold_p95_ms",
    )
    p95_ratio = (
        candidate_p95 / baseline_p95
        if baseline_p95 is not None
        and candidate_p95 is not None
        and baseline_p95 > 0
        else None
    )
    candidate_gate_reports = [
        cast(dict[str, object], report["gates"]) for report in candidate_reports
    ]
    candidate_gates_passed = all(
        gate.get("passed") is True for gate in candidate_gate_reports
    )
    performance_keys = (
        "cold_p50_ms",
        "ttfc_p50_ms",
        "ttfc_p95_ms",
        "warm_p50_ms",
        "warm_p95_ms",
        "warm_cache_hit_rate",
        "process_cpu_seconds",
    )
    aggregate_performance = {
        f"baseline_{key}": _median_numeric(
            baseline_reports,
            "performance",
            key,
        )
        for key in performance_keys
    }
    aggregate_performance.update(
        {
            f"candidate_{key}": _median_numeric(
                candidate_reports,
                "performance",
                key,
            )
            for key in performance_keys
        }
    )
    comparison = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "kind": "v3_07_public_bc_comparison",
        "dataset_sha256": baseline["dataset_sha256"],
        "split": baseline["split"],
        "trace_mode": baseline["trace_mode"],
        "fixed_identity_passed": True,
        "baseline_target": baseline["target"],
        "candidate_target": candidate["target"],
        "quality_delta": quality_delta,
        "performance": {
            "method": "median_of_independent_workspace_runs",
            "replicate_count": len(baseline_reports),
            **aggregate_performance,
            "baseline_cold_p95_ms": baseline_p95,
            "candidate_cold_p95_ms": candidate_p95,
            "baseline_cold_p95_ms_runs": [
                cast(dict[str, object], report["performance"])["cold_p95_ms"]
                for report in baseline_reports
            ],
            "candidate_cold_p95_ms_runs": [
                cast(dict[str, object], report["performance"])["cold_p95_ms"]
                for report in candidate_reports
            ],
            "candidate_to_baseline_p95_ratio": (
                round(p95_ratio, 6) if p95_ratio is not None else None
            ),
            "p95_within_20_percent": (
                p95_ratio <= _P95_REGRESSION_LIMIT
                if p95_ratio is not None
                else False
            ),
            "baseline_singleflight_runs": [
                cast(dict[str, object], report["performance"]).get(
                    "singleflight"
                )
                for report in baseline_reports
            ],
            "candidate_singleflight_runs": [
                cast(dict[str, object], report["performance"]).get(
                    "singleflight"
                )
                for report in candidate_reports
            ],
            "baseline_resource_runs": [
                cast(dict[str, object], report["performance"]).get(
                    "resource_after"
                )
                for report in baseline_reports
            ],
            "candidate_resource_runs": [
                cast(dict[str, object], report["performance"]).get(
                    "resource_after"
                )
                for report in candidate_reports
            ],
        },
        "candidate_gate_runs": candidate_gate_reports,
        "report_sha256": {
            "baseline": [
                _canonical_sha256(report) for report in baseline_reports
            ],
            "candidate": [
                _canonical_sha256(report) for report in candidate_reports
            ],
        },
        "passed": candidate_gates_passed
        and p95_ratio is not None
        and p95_ratio <= _P95_REGRESSION_LIMIT,
    }
    _write_json(output, comparison)
    return comparison


def _print_summary(payload: Mapping[str, object]) -> None:
    summary = {
        key: payload[key]
        for key in (
            "kind",
            "dataset_sha256",
            "split",
            "trace_mode",
            "quality",
            "gates",
            "performance",
            "passed",
        )
        if key in payload
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="V3-07 公开查询质量 Product API 评测。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="校验公开数据集与门槛。")

    prepare = subparsers.add_parser(
        "prepare",
        help="通过 Product API 建立 seed。",
    )
    prepare.add_argument("--target-root", type=Path, required=True)
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--qdrant-url", required=True)
    prepare.add_argument("--output", type=Path, required=True)

    clone = subparsers.add_parser("clone", help="复制已关闭 seed。")
    clone.add_argument("--source", type=Path, required=True)
    clone.add_argument("--destination", type=Path, required=True)

    run = subparsers.add_parser("run", help="运行单个 split 与 Trace 模式。")
    run.add_argument("--target-root", type=Path, required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--seed-manifest", type=Path, required=True)
    run.add_argument("--qdrant-url", required=True)
    run.add_argument("--split", choices=("tuning", "holdout"), required=True)
    run.add_argument(
        "--trace-mode",
        choices=("SAFE", "DIAGNOSTIC"),
        required=True,
    )
    run.add_argument("--output", type=Path, required=True)

    compare = subparsers.add_parser("compare", help="比较同身份 B/C 报告。")
    compare.add_argument(
        "--baseline",
        type=Path,
        nargs="+",
        required=True,
        help="至少三个独立 baseline 报告。",
    )
    compare.add_argument(
        "--candidate",
        type=Path,
        nargs="+",
        required=True,
        help="与 baseline 次数相同的独立 candidate 报告。",
    )
    compare.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行 V3-07 公开评测子命令。

    Args:
        argv: 可选命令行参数；默认使用当前进程参数。

    Returns:
        成功为 0；候选门槛失败为 2。

    """
    arguments = _parser().parse_args(argv)
    if arguments.command == "validate":
        _print_summary(
            {
                "kind": "v3_07_public_dataset_validation",
                **validate_dataset(),
            }
        )
        return 0
    if arguments.command == "prepare":
        payload = prepare_workspace(
            arguments.target_root,
            arguments.workspace,
            arguments.qdrant_url,
            arguments.output,
        )
        _print_summary(payload)
        return 0
    if arguments.command == "clone":
        clone_workspace(arguments.source, arguments.destination)
        print(f"cloned={arguments.destination.resolve()}")
        return 0
    if arguments.command == "run":
        payload = run_evaluation(
            arguments.target_root,
            arguments.workspace,
            arguments.seed_manifest,
            arguments.qdrant_url,
            arguments.split,
            arguments.trace_mode,
            arguments.output,
        )
        _print_summary(payload)
        return 0 if cast(dict[str, object], payload["gates"])["passed"] else 2
    if arguments.command == "compare":
        payload = compare_reports(
            arguments.baseline,
            arguments.candidate,
            arguments.output,
        )
        _print_summary(payload)
        return 0 if payload["passed"] else 2
    raise AssertionError(f"未处理的命令：{arguments.command}")


if __name__ == "__main__":
    raise SystemExit(main())
