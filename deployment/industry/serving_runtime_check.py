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
_PRIVATE_MODE = 0o600
_TRACE_SCHEMA_TARGET_VERSION = 2
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
_LEGACY_2C4_SCHEMA_PROFILE = "industry-trace-2c4-v0"
_LEGACY_2C4_SCHEMA = """
CREATE TABLE traces (
    trace_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    pipeline_fingerprint TEXT NOT NULL,
    serving_fingerprint TEXT NOT NULL,
    release_revision TEXT NOT NULL,
    active_collection TEXT NOT NULL,
    index_manifest_sha256 TEXT NOT NULL,
    payload_schema_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    refusal_code TEXT,
    error_code TEXT,
    feedback_useful INTEGER,
    capture_complete INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX traces_created_idx
ON traces(created_at DESC, trace_id DESC);
CREATE INDEX traces_expires_idx ON traces(expires_at);

CREATE TABLE artifacts (
    trace_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    original_bytes INTEGER NOT NULL,
    compressed_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    compressed_payload BLOB NOT NULL,
    PRIMARY KEY (trace_id, artifact_id),
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
);

CREATE TABLE spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    sequence INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    status TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    input_artifact_id TEXT,
    output_artifact_id TEXT,
    PRIMARY KEY (trace_id, span_id),
    UNIQUE (trace_id, sequence),
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE,
    FOREIGN KEY (trace_id, parent_span_id)
        REFERENCES spans(trace_id, span_id),
    FOREIGN KEY (trace_id, input_artifact_id)
        REFERENCES artifacts(trace_id, artifact_id),
    FOREIGN KEY (trace_id, output_artifact_id)
        REFERENCES artifacts(trace_id, artifact_id)
);

CREATE TABLE candidate_decisions (
    trace_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    stage TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    selected INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY (trace_id, sequence),
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
);
"""


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
    trace_before = _stable_source_identity(trace_database)
    schema = trace_schema(trace_database)
    after = {
        path.name: _source_identity(path) for path in sorted(entries)
    }
    if before != after:
        raise RuntimeCheckError("PRE_UPDATE_SOURCE_MUTATED")
    try:
        trace_after = _stable_source_identity(trace_database)
    except (OSError, RuntimeCheckError) as error:
        raise RuntimeCheckError("PRE_UPDATE_SOURCE_MUTATED") from error
    if trace_before != trace_after:
        raise RuntimeCheckError("PRE_UPDATE_SOURCE_MUTATED")
    return {
        "config": {"files": files, "profile": config_profile},
        "trace": {
            "filename": trace_database.name,
            "has_question_columns": schema["has_question_columns"],
            "mode": trace_before["mode"],
            "quick_check": schema["quick_check"],
            "schema_profile": schema["schema_profile"],
            "sqlite_user_version": schema["sqlite_user_version"],
            "trace_count": schema["trace_count"],
        },
    }


def pre_update_index_state() -> dict[str, object]:
    """从旧镜像现有依赖读取活动索引身份且不修改运行状态。

    Args:
        无参数；所有输入来自受控环境变量。

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


def backup_trace_database(  # noqa: PLR0912, PLR0915
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
    source_identity = _stable_source_identity(source)
    source_before = _volatile_source_observation(source)
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
            integrity, page_count, user_version, trace_count = _sqlite_identity(
                destination_connection
            )
        finally:
            destination_connection.close()
            source_connection.close()
        if integrity != "ok":
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
    try:
        source_after_identity = _stable_source_identity(source)
        source_after = _volatile_source_observation(source)
    except (OSError, RuntimeCheckError) as error:
        destination.unlink(missing_ok=True)
        raise RuntimeCheckError("TRACE_BACKUP_SOURCE_MUTATED") from error
    if source_identity != source_after_identity:
        destination.unlink(missing_ok=True)
        raise RuntimeCheckError("TRACE_BACKUP_SOURCE_MUTATED")
    try:
        with sqlite3.connect(
            f"file:{urllib.parse.quote(str(destination))}?mode=ro", uri=True
        ) as published_connection:
            published = _sqlite_identity(published_connection)
    except sqlite3.Error as error:
        destination.unlink(missing_ok=True)
        raise RuntimeCheckError("TRACE_BACKUP_INTEGRITY_FAILED") from error
    if published != (integrity, page_count, user_version, trace_count):
        destination.unlink(missing_ok=True)
        raise RuntimeCheckError("TRACE_BACKUP_INTEGRITY_FAILED")
    source_changed = source_before != source_after
    return {
        "backup_filename": destination.name,
        "bytes": destination.stat().st_size,
        "created_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "mode": "0600",
        "owner": {"gid": owner_gid, "uid": owner_uid},
        "page_count": page_count,
        "schema_version": "2",
        "sha256": _file_sha256(destination),
        "source_changed_during_backup": source_changed,
        "source_database_identity": source_identity,
        "source_database_observation": {
            "after": source_after,
            "before": source_before,
        },
        "source_filename": source.name,
        "sqlite_user_version": user_version,
        "target_revision": target_revision,
        "trace_count": trace_count,
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
            quick_check = str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            )
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(traces)")
            }
            trace_count = int(
                connection.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
            )
            if quick_check != "ok":
                raise RuntimeCheckError("TRACE_QUICK_CHECK_FAILED")
            question_columns = {
                "question_text",
                "question_sha256",
            } & columns
            if question_columns and question_columns != {
                "question_text",
                "question_sha256",
            }:
                raise RuntimeCheckError("TRACE_QUESTION_COLUMNS_PARTIAL")
            if version == 0:
                if _sqlite_schema_identity(connection) != (
                    _legacy_2c4_schema_identity()
                ):
                    raise RuntimeCheckError(
                        "TRACE_LEGACY_V0_SCHEMA_MISMATCH"
                    )
                schema_profile = _LEGACY_2C4_SCHEMA_PROFILE
            elif version == 1:
                schema_profile = "trace-v1"
            elif (
                version == _TRACE_SCHEMA_TARGET_VERSION
                and question_columns
                == {
                    "question_text",
                    "question_sha256",
                }
            ):
                schema_profile = "trace-v2"
            elif version == _TRACE_SCHEMA_TARGET_VERSION:
                raise RuntimeCheckError("TRACE_SCHEMA_VERSION_MISMATCH")
            else:
                raise RuntimeCheckError("TRACE_SCHEMA_VERSION_UNSUPPORTED")
        finally:
            connection.close()
    except RuntimeCheckError:
        raise
    except sqlite3.Error as error:
        raise RuntimeCheckError("TRACE_SCHEMA_READ_FAILED") from error
    return {
        "has_question_columns": bool(question_columns),
        "quick_check": quick_check,
        "schema_profile": schema_profile,
        "sqlite_user_version": version,
        "trace_count": trace_count,
    }


def _legacy_2c4_schema_identity() -> dict[str, object]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_LEGACY_2C4_SCHEMA)
        return _sqlite_schema_identity(connection)
    finally:
        connection.close()


def _sqlite_schema_identity(
    connection: sqlite3.Connection,
) -> dict[str, object]:
    """返回不含数据的 SQLite 表、列、索引和外键结构身份。"""
    tables = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    )
    auxiliary = tuple(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('trigger', 'view') ORDER BY type, name"
        )
    )
    table_identities: dict[str, object] = {}
    for table in tables:
        escaped = table.replace('"', '""')
        columns = tuple(
            tuple(row)
            for row in connection.execute(
                f'PRAGMA table_info("{escaped}")'
            )
        )
        foreign_keys = tuple(
            tuple(row)
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{escaped}")'
            )
        )
        indexes = []
        for row in connection.execute(f'PRAGMA index_list("{escaped}")'):
            index_name = str(row[1])
            escaped_index = index_name.replace('"', '""')
            index_columns = tuple(
                tuple(item)
                for item in connection.execute(
                    f'PRAGMA index_xinfo("{escaped_index}")'
                )
            )
            indexes.append((tuple(row), index_columns))
        table_identities[table] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": tuple(indexes),
        }
    return {
        "auxiliary": auxiliary,
        "tables": table_identities,
    }


def validate_runtime_state(
    pre_index_path: Path,
    target_contract_path: Path,
    verified_state_path: Path | None,
    update_manifest_path: Path,
    runtime_state_path: Path,
) -> dict[str, object]:
    """交叉验证 target 运行态、索引不变量和更新合同。

    Args:
        pre_index_path: 激活前保存的索引身份。
        target_contract_path: 从更新 manifest 派生的 target 合同。
        verified_state_path: 已完成 verify 的状态；首次 verify 可省略。
        update_manifest_path: 当前事务冻结的 UPDATE_MANIFEST。
        runtime_state_path: 当前 App 实时导出的 runtime-state v2。

    Returns:
        可审计但不含 secret 的 canonical 验证摘要。

    Raises:
        RuntimeCheckError: 任一字段、类型或跨文件关系不成立。

    """
    pre = _private_json_object(pre_index_path, "pre index")
    target = _private_json_object(target_contract_path, "target contract")
    manifest = _private_json_object(update_manifest_path, "update manifest")
    actual = _private_json_object(runtime_state_path, "runtime state")
    verified = (
        _private_json_object(verified_state_path, "verified state")
        if verified_state_path is not None
        else None
    )
    _validate_runtime_contract_inputs(pre, target, manifest)
    expected_fields = {
        "active_collection",
        "alias",
        "index_fingerprint",
        "installed_revision",
        "manifest_sha256",
        "point_count",
        "production_ready",
        "release_matches",
        "release_revision",
        "run_mode",
        "schema_version",
        "serving_fingerprint",
        "trace_question_capture",
        "trace_question_retention_seconds",
        "trace_schema_version",
        "ui_cookie_secure",
        "ui_query_auth_mode",
    }
    if set(actual) != expected_fields:
        raise RuntimeCheckError("RUNTIME_STATE_FIELDS_INVALID")
    index_fields = {
        "active_collection",
        "alias",
        "index_fingerprint",
        "manifest_sha256",
        "point_count",
    }
    if any(actual.get(key) != pre.get(key) for key in index_fields):
        raise RuntimeCheckError("RUNTIME_INDEX_IDENTITY_DRIFT")
    trace = target["trace"]
    ui = target["ui"]
    revision = target["revision"]
    if not isinstance(trace, dict) or not isinstance(ui, dict):
        raise RuntimeCheckError("TARGET_CONTRACT_INVALID")
    if (
        actual.get("schema_version") != "2"
        or actual.get("release_revision") != revision
        or actual.get("installed_revision") != revision
        or actual.get("release_matches") is not True
        or actual.get("serving_fingerprint")
        != target.get("serving_fingerprint")
        or actual.get("ui_query_auth_mode") != ui["query_auth_mode"]
        or actual.get("ui_cookie_secure") != ui["cookie_secure"]
        or actual.get("trace_question_capture")
        != trace["question_capture"]
        or actual.get("trace_question_retention_seconds")
        != trace["question_retention_seconds"]
        or actual.get("trace_schema_version") != trace["schema_version"]
        or actual.get("run_mode") != "demo"
        or actual.get("production_ready") is not False
    ):
        raise RuntimeCheckError("RUNTIME_SERVING_CONTRACT_MISMATCH")
    fingerprint = actual.get("serving_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
    ):
        raise RuntimeCheckError("SERVING_FINGERPRINT_INVALID")
    if verified is not None:
        expected_verified = {
            "index": {key: actual[key] for key in sorted(index_fields)},
            "revision": revision,
            "schema_version": "2",
            "stage": "last_good",
            "update_kind": "serving_app_update",
        }
        if verified != expected_verified:
            raise RuntimeCheckError("VERIFIED_STATE_MISMATCH")
    return {
        "index_fingerprint": actual["index_fingerprint"],
        "revision": revision,
        "schema_version": "1",
        "serving_fingerprint": fingerprint,
        "verified_state_checked": verified is not None,
    }


def _validate_runtime_contract_inputs(
    pre: dict[str, object],
    target: dict[str, object],
    manifest: dict[str, object],
) -> None:
    """验证运行态比较所依赖的三个静态合同。"""
    pre_fields = {
        "active_collection",
        "alias",
        "index_fingerprint",
        "manifest_sha256",
        "payload_schema",
        "point_count",
        "release_revision",
        "source_count",
    }
    if set(pre) != pre_fields:
        raise RuntimeCheckError("PRE_INDEX_FIELDS_INVALID")
    point_count = pre.get("point_count")
    if (
        not isinstance(point_count, int)
        or isinstance(point_count, bool)
        or point_count <= 0
        or _SHA256.fullmatch(str(pre.get("index_fingerprint"))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(pre.get("manifest_sha256")))
        is None
    ):
        raise RuntimeCheckError("PRE_INDEX_IDENTITY_INVALID")
    if set(target) != {
        "index_fingerprint",
        "revision",
        "serving_fingerprint",
        "trace",
        "ui",
    }:
        raise RuntimeCheckError("TARGET_CONTRACT_FIELDS_INVALID")
    trace = target.get("trace")
    ui = target.get("ui")
    if (
        not isinstance(trace, dict)
        or trace
        != {
            "question_capture": "plaintext",
            "question_retention_seconds": 604800,
            "schema_version": 2,
        }
        or not isinstance(ui, dict)
        or ui
        != {
            "allow_insecure_http": True,
            "cookie_secure": False,
            "query_auth_mode": "same_origin_session",
            "session_ttl_seconds": 1800,
        }
        or not isinstance(target.get("revision"), str)
        or _REVISION.fullmatch(str(target["revision"])) is None
        or _SHA256.fullmatch(str(target.get("index_fingerprint"))) is None
        or _SHA256.fullmatch(str(target.get("serving_fingerprint"))) is None
    ):
        raise RuntimeCheckError("TARGET_CONTRACT_INVALID")
    index = manifest.get("index_fingerprint")
    serving = manifest.get("serving_fingerprint")
    if (
        manifest.get("revision") != target["revision"]
        or not isinstance(index, dict)
        or index.get("target") != target["index_fingerprint"]
        or index.get("reindex_required") is not False
        or not isinstance(serving, dict)
        or serving.get("target") != target["serving_fingerprint"]
        or manifest.get("trace") != trace
        or manifest.get("ui") != ui
    ):
        raise RuntimeCheckError("UPDATE_MANIFEST_CONTRACT_MISMATCH")
    if pre.get("index_fingerprint") != target["index_fingerprint"]:
        raise RuntimeCheckError("INDEX_FINGERPRINT_CHANGED")


def _private_json_object(path: Path, label: str) -> dict[str, object]:
    """读取 mode 0600 的普通 JSON object 文件。"""
    try:
        value = path.lstat()
    except OSError as error:
        raise RuntimeCheckError(f"{label.upper()}_FILE_INVALID") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(value.st_mode)
        or stat.S_IMODE(value.st_mode) != _PRIVATE_MODE
    ):
        raise RuntimeCheckError(f"{label.upper()}_FILE_INVALID")
    return _json_object(path.read_bytes(), label)


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
    try:
        value = path.lstat()
    except OSError as error:
        raise RuntimeCheckError("TRACE_DATABASE_INVALID") from error
    if path.is_symlink() or not stat.S_ISREG(value.st_mode):
        raise RuntimeCheckError("TRACE_DATABASE_INVALID")
    mode = stat.S_IMODE(value.st_mode)
    if mode != _PRIVATE_MODE:
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


def _stable_source_identity(path: Path) -> dict[str, object]:
    """返回活动 SQLite 文件不可在备份期间漂移的身份。

    Args:
        path: 待检查的活动 SQLite 文件。

    Returns:
        不含 size 和 mtime 的稳定文件身份。

    Raises:
        RuntimeCheckError: 路径不是 mode 0600 的普通非符号链接文件。

    """
    _require_regular_private_source(path)
    value = path.lstat()
    return {
        "device": value.st_dev,
        "file_type": "regular",
        "gid": value.st_gid,
        "inode": value.st_ino,
        "mode": f"{stat.S_IMODE(value.st_mode):04o}",
        "uid": value.st_uid,
    }


def _volatile_source_observation(path: Path) -> dict[str, int | None]:
    """记录活动 SQLite 的可变大小和时间观测值。

    Args:
        path: 活动 SQLite 主文件。

    Returns:
        主文件大小、mtime 及可选 WAL 大小。

    """
    value = path.stat()
    wal_path = Path(f"{path}-wal")
    wal_bytes = None
    if wal_path.exists() and not wal_path.is_symlink():
        wal_value = wal_path.stat()
        if stat.S_ISREG(wal_value.st_mode):
            wal_bytes = wal_value.st_size
    return {
        "bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "wal_bytes": wal_bytes,
    }


def _sqlite_identity(
    connection: sqlite3.Connection,
) -> tuple[str, int, int, int]:
    """读取 SQLite 完整性、页数、版本和 Trace 条数。

    Args:
        connection: 已打开的 SQLite 连接。

    Returns:
        integrity_check、page_count、user_version 与 Trace 条数。

    """
    integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
    integrity = str(integrity_row[0]) if integrity_row is not None else ""
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    user_version = int(
        connection.execute("PRAGMA user_version").fetchone()[0]
    )
    trace_count = int(
        connection.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    )
    return integrity, page_count, user_version, trace_count


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
    runtime = commands.add_parser("validate-runtime-state")
    runtime.add_argument("pre_index", type=Path)
    runtime.add_argument("target_contract", type=Path)
    runtime.add_argument("verified_state")
    runtime.add_argument("update_manifest", type=Path)
    runtime.add_argument("runtime_state", type=Path)
    return parser.parse_args()


def main() -> int:
    """执行只读身份、在线 Trace 备份或 schema 报告命令。

    Args:
        无参数；命令行参数由 argparse 解析。

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
        elif arguments.command == "trace-schema":
            result = trace_schema(arguments.database)
        else:
            verified_state = (
                None
                if arguments.verified_state == "-"
                else Path(arguments.verified_state)
            )
            result = validate_runtime_state(
                arguments.pre_index,
                arguments.target_contract,
                verified_state,
                arguments.update_manifest,
                arguments.runtime_state,
            )
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
