#!/usr/bin/env python3
"""以版本化快照和单一原子指针维护 Industry last-good。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT = re.compile(r"[0-9a-f]{12}-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
_POINTER_FIELDS = {
    "created_at",
    "manifest_sha256",
    "revision",
    "snapshot_id",
}


class LastGoodError(RuntimeError):
    """表示 last-good 快照或原子指针不可信。"""


FailurePoint = Literal[
    "after_env",
    "after_state",
    "after_manifest",
    "before_pointer",
]


def promote_last_good(
    backup_path: Path,
    env_path: Path,
    state_path: Path,
    revision: str,
    *,
    failure_point: FailurePoint | None = None,
) -> dict[str, object]:
    """完整发布新快照后以一次原子 rename 更新权威指针。

    Args:
        backup_path: Industry 私有备份目录。
        env_path: 当前已验证 private env。
        state_path: 当前已验证状态 JSON。
        revision: 与 env、状态和镜像一致的完整 Git SHA。
        failure_point: 仅供故障注入测试使用的中断点。

    Returns:
        权威指针的 canonical JSON object。

    Raises:
        LastGoodError: 输入、快照或指针合同不成立。

    """
    if _REVISION.fullmatch(revision) is None:
        raise LastGoodError("LAST_GOOD_REVISION_INVALID")
    _require_private_file(env_path, "env")
    _require_private_file(state_path, "state")
    state = _load_object(state_path, "state")
    state_revision = _state_revision(state)
    if state_revision != revision:
        raise LastGoodError("LAST_GOOD_STATE_REVISION_MISMATCH")
    backup_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshots = backup_path / "last-good-snapshots"
    snapshots.mkdir(mode=0o700, exist_ok=True)
    created_at = datetime.now(timezone.utc)  # noqa: UP017
    snapshot_id = (
        f"{revision[:12]}-{created_at.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    final = snapshots / snapshot_id
    temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=snapshots))
    temporary.chmod(0o700)
    try:
        target_env = temporary / "rag-industry.env"
        _copy_private_file(env_path, target_env)
        _inject_failure(failure_point, "after_env")
        target_state = temporary / "state.json"
        _copy_private_file(state_path, target_state)
        _inject_failure(failure_point, "after_state")
        manifest = {
            "created_at": created_at.isoformat(),
            "files": {
                "rag-industry.env": _identity(target_env),
                "state.json": _identity(target_state),
            },
            "revision": revision,
            "schema_version": "1",
        }
        manifest_path = temporary / "SNAPSHOT_MANIFEST.json"
        _write_private_json(manifest_path, manifest)
        _inject_failure(failure_point, "after_manifest")
        _fsync_directory(temporary)
        temporary.replace(final)
        _fsync_directory(snapshots)
        manifest_sha256 = _sha256(final / "SNAPSHOT_MANIFEST.json")
        pointer: dict[str, object] = {
            "created_at": created_at.isoformat(),
            "manifest_sha256": manifest_sha256,
            "revision": revision,
            "snapshot_id": snapshot_id,
        }
        _inject_failure(failure_point, "before_pointer")
        _write_private_json_atomic(
            backup_path / "last-good-pointer.json", pointer
        )
        _fsync_directory(backup_path)
    except Exception as error:
        if temporary.exists():
            shutil.rmtree(temporary)
        if isinstance(error, LastGoodError):
            raise
        raise LastGoodError("LAST_GOOD_PROMOTION_FAILED") from error
    return pointer


def resolve_last_good(backup_path: Path) -> dict[str, object]:
    """验证权威指针和整个快照后返回可供 rollback 使用的路径。

    Args:
        backup_path: 包含 last-good pointer 的私有备份目录。

    Returns:
        env、state 路径及已验证 revision。

    Raises:
        LastGoodError: 指针、manifest、exact set 或文件 SHA 无效。

    """
    pointer_path = backup_path / "last-good-pointer.json"
    pointer = _load_object(pointer_path, "pointer")
    if set(pointer) != _POINTER_FIELDS:
        raise LastGoodError("LAST_GOOD_POINTER_FIELDS_INVALID")
    snapshot_id = pointer.get("snapshot_id")
    revision = pointer.get("revision")
    manifest_sha256 = pointer.get("manifest_sha256")
    if (
        not isinstance(snapshot_id, str)
        or _SNAPSHOT.fullmatch(snapshot_id) is None
    ):
        raise LastGoodError("LAST_GOOD_SNAPSHOT_ID_INVALID")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise LastGoodError("LAST_GOOD_REVISION_INVALID")
    if (
        not isinstance(manifest_sha256, str)
        or _SHA256.fullmatch(manifest_sha256) is None
    ):
        raise LastGoodError("LAST_GOOD_MANIFEST_SHA_INVALID")
    snapshot = backup_path / "last-good-snapshots" / snapshot_id
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise LastGoodError("LAST_GOOD_SNAPSHOT_INVALID")
    expected_names = {
        "SNAPSHOT_MANIFEST.json",
        "rag-industry.env",
        "state.json",
    }
    if {path.name for path in snapshot.iterdir()} != expected_names:
        raise LastGoodError("LAST_GOOD_SNAPSHOT_EXACT_SET_INVALID")
    manifest_path = snapshot / "SNAPSHOT_MANIFEST.json"
    if _sha256(manifest_path) != manifest_sha256:
        raise LastGoodError("LAST_GOOD_MANIFEST_SHA_MISMATCH")
    manifest = _load_object(manifest_path, "snapshot manifest")
    files = manifest.get("files")
    if (
        manifest.get("schema_version") != "1"
        or manifest.get("revision") != revision
        or not isinstance(files, dict)
        or set(files) != {"rag-industry.env", "state.json"}
    ):
        raise LastGoodError("LAST_GOOD_MANIFEST_INVALID")
    for name in ("rag-industry.env", "state.json"):
        path = snapshot / name
        _require_private_file(path, name)
        if files.get(name) != _identity(path):
            raise LastGoodError("LAST_GOOD_FILE_IDENTITY_MISMATCH")
    return {
        "env_path": str(snapshot / "rag-industry.env"),
        "revision": revision,
        "snapshot_id": snapshot_id,
        "state_path": str(snapshot / "state.json"),
    }


def migrate_legacy_last_good(backup_path: Path) -> dict[str, object]:
    """在无新 pointer 时一次性只读迁移旧 last-good 双文件。

    Args:
        backup_path: Industry 私有备份目录。

    Returns:
        已存在或新创建的 last-good 解析结果。

    Raises:
        LastGoodError: 旧文件缺失、env revision 无效或迁移失败。

    """
    if (backup_path / "last-good-pointer.json").exists():
        return resolve_last_good(backup_path)
    env_path = backup_path / "last-good.env"
    state_path = backup_path / "last-good.json"
    _require_private_file(env_path, "legacy env")
    _require_private_file(state_path, "legacy state")
    revision = _exact_env_value(env_path, "RAG_RELEASE_REVISION")
    promote_last_good(backup_path, env_path, state_path, revision)
    return resolve_last_good(backup_path)


def _state_revision(state: dict[str, object]) -> str | None:
    revision = state.get("revision")
    if isinstance(revision, str):
        return revision
    target = state.get("target")
    if isinstance(target, dict):
        value = target.get("revision")
        return value if isinstance(value, str) else None
    return None


def _exact_env_value(path: Path, key: str) -> str:
    matches = [
        line.split("=", 1)[1].strip("\"'")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{key}=")
    ]
    if len(matches) != 1 or not matches[0]:
        raise LastGoodError(f"{key}_INVALID")
    return matches[0]


def _copy_private_file(source: Path, destination: Path) -> None:
    with source.open("rb") as input_stream:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())


def _write_private_json(path: Path, value: Mapping[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _write_private_json_atomic(
    path: Path, value: Mapping[str, object]
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"), sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_object(path: Path, label: str) -> dict[str, object]:
    _require_private_file(path, label)
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LastGoodError(
            f"LAST_GOOD_{label.upper()}_JSON_INVALID"
        ) from error
    if not isinstance(value, dict):
        raise LastGoodError(f"LAST_GOOD_{label.upper()}_INVALID")
    return value


def _require_private_file(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise LastGoodError(f"LAST_GOOD_{label.upper()}_FILE_INVALID")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise LastGoodError(f"LAST_GOOD_{label.upper()}_MODE_INVALID")


def _identity(path: Path) -> dict[str, object]:
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _inject_failure(
    selected: FailurePoint | None, current: FailurePoint
) -> None:
    if selected == current:
        raise LastGoodError(f"INJECTED_{current.upper()}")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    promote = commands.add_parser("promote")
    promote.add_argument("backup_path", type=Path)
    promote.add_argument("env_path", type=Path)
    promote.add_argument("state_path", type=Path)
    promote.add_argument("revision")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("backup_path", type=Path)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("backup_path", type=Path)
    return parser.parse_args()


def main() -> int:
    """执行 last-good promote、resolve 或一次性旧格式迁移。

    Returns:
        合同成立返回 0；失败返回 1 和稳定错误码。

    """
    arguments = _arguments()
    try:
        if arguments.command == "promote":
            promote_last_good(
                arguments.backup_path,
                arguments.env_path,
                arguments.state_path,
                arguments.revision,
            )
            value = resolve_last_good(arguments.backup_path)
        elif arguments.command == "resolve":
            value = resolve_last_good(arguments.backup_path)
        else:
            value = migrate_legacy_last_good(arguments.backup_path)
    except LastGoodError as error:
        print(f"RAG_INDUSTRY_LAST_GOOD_FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
