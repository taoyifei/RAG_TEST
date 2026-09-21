"""离线构建与活动 Index Revision 精确绑定的湾事通部门 Profile。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.wanshitong.department_profiles import (
    DepartmentProfileSourceReader,
    DepartmentProfileStore,
    build_department_profiles,
)


def main(argv: Sequence[str] | None = None) -> int:
    """从候选数据副本构建 lexical-only Profile 文件。

    Args:
        argv: 可选命令行参数；默认读取进程参数。

    Returns:
        成功为 0；参数或数据错误由 argparse/调用栈返回非零。

    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve(strict=True)
    if data_dir.is_symlink():
        raise ValueError("部门 Profile 数据目录禁止 symlink。")
    database = data_dir / "universal-rag.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise ValueError("部门 Profile 数据库不存在或不是普通文件。")
    output_root = (
        args.output_root.resolve(strict=False)
        if args.output_root is not None
        else data_dir / "department-profiles"
    )
    snapshot = DepartmentProfileSourceReader(
        SqliteConnectionFactory(database)
    ).snapshot(args.project_id, args.knowledge_base_id)
    profiles = build_department_profiles(snapshot)
    output = DepartmentProfileStore(output_root).save(profiles)
    print(
        json.dumps(
            {
                "status": "BUILT",
                "scope_id": profiles.scope_id,
                "index_revision_id": profiles.index_revision_id,
                "metadata_revision": profiles.metadata_revision,
                "profile_revision": profiles.profile_revision,
                "department_count": len(profiles.profiles),
                "document_count": sum(
                    profile.document_count for profile in profiles.profiles
                ),
                "embedding_mode": "LEXICAL_ONLY",
                "output": str(output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
