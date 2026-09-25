"""从旧测试栈的只读 blob 目录恢复精确版本到候选归档。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
from typing import Any


def _safe_relative(value: str, version_id: str) -> pathlib.PurePosixPath:
    source = pathlib.PurePosixPath(value)
    if source.is_absolute() or any(
        part in {"", ".", ".."} for part in source.parts
    ):
        raise ValueError("旧来源路径不安全")
    if source.suffix.lower() != ".docx" or not re.fullmatch(
        r"[A-Za-z0-9_-]+", version_id
    ):
        raise ValueError("旧来源文件类型或版本 ID 无效")
    return source.with_name(f"{source.stem}__{version_id}{source.suffix}")


def _digest(path: pathlib.Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _write_json(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main() -> None:
    """校验旧 blob 哈希与大小，再复制到候选目录并写版本映射。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--missing-report", required=True, type=pathlib.Path)
    parser.add_argument("--blob-root", required=True, type=pathlib.Path)
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    args = parser.parse_args()
    report = json.loads(args.missing_report.read_text(encoding="utf-8"))
    missing = report["legacy_exact_source_missing"]
    if len(missing) != report["legacy_exact_source_missing_count"]:
        raise RuntimeError("缺失清单条数不一致")
    blob_root = args.blob_root.resolve(strict=True)
    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for item in missing:
        digest = item["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("旧源 SHA-256 无效")
        relative = _safe_relative(
            item["source_relative_path"], item["document_version_id"]
        )
        source = blob_root / digest[:2] / digest
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"旧原件缺失：{digest}")
        actual, size = _digest(source)
        if actual != digest or size != item["bytes"]:
            raise RuntimeError(f"旧原件与数据库不符：{digest}")
        destination = output_root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_symlink() or _digest(destination) != (
                digest,
                size,
            ):
                raise RuntimeError(f"候选归档已有不同内容：{relative}")
        else:
            temporary = destination.with_name(
                f"{destination.name}.{os.getpid()}.tmp"
            )
            with source.open("rb") as original, temporary.open("xb") as target:
                for chunk in iter(lambda: original.read(1024 * 1024), b""):
                    target.write(chunk)
            if _digest(temporary) != (digest, size):
                raise RuntimeError(f"候选归档复制校验失败：{relative}")
            temporary.replace(destination)
        rows.append(
            {
                "legacy_document_id": item["document_id"],
                "legacy_document_version_id": item["document_version_id"],
                "source_relative_path": item["source_relative_path"],
                "archive_relative_path": relative.as_posix(),
                "sha256": digest,
                "bytes": size,
                "status": "exact_bytes_archived",
            }
        )
    _write_json(args.manifest, rows)
    print(f"精确旧版本已归档 {len(rows)}/{len(missing)}")


if __name__ == "__main__":
    main()
