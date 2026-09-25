#!/usr/bin/env python3
"""从上次锁定的 15 份原始 DOCX 恢复可试用的原版 WeKnora 知识库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ORIGIN = "http://127.0.0.1:18390"
EMAIL = "wklive20260925@example.test"
USERNAME = "wklive20260925"
DOCX_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
EXPECTED_DOCUMENTS = 15
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _save_private(path: Path, data: dict[str, Any]) -> None:
    """原子保存凭据及进度，文件只允许当前用户读取。"""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        temporary.chmod(0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_private(path: Path) -> dict[str, Any]:
    """读取本轮身份文件，拒绝链接和宽松权限。"""
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError(f"私有文件缺失或权限不安全：{path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"私有文件结构错误：{path.name}")
    return value


def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: bytes | None = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    """只向隔离实例回环 API 发请求，不打印 Token 或文档正文。"""
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("API 路径必须是单斜线开头的相对路径")
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - 固定回环地址。
        ORIGIN + path, data=payload, headers=headers, method=method
    )
    try:
        with _OPENER.open(request, timeout=120) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"B API {method} {path} HTTP {error.code}") from None
    if not isinstance(value, dict) or value.get("success") is not True:
        raise RuntimeError(f"B API {method} {path} 响应失败")
    return value


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _documents(root: Path) -> list[tuple[str, Path, str]]:
    """核验冻结清单和原件字节，返回固定顺序的 15 份 DOCX。"""
    lock = json.loads((root / "corpus.lock.json").read_text(encoding="utf-8"))
    records = lock["documents"]
    if not isinstance(records, list) or len(records) != EXPECTED_DOCUMENTS:
        raise RuntimeError("冻结清单不是预期的 15 份原件")
    documents = []
    for record in records:
        control_id = record["control_id"]
        if not isinstance(control_id, str) or not control_id.startswith(
            "DOCX-"
        ):
            raise RuntimeError("原件控制 ID 无效")
        source = root / "corpus-evidence" / f"{control_id}.docx"
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"原件缺失：{control_id}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if (
            digest != record["sha256"]
            or source.stat().st_size != record["size_bytes"]
        ):
            raise RuntimeError(f"原件字节不匹配：{control_id}")
        documents.append((control_id, source, digest))
    return documents


def _login(identity: dict[str, Any]) -> str:
    response = _request(
        "POST",
        "/api/v1/auth/login",
        payload=_json(
            {"email": identity["email"], "password": identity["password"]}
        ),
    )
    token = response.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("B 登录响应缺少 Token")
    return token


def init(root: Path) -> None:
    """创建独立试用账号、单知识库和原生 quick-answer 配置。"""
    _documents(root)
    identity_path = root / "identity.private.json"
    if identity_path.exists():
        identity = _read_private(identity_path)
    else:
        identity = {
            "email": EMAIL,
            "username": USERNAME,
            "password": secrets.token_urlsafe(21),
        }
        _save_private(identity_path, identity)
    if not identity.get("registered"):
        _request(
            "POST",
            "/api/v1/auth/register",
            payload=_json(
                {
                    "email": identity["email"],
                    "username": identity["username"],
                    "password": identity["password"],
                }
            ),
        )
        identity["registered"] = True
        _save_private(identity_path, identity)
    identity["token"] = _login(identity)
    _save_private(identity_path, identity)
    if not identity.get("kb_id"):
        response = _request(
            "POST",
            "/api/v1/knowledge-bases",
            token=identity["token"],
            payload=_json(
                {
                    "name": "湾事通原版试用（15份原件）",
                    "type": "document",
                    "embedding_model_id": "wkab-qwen3-embedding-0-6b",
                    "summary_model_id": "wkab-qwen3-8b-awq",
                }
            ),
        )
        identity["kb_id"] = response["data"]["id"]
        _save_private(identity_path, identity)
    if not identity.get("agent_id"):
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
            payload=_json({"name": "湾事通原版问答试用", "config": config}),
        )
        identity["agent_id"] = response["data"]["id"]
        _save_private(identity_path, identity)
    _save_private(root / "agent.private.json", {"id": identity["agent_id"]})
    print("已创建独立账号、知识库及原生问答配置；凭据未输出。")


def _multipart(source: Path) -> tuple[bytes, str]:
    """原样上传 DOCX 字节，不额外生成摘要。"""
    boundary = secrets.token_hex(20)
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="process_config"\r\n\r\n'
        '{"summary_enabled":false}\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; '
        f'filename="{source.name}"\r\n'
        f"Content-Type: {DOCX_TYPE}\r\n\r\n"
    ).encode()
    return (
        prefix + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode(),
        f"multipart/form-data; boundary={boundary}",
    )


def upload(root: Path) -> None:
    """把冻结原件导入独立知识库，并逐项记录上传回执。"""
    identity_path = root / "identity.private.json"
    identity = _read_private(identity_path)
    identity["token"] = _login(identity)
    _save_private(identity_path, identity)
    report_path = root / "import.private.json"
    report = (
        _read_private(report_path)
        if report_path.exists()
        else {"documents": []}
    )
    completed = {row["control_id"] for row in report["documents"]}
    if not completed:
        existing = _request(
            "GET",
            f"/api/v1/knowledge-bases/{identity['kb_id']}/knowledge?page=1&page_size=100",
            token=identity["token"],
        )["data"]
        if existing:
            raise RuntimeError("知识库已有文档而本轮没有导入回执，拒绝重复导入")
    for control_id, source, digest in _documents(root):
        if control_id in completed:
            continue
        body, content_type = _multipart(source)
        response = _request(
            "POST",
            f"/api/v1/knowledge-bases/{identity['kb_id']}/knowledge/file",
            token=identity["token"],
            payload=body,
            content_type=content_type,
        )
        data = response["data"]
        report["documents"].append(
            {
                "control_id": control_id,
                "sha256": digest,
                "knowledge_id": data["id"],
                "parse_status": data.get("parse_status"),
            }
        )
        _save_private(report_path, report)
        print(f"{control_id}: 已上传", flush=True)
    if len(report["documents"]) != EXPECTED_DOCUMENTS:
        raise RuntimeError("知识库未收到全部 15 份原件")


def wait(root: Path) -> None:
    """等待全部原件完成原生解析，保留失败状态。"""
    identity_path = root / "identity.private.json"
    identity = _read_private(identity_path)
    identity["token"] = _login(identity)
    _save_private(identity_path, identity)
    deadline = time.monotonic() + 1800
    last: dict[str, int] = {}
    while time.monotonic() < deadline:
        rows = _request(
            "GET",
            f"/api/v1/knowledge-bases/{identity['kb_id']}/knowledge?page=1&page_size=100",
            token=identity["token"],
        )["data"]
        last = {}
        for row in rows:
            state = row.get("parse_status", "missing")
            last[state] = last.get(state, 0) + 1
        if (
            len(rows) == EXPECTED_DOCUMENTS
            and last.get("completed") == EXPECTED_DOCUMENTS
        ):
            print(
                json.dumps(
                    {
                        "documents": 15,
                        "states": last,
                        "chunks": (
                            sum(row["chunk_count"] for row in rows)
                            if all("chunk_count" in row for row in rows)
                            else None
                        ),
                    }
                )
            )
            return
        if any(state in last for state in ("failed", "error")):
            raise RuntimeError(f"B 原生解析失败：{last}")
        time.sleep(5)
    raise TimeoutError(f"B 原生解析超时：{last}")


def main() -> None:
    """分步执行，允许在同一私有运行目录中继续未完成阶段。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "upload", "wait"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if root != Path("/data/tyf/wanshitong-weknora-live-20260925"):
        raise ValueError("拒绝访问其他运行目录")
    {"init": init, "upload": upload, "wait": wait}[args.command](root)


if __name__ == "__main__":
    main()
