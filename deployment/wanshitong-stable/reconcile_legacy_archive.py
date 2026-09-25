"""把旧精确版本归档与原生知识 ID、解析终态合为可追溯清单。"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

from reconcile_corpus import _native_rows, _write_json


def _read_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) for row in rows
    ):
        raise RuntimeError(f"清单格式无效：{path}")
    return rows


def main() -> None:
    """逐件校验源哈希、原生身份和解析完成状态。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--recovered-manifest", required=True, type=pathlib.Path
    )
    parser.add_argument("--import-manifest", required=True, type=pathlib.Path)
    parser.add_argument("--output-manifest", required=True, type=pathlib.Path)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--password-file", required=True, type=pathlib.Path)
    args = parser.parse_args()
    recovered = _read_rows(args.recovered_manifest)
    imported = _read_rows(args.import_manifest)
    by_path = {row["source_relative_path"]: row for row in imported}
    if len(by_path) != len(imported) or len(imported) != len(recovered):
        raise RuntimeError("归档与原生导入的件数或路径不一致")
    native = _native_rows(
        args.api_base.rstrip("/"),
        args.kb_id,
        args.admin_email,
        args.password_file.read_text(encoding="utf-8"),
    )
    by_id = {row["id"]: row for row in native}
    if len(by_id) != len(native):
        raise RuntimeError("原生知识 ID 重复")
    completed = 0
    for row in recovered:
        item = by_path.get(row["archive_relative_path"])
        if item is None or item["sha256"] != row["sha256"]:
            raise RuntimeError("旧精确原件与导入件不一致")
        knowledge_id = item["native_knowledge_id"]
        document = by_id.get(knowledge_id)
        if document is None:
            raise RuntimeError("原生知识库缺少已导入件")
        row["native_knowledge_base_id"] = args.kb_id
        row["native_knowledge_id"] = knowledge_id
        row["native_parse_status"] = document.get("parse_status")
        row["status"] = (
            "indexed_in_private_archive"
            if row["native_parse_status"] == "completed"
            else "uploaded_unindexed"
        )
        completed += row["status"] == "indexed_in_private_archive"
    if len(native) != len(recovered):
        raise RuntimeError("归档知识库含有清单外资料")
    _write_json(args.output_manifest, recovered)
    print(f"旧精确版本归档完成 {completed}/{len(recovered)}")


if __name__ == "__main__":
    main()
