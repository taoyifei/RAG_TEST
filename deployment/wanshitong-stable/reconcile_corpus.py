"""对照旧库只读快照、现有原件和 WeKnora 原生索引状态。"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import urllib.request
from collections import Counter, defaultdict
from typing import Any


def _get_json(
    url: str, *, token: str | None = None, body: object = None
) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise ValueError("原生 API 仅允许 HTTP(S) 地址")
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 - 已限定 HTTP(S)。
        url, data=data, headers=headers
    )
    with urllib.request.urlopen(  # noqa: S310 - 已限定 HTTP(S)。
        request, timeout=30
    ) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("success") is not True:
        raise RuntimeError("原生 API 未返回成功状态")
    return result


def _legacy_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            "SELECT d.document_id,d.current_version_id,"
            "v.content_sha256,v.size_bytes,d.display_name,"
            "m.source_relative_path FROM documents AS d "
            "JOIN document_versions AS v "
            "ON v.document_version_id=d.current_version_id "
            "LEFT JOIN wanshitong_document_metadata AS m "
            "ON m.document_id=d.document_id "
            "WHERE d.status='active' AND d.lifecycle_status='active'"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        database.close()


def _native_rows(
    api_base: str, kb_id: str, email: str, password: str
) -> list[dict[str, Any]]:
    login = _get_json(
        f"{api_base}/auth/login",
        body={"email": email, "password": password},
    )
    token = login.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("原生管理员登录缺少令牌")
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        response = _get_json(
            f"{api_base}/knowledge-bases/{kb_id}/knowledge"
            f"?page={page}&page_size=100",
            token=token,
        )
        batch = response.get("data")
        if not isinstance(batch, list) or any(
            not isinstance(item, dict) for item in batch
        ):
            raise RuntimeError("原生文档列表格式无效")
        items.extend(batch)
        total = response.get("total")
        if not isinstance(total, int) or len(items) >= total:
            break
        if not batch:
            raise RuntimeError("原生文档分页提前结束")
        page += 1
    return items


def _write_json(path: pathlib.Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    """为每份候选原件写入原生状态和旧版精确版本关联。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-snapshot", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--report", required=True, type=pathlib.Path)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--password-file", required=True, type=pathlib.Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise RuntimeError("导入清单格式无效")
    legacy = _legacy_rows(args.legacy_snapshot)
    native = _native_rows(
        args.api_base.rstrip("/"),
        args.kb_id,
        args.admin_email,
        args.password_file.read_text(encoding="utf-8"),
    )
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in legacy:
        by_hash[str(row["content_sha256"])].append(row)
        if row["source_relative_path"]:
            by_path[str(row["source_relative_path"])].append(row)
    native_by_id = {item["id"]: item for item in native}
    for row in manifest:
        digest = row["sha256"]
        document = native_by_id.get(row["native_knowledge_id"])
        row["legacy_exact_matches"] = [
            {
                "document_id": old["document_id"],
                "document_version_id": old["current_version_id"],
            }
            for old in by_hash[digest]
        ]
        row["legacy_path_matches"] = [
            {
                "document_id": old["document_id"],
                "document_version_id": old["current_version_id"],
                "same_hash": old["content_sha256"] == digest,
            }
            for old in by_path[row["source_relative_path"]]
        ]
        row["native_parse_status"] = (
            document.get("parse_status") if document is not None else "missing"
        )
        row["native_folder_path"] = (
            document.get("folder_path") if document is not None else None
        )
        row["searchable"] = row["native_parse_status"] == "completed"
    current_hashes = {row["sha256"] for row in manifest}
    missing = [
        {
            "document_id": row["document_id"],
            "document_version_id": row["current_version_id"],
            "source_relative_path": row["source_relative_path"],
            "sha256": row["content_sha256"],
            "bytes": row["size_bytes"],
            "status": "legacy_exact_source_missing",
        }
        for row in legacy
        if row["content_sha256"] not in current_hashes
    ]
    report = {
        "legacy_active_versions": len(legacy),
        "current_corpus_entries": len(manifest),
        "current_unique_hashes": len(current_hashes),
        "native_unique_knowledge": len(native),
        "native_parse_statuses": dict(
            Counter(item.get("parse_status") for item in native)
        ),
        "legacy_exact_source_missing": missing,
        "legacy_exact_source_missing_count": len(missing),
        "unsearchable_current_entries": [
            row["source_relative_path"]
            for row in manifest
            if not row["searchable"]
        ],
    }
    _write_json(args.manifest, manifest)
    _write_json(args.report, report)
    print(
        "legacy",
        len(legacy),
        "current",
        len(manifest),
        "native",
        len(native),
        "missing_exact",
        len(missing),
        "native_statuses",
        report["native_parse_statuses"],
    )


if __name__ == "__main__":
    main()
