"""把现场授权文档按原始字节上传到 WeKnora 原生知识库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

_HTTP_CONFLICT = 409


class NativeDuplicateError(Exception):
    """原生知识库已保存相同文件内容。"""

    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item


def _request(
    url: str,
    *,
    token: str | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if content_type is not None:
        headers["Content-Type"] = content_type
    if not url.startswith(("http://", "https://")):
        raise ValueError("原生 API 仅允许 HTTP(S) 地址")
    request = urllib.request.Request(  # noqa: S310 - 已限定 HTTP(S)。
        url, data=data, headers=headers
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - 已限定 HTTP(S)。
            request, timeout=120
        ) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        if error.code == _HTTP_CONFLICT:
            duplicate = json.loads(body)
            item = duplicate.get("data")
            if duplicate.get("code") == "duplicate_file" and isinstance(
                item, dict
            ):
                raise NativeDuplicateError(item) from error
        raise RuntimeError(
            f"WeKnora HTTP {error.code}: {body[:1000]}"
        ) from error
    if not isinstance(result, dict) or result.get("success") is not True:
        raise RuntimeError(f"WeKnora 拒绝请求: {result!r}")
    return result


def _multipart(
    *, filename: str, content: bytes, metadata: dict[str, str]
) -> tuple[bytes, str]:
    boundary = "wst" + os.urandom(18).hex()
    parts: list[bytes] = []
    fields = {
        "fileName": filename,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }
    for name, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                ).encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{pathlib.Path(filename).name}"\r\n'
            ).encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _save_manifest(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    """逐件上传并记录内容哈希；重新运行时跳过已成功的同一字节。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--password-file", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument(
        "--migration-source", default="wanshitong-current-corpus-20260925"
    )
    args = parser.parse_args()
    root = args.corpus.resolve(strict=True)
    if not root.is_dir():
        parser.error("corpus 必须是目录")
    paths = sorted(path for path in root.rglob("*.docx") if path.is_file())
    if not paths:
        parser.error("corpus 中没有 DOCX")
    rows: list[dict[str, Any]] = (
        json.loads(args.manifest.read_text(encoding="utf-8"))
        if args.manifest.exists()
        else []
    )
    completed = {
        (row["source_relative_path"], row["sha256"])
        for row in rows
        if row.get("status") in {"uploaded", "duplicate_reused"}
    }
    base = args.api_base.rstrip("/")
    password = args.password_file.read_text(encoding="utf-8")
    login = _request(
        f"{base}/auth/login",
        data=json.dumps(
            {"email": args.admin_email, "password": password}
        ).encode(),
        content_type="application/json",
    )
    token = login.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("原生管理员登录未返回令牌")
    uploaded = 0
    for path in paths:
        if path.is_symlink():
            raise RuntimeError(f"拒绝符号链接: {path}")
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if (relative, digest) in completed:
            continue
        body, content_type = _multipart(
            filename=relative,
            content=content,
            metadata={
                "source_relative_path": relative,
                "source_sha256": digest,
                "migration_source": args.migration_source,
            },
        )
        try:
            result = _request(
                f"{base}/knowledge-bases/{args.kb_id}/knowledge/file",
                token=token,
                data=body,
                content_type=content_type,
            )
            item = result.get("data")
            status = "uploaded"
        except NativeDuplicateError as error:
            item = error.item
            metadata = item.get("metadata")
            if (
                not isinstance(metadata, dict)
                or metadata.get("source_sha256") != digest
            ):
                raise RuntimeError(
                    f"原生重复文件与当前源 SHA-256 不一致: {relative}"
                ) from error
            status = "duplicate_reused"
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise RuntimeError(f"上传成功但缺少原生知识 ID: {relative}")
        rows.append(
            {
                "source_relative_path": relative,
                "sha256": digest,
                "bytes": len(content),
                "native_knowledge_id": item["id"],
                "status": status,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - 60 主机仍使用 Python 3.10。
            }
        )
        _save_manifest(args.manifest, rows)
        uploaded += 1
        print(f"{status} {uploaded}: {relative}", flush=True)
    print(
        f"本轮新增 {uploaded}，清单累计 {len(rows)}，源目录 DOCX {len(paths)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
