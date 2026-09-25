#!/usr/bin/env python3
"""冻结同一批原始 DOCX，并分别导入隔离 A、B 实例。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

from import_docx import (  # type: ignore[import-not-found]
    ImportResult,
    PreparedDocument,
    WanshitongAdminClient,
    _read_bootstrap_token,
    _upload_one,
    load_and_validate_manifest,
)

SELECTED_IDS = (
    "DOCX-001",
    "DOCX-003",
    "DOCX-004",
    "DOCX-005",
    "DOCX-006",
    "DOCX-007",
    "DOCX-009",
    "DOCX-012",
    "DOCX-014",
    "DOCX-016",
    "DOCX-022",
    "DOCX-023",
    "DOCX-028",
    "DOCX-042",
    "DOCX-045",
)
B_ORIGIN = "http://127.0.0.1:18390"
A_ORIGIN = "http://127.0.0.1:18389"
B_EMAIL = "wkab20260925@example.test"


def _documents(args: argparse.Namespace) -> tuple[PreparedDocument, ...]:
    """复用 Q1 原始 DOCX 全量清单校验，再取预先冻结的 15 份。"""
    prepared = load_and_validate_manifest(args.manifest, args.corpus)
    by_id = {item.control_id: item for item in prepared}
    return tuple(by_id[control_id] for control_id in SELECTED_IDS)


def _write_new(path: Path, value: object) -> None:
    """排他写入私有 JSON，避免覆盖既有实验记录。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _lock(args: argparse.Namespace) -> dict[str, object]:
    """读取冻结清单，并在每次导入前检查原始字节。"""
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    documents = _documents(args)
    locked = lock["documents"]
    if [item.control_id for item in documents] != [
        item["control_id"] for item in locked
    ]:
        raise ValueError("文档集合或顺序与冻结清单不一致")
    for document, item in zip(documents, locked, strict=True):
        digest = hashlib.sha256(document.file_path.read_bytes()).hexdigest()
        if digest != item["sha256"] or document.sha256 != item["sha256"]:
            raise ValueError(f"原始字节漂移：{document.control_id}")
    return lock


def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: bytes | None = None,
    content_type: str = "application/json",
) -> dict[str, object]:
    """只访问隔离 B 的回环 API；报错不打印凭据或上传正文。"""
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("B API 路径必须是单斜线开头的相对路径")
    request = urllib.request.Request(  # noqa: S310 - 固定回环 Origin。
        B_ORIGIN + path,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - 固定回环 Origin。
            request, timeout=120
        ) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read(2048)
        raise RuntimeError(
            f"B API {method} {path} HTTP {error.code}: "
            f"{body.decode('utf-8', 'replace')[:500]}"
        ) from None
    if not isinstance(value, dict) or value.get("success") is not True:
        raise RuntimeError(f"B API {method} {path} 响应失败")
    return value


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def freeze(args: argparse.Namespace) -> None:
    """在任一回答生成前固定原件摘要与选题文档范围。"""
    selected = _documents(args)
    lock = {
        "schema": "wkab-corpus-v1",
        "source_manifest_sha256": hashlib.sha256(
            args.manifest.read_bytes()
        ).hexdigest(),
        "documents": [
            {
                "control_id": item.control_id,
                "source_relative_path": item.literal_path,
                "sha256": item.sha256,
                "size_bytes": item.file_path.stat().st_size,
            }
            for item in selected
        ],
    }
    _write_new(args.lock, lock)
    print(f"冻结原始 DOCX：{len(selected)} 份；SHA 已记录。")


def import_a(args: argparse.Namespace) -> None:
    """经 Q1 管理员 API 导入 A 专属索引并等待可检索。"""
    _lock(args)
    token = _read_bootstrap_token(args.a_token)
    client = WanshitongAdminClient(A_ORIGIN)
    client.login(token)
    existing = client.list_documents()
    if existing:
        raise RuntimeError("A 隔离实例不是空文档库")
    results: list[dict[str, object]] = []
    for document in _documents(args):
        result = ImportResult(
            control_id=document.control_id,
            source_relative_path=document.api_path,
            sha256=document.sha256,
        )
        _upload_one(
            client,
            document,
            result,
            wait=True,
            timeout_seconds=1800,
            poll_seconds=2.0,
        )
        results.append(asdict(result))
        print(
            f"A {document.control_id}: {result.final_job_state}, "
            f"retrievable={result.retrievable}",
            flush=True,
        )
        if result.final_job_state != "succeeded" or not result.retrievable:
            break
    _write_new(args.report, {"side": "A", "documents": results})
    if len(results) != len(SELECTED_IDS) or not all(
        item["retrievable"] for item in results
    ):
        raise RuntimeError("A 导入未全数可检索")


def init_b(args: argparse.Namespace) -> None:
    """为原版 B 注册独立实验账号并创建单个独立知识库。"""
    _lock(args)
    password = secrets.token_hex(16)
    _request(
        "POST",
        "/api/v1/auth/register",
        payload=_json(
            {
                "username": "wkab20260925",
                "email": B_EMAIL,
                "password": password,
            }
        ),
    )
    login = _request(
        "POST",
        "/api/v1/auth/login",
        payload=_json({"email": B_EMAIL, "password": password}),
    )
    token = login.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("B 登录响应缺少 Token")
    kb = _request(
        "POST",
        "/api/v1/knowledge-bases",
        token=token,
        payload=_json(
            {
                "name": "湾事通隔离AB原件15份",
                "type": "document",
                "embedding_model_id": "wkab-qwen3-embedding-0-6b",
            }
        ),
    )
    data = kb.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise RuntimeError("B 知识库创建响应缺少 ID")
    _write_new(
        args.b_identity,
        {
            "email": B_EMAIL,
            "password": password,
            "token": token,
            "kb_id": data["id"],
        },
    )
    print(f"B 实验账号及知识库已创建；KB ID={data['id']}。")


def config_b(args: argparse.Namespace) -> None:
    """用原版 quick-answer 配置绑定重排，仍走 KnowledgeQA 流程。"""
    _lock(args)
    identity = json.loads(args.b_identity.read_text(encoding="utf-8"))
    config = {
        "agent_mode": "quick-answer",
        "kb_selection_mode": "selected",
        "knowledge_bases": [identity["kb_id"]],
        "model_id": "wkab-qwen3-8b-awq",
        "rerank_model_id": "wkab-qwen3-reranker-0-6b",
        "embedding_top_k": 30,
        "keyword_threshold": 0.3,
        "vector_threshold": 0.2,
        "rerank_top_k": 6,
        "rerank_threshold": 0.3,
        "enable_rewrite": True,
        "enable_query_expansion": True,
        "fallback_strategy": "fixed",
        "fallback_response": "根据现有资料无法确定。",
        "temperature": 0,
        "max_completion_tokens": 2680,
        "thinking": False,
        "citation_enabled": True,
    }
    response = _request(
        "POST",
        "/api/v1/agents",
        token=identity["token"],
        payload=_json({"name": "隔离AB原版标准问答", "config": config}),
    )
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        raise RuntimeError("B quick-answer 配置创建响应缺少 ID")
    _write_new(args.b_agent, {"id": data["id"], "config": config})
    print(f"B 原版 quick-answer 配置已固定；ID={data['id']}。")


def _multipart(document: PreparedDocument) -> tuple[bytes, str]:
    """按原始字节构造标准上传表单，不改写 DOCX。"""
    boundary = secrets.token_hex(20)
    file_name = document.file_path.name
    if "\r" in file_name or "\n" in file_name or '"' in file_name:
        raise ValueError("文件名不适合 multipart 表单")
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="process_config"\r\n\r\n'
        '{"summary_enabled":false}\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; '
        f'filename="{file_name}"\r\n'
        f"Content-Type: {document.media_type}\r\n\r\n"
    ).encode()
    body = prefix + document.file_path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


def import_b(args: argparse.Namespace) -> None:
    """走原版知识库上传 API，记录异步解析回执。"""
    _lock(args)
    identity = json.loads(args.b_identity.read_text(encoding="utf-8"))
    kb_id = identity["kb_id"]
    token = identity["token"]
    results: list[dict[str, object]] = []
    for document in _documents(args):
        body, content_type = _multipart(document)
        response = _request(
            "POST",
            f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
            token=token,
            payload=body,
            content_type=content_type,
        )
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise RuntimeError(f"B 上传回执缺少 ID：{document.control_id}")
        results.append(
            {
                "control_id": document.control_id,
                "sha256": document.sha256,
                "knowledge_id": data["id"],
                "parse_status": data.get("parse_status"),
            }
        )
        print(
            f"B {document.control_id}: id={data['id']}, "
            f"status={data.get('parse_status')}",
            flush=True,
        )
    _write_new(args.report, {"side": "B", "documents": results})


def status_b(args: argparse.Namespace) -> None:
    """按冻结 ID 汇总 B 原版异步解析状态。"""
    identity = json.loads(args.b_identity.read_text(encoding="utf-8"))
    response = _request(
        "GET",
        f"/api/v1/knowledge-bases/{identity['kb_id']}/knowledge?page=1&page_size=100",
        token=identity["token"],
    )
    rows = response.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("B 文档列表响应无效")
    states: dict[str, int] = {}
    for row in rows:
        status = row.get("parse_status", "missing")
        states[status] = states.get(status, 0) + 1
    print(
        json.dumps(
            {
                "count": len(rows),
                "states": states,
                "fields": sorted(rows[0]) if rows else [],
                "chunks": [row.get("chunk_count") for row in rows],
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    """分步执行冻结、导入和状态检查。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "freeze",
            "import-a",
            "init-b",
            "config-b",
            "import-b",
            "status-b",
        ),
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--a-token", type=Path)
    parser.add_argument("--b-identity", type=Path)
    parser.add_argument("--b-agent", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    commands = {
        "freeze": freeze,
        "import-a": import_a,
        "init-b": init_b,
        "config-b": config_b,
        "import-b": import_b,
        "status-b": status_b,
    }
    started = time.monotonic()
    commands[args.command](args)
    elapsed = time.monotonic() - started
    print(f"step={args.command} elapsed_seconds={elapsed:.1f}")


if __name__ == "__main__":
    main()
