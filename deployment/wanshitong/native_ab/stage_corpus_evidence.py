#!/usr/bin/env python3
"""按冻结摘要复制 A/B 使用的原始 DOCX 到受限证据目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


def _sha256(path: Path) -> str:
    """计算文件原始字节摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    """严格核对原文再复制，目标文件名仅使用控制 ID。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    source_root = args.source_root.resolve(strict=True)
    args.outdir.mkdir(mode=0o700, exist_ok=False)
    manifest = []
    for document in lock["documents"]:
        control_id = document["control_id"]
        if not control_id.startswith("DOCX-") or not control_id[5:].isdigit():
            raise ValueError("控制 ID 非法")
        source = (source_root / document["source_relative_path"]).resolve(
            strict=True
        )
        if not source.is_relative_to(source_root):
            raise ValueError("原文路径越界")
        if source.stat().st_size != document["size_bytes"]:
            raise ValueError(f"原文字节数变化：{control_id}")
        if _sha256(source) != document["sha256"]:
            raise ValueError(f"原文摘要变化：{control_id}")
        target = args.outdir / f"{control_id}.docx"
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with (
            os.fdopen(descriptor, "wb") as output,
            source.open("rb") as input_stream,
        ):
            shutil.copyfileobj(input_stream, output)
        if _sha256(target) != document["sha256"]:
            raise ValueError(f"证据副本摘要错误：{control_id}")
        manifest.append(
            {"control_id": control_id, "sha256": document["sha256"]}
        )
    (args.outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"documents": len(manifest)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
