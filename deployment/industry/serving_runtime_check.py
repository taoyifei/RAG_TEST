#!/usr/bin/env python3
"""Industry serving update 的只读身份与 Trace 备份工具。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_HTTP_OK = 200
_CONFIG_NAMES = {
    "corpus-policy.json",
    "intent-router-calibration.json",
    "intent-router.json",
    "pipeline.json",
    "retrieval.json",
}
_CONFIG_PROFILE_MODES = {
    "first-deploy-private-v1": "0600",
    "serving-runtime-public-config-v1": "0644",
}


class RuntimeCheckError(RuntimeError):
    """表示 serving update 身份或备份合同不成立。"""


def pre_update_filesystem_state(
    config_directory: Path,
    trace_database: Path,
    config_profile: str,
    *,
    expected_sha256: dict[str, str] | None = None,
) -> dict[str, object]:
    """只读取得 container-owned config 与 Trace schema 身份。

    Args:
        config_directory: 挂载到旧 App 的五文件 config 目录。
        trace_database: 挂载到旧 App 的 Trace SQLite 文件。
        config_profile: 明确的来源 config 权限合同。
        expected_sha256: 可选的五文件精确 SHA256 合同。

    Returns:
        不含绝对路径、正文或 secret 的 canonical 身份字段。

    Raises:
        RuntimeCheckError: exact set、私有权限或 SQLite 合同不成立。

    """
    if not config_directory.is_dir() or config_directory.is_symlink():
        raise RuntimeCheckError("CONFIG_DIRECTORY_INVALID")
    expected_mode = _CONFIG_PROFILE_MODES.get(config_profile)
    if expected_mode is None:
        raise RuntimeCheckError("CONFIG_PROFILE_INVALID")
    entries = list(config_directory.iterdir())
    if {path.name for path in entries} != _CONFIG_NAMES:
        raise RuntimeCheckError("CONFIG_EXACT_SET_INVALID")
    if any(not path.is_file() or path.is_symlink() for path in entries):
        raise RuntimeCheckError("CONFIG_FILE_TYPE_INVALID")
    before = {
        path.name: _source_identity(path) for path in sorted(entries)
    }
    files = {
        path.name: {
            "gid": before[path.name]["gid"],
            "mode": before[path.name]["mode"],
            "sha256": _file_sha256(path),
            "uid": before[path.name]["uid"],
        }
        for path in sorted(entries)
    }
    if any(
        identity["mode"] != expected_mode for identity in before.values()
    ):
        raise RuntimeCheckError("CONFIG_FILE_MODE_INVALID")
    if expected_sha256 is not None:
        if (
            set(expected_sha256) != _CONFIG_NAMES
            or any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in expected_sha256.values()
            )
        ):
            raise RuntimeCheckError("CONFIG_EXPECTED_SHA256_INVALID")
        if any(
            files[name]["sha256"] != digest
            for name, digest in expected_sha256.items()
        ):
            raise RuntimeCheckError("CONFIG_FILE_SHA256_MISMATCH")
    _require_regular_private_source(trace_database)
    trace_before = _source_identity(trace_database)
    schema = trace_schema(trace_database)
    after = {
        path.name: _source_identity(path) for path in sorted(entries)
    }
    if before != after or trace_before != _source_identity(trace_database):
        raise RuntimeCheckError("PRE_UPDATE_SOURCE_MUTATED")
    return {
        "config": {"files": files, "profile": config_profile},
        "trace": {
            "filename": trace_database.name,
            "mode": trace_before["mode"],
            "sqlite_user_version": schema["sqlite_user_version"],
        },
    }


def pre_update_index_state() -> dict[str, object]:
    """从旧镜像现有依赖读取活动索引身份且不修改运行状态。

    Returns:
        经过 manifest、alias 和 point count 交叉校验的 canonical 字段。

    Raises:
        RuntimeCheckError: 环境、SQLite 或 Qdrant 身份不满足合同。

    """
    database = Path(_required_environment("RAG_MANIFEST_DATABASE"))
    alias = _required_environment("RAG_QDRANT_ALIAS")
    revision = _required_environment("RAG_RELEASE_REVISION")
    if _REVISION.fullmatch(revision) is None:
        raise RuntimeCheckError("RELEASE_REVISION_INVALID")
    row = _active_manifest(database)
    collection = _required_string(row, "collection_name")
    fingerprint = _required_sha256(row, "pipeline_fingerprint", prefix=True)
    manifest_text = _required_string(row, "manifest_json")
    manifest_sha256 = _required_sha256(row, "manifest_sha256", prefix=False)
    if hashlib.sha256(manifest_text.encode("utf-8")).hexdigest() != (
        manifest_sha256
    ):
        raise RuntimeCheckError("MANIFEST_SHA256_MISMATCH")
    manifest = _json_object(manifest_text.encode("utf-8"), "manifest")
    if manifest.get("collection_name") != collection:
        raise RuntimeCheckError("MANIFEST_COLLECTION_MISMATCH")
    if manifest.get("pipeline_fingerprint") != fingerprint:
        raise RuntimeCheckError("MANIFEST_FINGERPRINT_MISMATCH")
    aliases = _qdrant_json("GET", "/aliases").get("result")
    if not isinstance(aliases, dict):
        raise RuntimeCheckError("QDRANT_ALIASES_INVALID")
    rows = aliases.get("aliases")
    if not isinstance(rows, list):
        raise RuntimeCheckError("QDRANT_ALIASES_INVALID")
    targets = [
        item.get("collection_name")
        for item in rows
        if isinstance(item, dict) and item.get("alias_name") == alias
    ]
    if targets != [collection]:
        raise RuntimeCheckError("ACTIVE_ALIAS_MISMATCH")
    count_payload = _qdrant_json(
        "POST",
        f"/collections/{urllib.parse.quote(collection, safe='')}/points/count",
        {"exact": True},
    )
    result = count_payload.get("result")
    point_count = result.get("count") if isinstance(result, dict) else None
    if (
        not isinstance(point_count, int)
        or isinstance(point_count, bool)
        or point_count <= 0
    ):
        raise RuntimeCheckError("POINT_COUNT_INVALID")
    sources = manifest.get("sources")
    source_count = len(sources) if isinstance(sources, list) else None
    if source_count is not None and source_count <= 0:
        raise RuntimeCheckError("SOURCE_COUNT_INVALID")
    return {
        "active_collection": collection,
        "alias": alias,
        "index_fingerprint": fingerprint,
        "manifest_sha256": manifest_sha256,
        "payload_schema": "industry-pre-update-index-state-v1",
        "point_count": point_count,
        "release_revision": revision,
        "source_count": source_count,
    }


def backup_trace_database(
    source: Path,
    destination: Path,
    target_revision: str,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> dict[str, object]:
    """使用 SQLite 在线备份 API 创建 mode 0600 的更新前快照。

    Args:
        source: 活动 Trace SQLite 文件。
        destination: 不得预先存在的备份文件。
        target_revision: 此备份对应的目标 App 完整 Git revision。
        owner_uid: 最终快照 UID；省略时使用当前进程 UID。
        owner_gid: 最终快照 GID；省略时使用当前进程 GID。

    Returns:
        不含问题正文和绝对路径的备份身份。

    Raises:
        RuntimeCheckError: 路径、权限或 SQLite 备份校验失败。

    """
    _require_regular_private_source(source)
    if _REVISION.fullmatch(target_revision) is None:
        raise RuntimeCheckError("TRACE_BACKUP_TARGET_REVISION_INVALID")
    if owner_uid is None:
        owner_uid = os.getuid()
    if owner_gid is None:
        owner_gid = os.getgid()
    if owner_uid < 0 or owner_gid < 0:
        raise RuntimeCheckError("TRACE_BACKUP_OWNER_INVALID")
    source_identity = _source_identity(source)
    if destination.exists() or destination.is_symlink():
        raise RuntimeCheckError("TRACE_BACKUP_DESTINATION_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".trace-backup.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(source))}?mode=ro",
            uri=True,
        )
        destination_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(destination_connection)
            destination_connection.commit()
            integrity = destination_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            user_version = int(
                destination_connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
            )
            page_count = int(
                destination_connection.execute("PRAGMA page_count").fetchone()[
                    0
                ]
            )
        finally:
            destination_connection.close()
            source_connection.close()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeCheckError("TRACE_BACKUP_INTEGRITY_FAILED")
        os.chown(temporary, owner_uid, owner_gid)
        temporary.chmod(0o600)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.replace(destination)
        _fsync_directory(destination.parent)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        if isinstance(error, RuntimeCheckError):
            raise
        raise RuntimeCheckError("TRACE_BACKUP_FAILED") from error
    if source_identity != _source_identity(source):
        destination.unlink(missing_ok=True)
        raise RuntimeCheckError("TRACE_BACKUP_SOURCE_MUTATED")
    return {
        "backup_filename": destination.name,
        "bytes": destination.stat().st_size,
        "created_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "mode": "0600",
        "owner": {"gid": owner_gid, "uid": owner_uid},
        "page_count": page_count,
        "schema_version": "1",
        "sha256": _file_sha256(destination),
        "source_database_identity": source_identity,
        "source_filename": source.name,
        "sqlite_user_version": user_version,
        "target_revision": target_revision,
    }


def trace_schema(database: Path) -> dict[str, object]:
    """只读报告 Trace SQLite user_version 与问题字段状态。

    Args:
        database: Trace SQLite 路径。

    Returns:
        schema 版本和是否同时具有两个问题字段。

    """
    try:
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(database))}?mode=ro", uri=True
        )
        try:
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(traces)")
            }
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise RuntimeCheckError("TRACE_SCHEMA_READ_FAILED") from error
    return {
        "has_question_columns": {
            "question_text",
            "question_sha256",
        }.issubset(columns),
        "sqlite_user_version": version,
    }


def _active_manifest(database: Path) -> dict[str, object]:
    if not database.is_file() or database.is_symlink():
        raise RuntimeCheckError("MANIFEST_DATABASE_INVALID")
    before = database.stat()
    connection = sqlite3.connect(
        f"file:{urllib.parse.quote(str(database))}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT collection_name, pipeline_fingerprint, manifest_json,
                   manifest_sha256
            FROM index_manifests WHERE state='active'
            """
        ).fetchall()
    finally:
        connection.close()
    after = database.stat()
    if (before.st_mode, before.st_uid, before.st_gid, before.st_size) != (
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_size,
    ):
        raise RuntimeCheckError("MANIFEST_DATABASE_MUTATED")
    if len(rows) != 1:
        raise RuntimeCheckError("ACTIVE_MANIFEST_INVALID")
    return dict(rows[0])


def _qdrant_json(
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    base_url = _required_environment("RAG_QDRANT_URL").rstrip("/")
    qdrant_credential = _required_environment("RAG_QDRANT_API_KEY")
    data = None
    headers = {"api-key": qdrant_credential, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(  # noqa: S310
        f"{base_url}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            if response.status != _HTTP_OK:
                raise RuntimeCheckError("QDRANT_RESPONSE_INVALID")
            return _json_object(response.read(), "qdrant response")
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeCheckError("QDRANT_REQUEST_FAILED") from error


def _required_environment(key: str) -> str:
    value = os.environ.get(key)
    if value is None or not value:
        raise RuntimeCheckError(f"{key}_MISSING")
    return value


def _required_owner(key: str) -> int:
    value = _required_environment(key)
    if not value.isdigit():
        raise RuntimeCheckError(f"{key}_INVALID")
    owner = int(value)
    if owner < 0:
        raise RuntimeCheckError(f"{key}_INVALID")
    return owner


def _required_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RuntimeCheckError(f"{key.upper()}_INVALID")
    return item


def _required_sha256(
    value: dict[str, object], key: str, *, prefix: bool
) -> str:
    item = _required_string(value, key)
    expected_length = 71 if prefix else 64
    if len(item) != expected_length or _SHA256.fullmatch(item) is None:
        raise RuntimeCheckError(f"{key.upper()}_INVALID")
    return item


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeCheckError(f"{label} JSON 无效。") from error
    if not isinstance(value, dict):
        raise RuntimeCheckError(f"{label} 必须是 JSON object。")
    return value


def _require_regular_private_source(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeCheckError("TRACE_DATABASE_INVALID")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RuntimeCheckError("TRACE_DATABASE_NOT_PRIVATE")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_identity(path: Path) -> dict[str, object]:
    value = path.stat()
    return {
        "bytes": value.st_size,
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "mode": f"{stat.S_IMODE(value.st_mode):04o}",
        "mtime_ns": value.st_mtime_ns,
        "uid": value.st_uid,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pre-update-index-state")
    filesystem = commands.add_parser("pre-update-filesystem-state")
    filesystem.add_argument("config_directory", type=Path)
    filesystem.add_argument("trace_database", type=Path)
    filesystem.add_argument("config_profile")
    for command in ("backup-trace-database", "backup-trace"):
        backup = commands.add_parser(command)
        backup.add_argument("source", type=Path)
        backup.add_argument("destination", type=Path)
        backup.add_argument("target_revision")
    schema = commands.add_parser("trace-schema")
    schema.add_argument("database", type=Path)
    return parser.parse_args()


def main() -> int:
    """执行只读身份、在线 Trace 备份或 schema 报告命令。

    Returns:
        合同成立返回 0，否则返回 1 且只输出稳定错误码。

    """
    arguments = _arguments()
    try:
        if arguments.command == "pre-update-index-state":
            result = pre_update_index_state()
        elif arguments.command == "pre-update-filesystem-state":
            result = pre_update_filesystem_state(
                arguments.config_directory,
                arguments.trace_database,
                arguments.config_profile,
            )
        elif arguments.command in {
            "backup-trace-database",
            "backup-trace",
        }:
            result = backup_trace_database(
                arguments.source,
                arguments.destination,
                arguments.target_revision,
                owner_uid=_required_owner("RAG_UPDATE_OWNER_UID"),
                owner_gid=_required_owner("RAG_UPDATE_OWNER_GID"),
            )
        else:
            result = trace_schema(arguments.database)
    except RuntimeCheckError as error:
        print(f"RAG_INDUSTRY_RUNTIME_CHECK_FAILED: {error}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError):
        print(
            "RAG_INDUSTRY_RUNTIME_CHECK_FAILED: RUNTIME_IO_INVALID",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
