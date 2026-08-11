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
_PRIVATE_MODE = 0o600


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
    return _create_last_good_snapshot(
        backup_path,
        env_path,
        state_path,
        revision,
        publish_pointer=True,
        failure_point=failure_point,
    )


def _create_last_good_snapshot(  # noqa: PLR0913
    backup_path: Path,
    env_path: Path,
    state_path: Path,
    revision: str,
    *,
    publish_pointer: bool,
    failure_point: FailurePoint | None = None,
) -> dict[str, object]:
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
        if publish_pointer:
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
    resolved = _resolve_snapshot(
        backup_path,
        snapshot_id,
        revision,
        manifest_sha256,
    )
    return {
        **resolved,
        "revision": revision,
        "snapshot_id": snapshot_id,
    }


def _resolve_snapshot(
    backup_path: Path,
    snapshot_id: str,
    revision: str,
    manifest_sha256: str,
) -> dict[str, object]:
    """不依赖 pointer 完整验证一个指定 revision 的版本化快照。"""
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


def inspect_last_good(backup_path: Path) -> dict[str, object]:
    """只读分类新 pointer 和两种 legacy 历史布局。

    Args:
        backup_path: Industry 私有备份目录。

    Returns:
        不含绝对路径的 canonical 分类与文件身份。

    Raises:
        LastGoodError: pointer 损坏、state-only 或 legacy 文件不可信。

    """
    pointer_path = backup_path / "last-good-pointer.json"
    if _exists_or_symlink(pointer_path):
        return _pointer_evidence(backup_path)
    env_path = backup_path / "last-good.env"
    state_path = backup_path / "last-good.json"
    env_exists = _exists_or_symlink(env_path)
    state_exists = _exists_or_symlink(state_path)
    if state_exists and not env_exists:
        raise LastGoodError("LEGACY_LAST_GOOD_STATE_ONLY_INVALID")
    if not env_exists:
        return {"schema_version": "1", "state": "absent"}
    _require_private_file(env_path, "legacy env")
    revision = _exact_env_value(env_path, "RAG_RELEASE_REVISION")
    if _REVISION.fullmatch(revision) is None:
        raise LastGoodError("LEGACY_LAST_GOOD_REVISION_INVALID")
    value: dict[str, object] = {
        "env": _evidence_identity(env_path),
        "revision": revision,
        "schema_version": "1",
        "state": "legacy_env_only",
    }
    if state_exists:
        _require_private_file(state_path, "legacy state")
        state = _load_object(state_path, "legacy state")
        if _state_revision(state) != revision:
            raise LastGoodError("LEGACY_LAST_GOOD_REVISION_MISMATCH")
        value["legacy_state"] = _evidence_identity(state_path)
        value["state"] = "legacy_pair"
    return value


def checkpoint_source_last_good(  # noqa: PLR0913, PLR0917
    backup_path: Path,
    env_path: Path,
    state_path: Path,
    revision: str,
    inspection_path: Path,
    update_manifest_path: Path,
) -> dict[str, object]:
    """在 source 全验证后封存可审计的更新前快照。

    Args:
        backup_path: Industry 私有备份目录。
        env_path: 当前实际 source private env。
        state_path: 当前 source 的 canonical 状态。
        revision: 当前 source 完整 Git SHA。
        inspection_path: source 验证前保存的只读 last-good 分类。
        update_manifest_path: 声明可信历史 revision 的更新 manifest。

    Returns:
        source snapshot、前后 pointer 与 legacy 证据。

    Raises:
        LastGoodError: 证据漂移、source 不匹配或未知 pointer revision。

    """
    if _REVISION.fullmatch(revision) is None:
        raise LastGoodError("SOURCE_CHECKPOINT_REVISION_INVALID")
    _require_private_file(env_path, "source env")
    _require_private_file(state_path, "source state")
    if _state_revision(_load_object(state_path, "source state")) != revision:
        raise LastGoodError("SOURCE_CHECKPOINT_STATE_REVISION_MISMATCH")
    before = _load_object(inspection_path, "inspection")
    if inspect_last_good(backup_path) != before:
        raise LastGoodError("SOURCE_CHECKPOINT_PRE_EVIDENCE_DRIFT")
    trusted = _trusted_last_good_revisions(update_manifest_path, revision)
    state = before.get("state")
    if state in {"legacy_env_only", "legacy_pair"}:
        env_identity = before.get("env")
        if (
            before.get("revision") != revision
            or not isinstance(env_identity, dict)
            or env_identity.get("sha256") != _sha256(env_path)
        ):
            raise LastGoodError("LEGACY_LAST_GOOD_SOURCE_MISMATCH")
    elif state == "pointer":
        pointer_revision = before.get("revision")
        if pointer_revision not in trusted:
            raise LastGoodError("LAST_GOOD_POINTER_REVISION_UNTRUSTED")
    elif state != "absent":
        raise LastGoodError("LAST_GOOD_INSPECTION_STATE_INVALID")

    pointer_before = _current_pointer_evidence(backup_path)
    publish_pointer = state == "pointer"
    if publish_pointer and before.get("revision") == revision:
        resolved = resolve_last_good(backup_path)
        if (
            _sha256(Path(str(resolved["env_path"]))) == _sha256(env_path)
            and _sha256(Path(str(resolved["state_path"])))
            == _sha256(state_path)
        ):
            return {
                "pointer_after": pointer_before,
                "pointer_before": pointer_before,
                "reused": True,
                "revision": revision,
                "schema_version": "1",
                "source_snapshot": _snapshot_evidence(
                    backup_path,
                    str(resolved["snapshot_id"]),
                ),
            }
    pointer = _create_last_good_snapshot(
        backup_path,
        env_path,
        state_path,
        revision,
        publish_pointer=publish_pointer,
    )
    after = _current_pointer_evidence(backup_path)
    if not publish_pointer and after != {"state": "absent"}:
        raise LastGoodError("SOURCE_CHECKPOINT_POINTER_UNEXPECTED")
    return {
        "pointer_after": after,
        "pointer_before": pointer_before,
        "reused": False,
        "revision": revision,
        "schema_version": "1",
        "source_snapshot": _snapshot_evidence(
            backup_path,
            str(pointer["snapshot_id"]),
        ),
    }


def finalize_target_last_good(  # noqa: PLR0913, PLR0917
    backup_path: Path,
    target_env_path: Path,
    verified_state_path: Path,
    target_revision: str,
    inspection_path: Path,
    source_state_path: Path,
    source_checkpoint_path: Path,
) -> dict[str, object]:
    """按 source checkpoint 精确状态晋升或核对 target pointer。

    Args:
        backup_path: Industry 私有备份目录。
        target_env_path: 当前 target private env。
        verified_state_path: target 验证状态。
        target_revision: target 完整 Git SHA。
        inspection_path: 更新前只读 last-good 分类。
        source_state_path: 已封存的 source canonical 状态。
        source_checkpoint_path: source snapshot 与 pointer 前后证据。

    Returns:
        晋升后已完整解析的 target last-good。

    Raises:
        LastGoodError: 证据、pointer 或 target 内容发生漂移。

    """
    if _REVISION.fullmatch(target_revision) is None:
        raise LastGoodError("LAST_GOOD_TARGET_REVISION_INVALID")
    _require_private_file(target_env_path, "target env")
    _require_private_file(verified_state_path, "verified state")
    if (
        _state_revision(_load_object(verified_state_path, "verified state"))
        != target_revision
    ):
        raise LastGoodError("LAST_GOOD_VERIFIED_STATE_REVISION_MISMATCH")
    inspection = _load_object(inspection_path, "inspection")
    source_state = _load_object(source_state_path, "source state")
    checkpoint = _load_object(source_checkpoint_path, "source checkpoint")
    source_snapshot = checkpoint.get("source_snapshot")
    if (
        checkpoint.get("schema_version") != "1"
        or checkpoint.get("revision") != _state_revision(source_state)
        or not isinstance(source_snapshot, dict)
        or source_snapshot.get("state_sha256") != _sha256(source_state_path)
    ):
        raise LastGoodError("SOURCE_CHECKPOINT_EVIDENCE_INVALID")
    snapshot_id = source_snapshot.get("snapshot_id")
    snapshot_manifest_sha = source_snapshot.get("manifest_sha256")
    if (
        set(source_snapshot)
        != {
            "env_sha256",
            "manifest_sha256",
            "snapshot_id",
            "state_sha256",
        }
        or not isinstance(snapshot_id, str)
        or _SNAPSHOT.fullmatch(snapshot_id) is None
        or not isinstance(snapshot_manifest_sha, str)
        or _SHA256.fullmatch(snapshot_manifest_sha) is None
    ):
        raise LastGoodError("SOURCE_CHECKPOINT_SNAPSHOT_INVALID")
    resolved_source = _resolve_snapshot(
        backup_path,
        snapshot_id,
        str(checkpoint["revision"]),
        snapshot_manifest_sha,
    )
    if (
        _sha256(Path(str(resolved_source["env_path"])))
        != source_snapshot.get("env_sha256")
        or _sha256(Path(str(resolved_source["state_path"])))
        != source_snapshot.get("state_sha256")
    ):
        raise LastGoodError("SOURCE_CHECKPOINT_SNAPSHOT_INVALID")
    expected_before = (
        {
            "pointer": inspection.get("pointer"),
            "pointer_sha256": inspection.get("pointer_sha256"),
            "revision": inspection.get("revision"),
            "state": "pointer",
        }
        if inspection.get("state") == "pointer"
        else {"state": "absent"}
    )
    if checkpoint.get("pointer_before") != expected_before:
        raise LastGoodError("SOURCE_CHECKPOINT_POINTER_BEFORE_INVALID")
    expected_current = checkpoint.get("pointer_after")
    if not isinstance(expected_current, dict):
        raise LastGoodError("SOURCE_CHECKPOINT_POINTER_AFTER_INVALID")
    current = _current_pointer_evidence(backup_path)
    if current.get("state") == "pointer" and (
        current.get("revision") == target_revision
    ):
        return _require_target_pointer_content(
            backup_path,
            target_env_path,
            verified_state_path,
            target_revision,
        )
    if current != expected_current:
        raise LastGoodError("LAST_GOOD_POINTER_STATE_MISMATCH")
    promote_last_good(
        backup_path,
        target_env_path,
        verified_state_path,
        target_revision,
    )
    return _require_target_pointer_content(
        backup_path,
        target_env_path,
        verified_state_path,
        target_revision,
    )


def restore_source_pointer(  # noqa: PLR0913, PLR0917
    backup_path: Path,
    source_checkpoint_path: Path,
    source_state_path: Path,
    source_env_path: Path,
    update_manifest_path: Path,
    target_env_path: Path,
    verified_state_path: Path,
    *,
    validate_only: bool = False,
) -> dict[str, object]:
    """将已验证 target pointer 原子恢复到事务密封的 source snapshot。

    Args:
        backup_path: Industry 私有备份目录。
        source_checkpoint_path: 事务创建的 source checkpoint。
        source_state_path: 更新前 source canonical 状态。
        source_env_path: 更新前 private env 副本。
        update_manifest_path: 本次更新的 UPDATE_MANIFEST。
        target_env_path: 本次事务的 candidate target env。
        verified_state_path: 已验证 target runtime 状态。
        validate_only: 只验证 target 到 source 的切换合同，不发布 pointer。

    Returns:
        已完整解析的 source last-good 身份。

    Raises:
        LastGoodError: source、target、snapshot 或 pointer 身份不可信。

    """
    for path, label in (
        (source_checkpoint_path, "source checkpoint"),
        (source_state_path, "source state"),
        (source_env_path, "source env"),
        (update_manifest_path, "update manifest"),
        (target_env_path, "target env"),
        (verified_state_path, "verified state"),
    ):
        _require_private_file(path, label)
    checkpoint = _load_object(source_checkpoint_path, "source checkpoint")
    manifest = _load_object(update_manifest_path, "update manifest")
    source_state = _load_object(source_state_path, "source state")
    verified_state = _load_object(verified_state_path, "verified state")
    source_revision = _state_revision(source_state)
    target_revision = manifest.get("revision")
    compatibility = manifest.get("source_compatibility")
    source_snapshot = checkpoint.get("source_snapshot")
    compatible_revisions = (
        compatibility.get("compatible_revisions")
        if isinstance(compatibility, dict)
        else None
    )
    if (
        not isinstance(source_revision, str)
        or _REVISION.fullmatch(source_revision) is None
        or not isinstance(target_revision, str)
        or _REVISION.fullmatch(target_revision) is None
        or source_revision == target_revision
        or not isinstance(compatibility, dict)
        or not isinstance(compatible_revisions, list)
        or source_revision not in compatible_revisions
        or checkpoint.get("revision") != source_revision
        or not isinstance(source_snapshot, dict)
    ):
        raise LastGoodError("SOURCE_POINTER_RESTORE_CONTRACT_INVALID")
    if (
        _exact_env_value(source_env_path, "RAG_RELEASE_REVISION")
        != source_revision
        or _exact_env_value(target_env_path, "RAG_RELEASE_REVISION")
        != target_revision
        or _state_revision(verified_state) != target_revision
    ):
        raise LastGoodError("SOURCE_POINTER_RESTORE_REVISION_MISMATCH")
    snapshot_id = source_snapshot.get("snapshot_id")
    manifest_sha256 = source_snapshot.get("manifest_sha256")
    if (
        set(source_snapshot)
        != {
            "env_sha256",
            "manifest_sha256",
            "snapshot_id",
            "state_sha256",
        }
        or not isinstance(snapshot_id, str)
        or _SNAPSHOT.fullmatch(snapshot_id) is None
        or not isinstance(manifest_sha256, str)
        or _SHA256.fullmatch(manifest_sha256) is None
    ):
        raise LastGoodError("SOURCE_POINTER_RESTORE_SNAPSHOT_INVALID")
    resolved_source = _resolve_snapshot(
        backup_path,
        snapshot_id,
        source_revision,
        manifest_sha256,
    )
    if (
        source_snapshot.get("env_sha256") != _sha256(source_env_path)
        or source_snapshot.get("state_sha256") != _sha256(source_state_path)
        or _sha256(Path(str(resolved_source["env_path"])))
        != source_snapshot.get("env_sha256")
        or _sha256(Path(str(resolved_source["state_path"])))
        != source_snapshot.get("state_sha256")
    ):
        raise LastGoodError("SOURCE_POINTER_RESTORE_SNAPSHOT_MISMATCH")
    current = resolve_last_good(backup_path)
    if current.get("revision") == source_revision:
        if (
            current.get("snapshot_id") != snapshot_id
            or _sha256(Path(str(current["env_path"])))
            != source_snapshot.get("env_sha256")
            or _sha256(Path(str(current["state_path"])))
            != source_snapshot.get("state_sha256")
        ):
            raise LastGoodError("SOURCE_POINTER_RESTORE_SOURCE_MISMATCH")
        return current
    if current.get("revision") != target_revision:
        raise LastGoodError("SOURCE_POINTER_RESTORE_CURRENT_REVISION_INVALID")
    if (
        _sha256(Path(str(current["env_path"]))) != _sha256(target_env_path)
        or _sha256(Path(str(current["state_path"])))
        != _sha256(verified_state_path)
    ):
        raise LastGoodError("SOURCE_POINTER_RESTORE_TARGET_MISMATCH")
    if validate_only:
        return {
            **resolved_source,
            "revision": source_revision,
            "snapshot_id": snapshot_id,
        }
    pointer = {
        "created_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "manifest_sha256": manifest_sha256,
        "revision": source_revision,
        "snapshot_id": snapshot_id,
    }
    _write_private_json_atomic(backup_path / "last-good-pointer.json", pointer)
    _fsync_directory(backup_path)
    resolved = resolve_last_good(backup_path)
    if resolved.get("revision") != source_revision:
        raise LastGoodError("SOURCE_POINTER_RESTORE_PUBLISH_MISMATCH")
    return resolved


def _require_target_pointer_content(
    backup_path: Path,
    target_env_path: Path,
    verified_state_path: Path,
    target_revision: str,
) -> dict[str, object]:
    """确认权威 pointer 的 revision 和两个文件均精确等于 target。"""
    resolved = resolve_last_good(backup_path)
    if resolved.get("revision") != target_revision:
        raise LastGoodError("LAST_GOOD_TARGET_REVISION_MISMATCH")
    env_path = Path(str(resolved.get("env_path", "")))
    state_path = Path(str(resolved.get("state_path", "")))
    if (
        _sha256(env_path) != _sha256(target_env_path)
        or _sha256(state_path) != _sha256(verified_state_path)
    ):
        raise LastGoodError("LAST_GOOD_TARGET_CONTENT_MISMATCH")
    return resolved


def _current_pointer_evidence(backup_path: Path) -> dict[str, object]:
    pointer_path = backup_path / "last-good-pointer.json"
    if not _exists_or_symlink(pointer_path):
        return {"state": "absent"}
    value = _pointer_evidence(backup_path)
    return {
        "pointer": value["pointer"],
        "pointer_sha256": value["pointer_sha256"],
        "revision": value["revision"],
        "state": "pointer",
    }


def _pointer_evidence(backup_path: Path) -> dict[str, object]:
    resolved = resolve_last_good(backup_path)
    pointer_path = backup_path / "last-good-pointer.json"
    return {
        "pointer": _load_object(pointer_path, "pointer"),
        "pointer_sha256": _sha256(pointer_path),
        "revision": resolved["revision"],
        "schema_version": "1",
        "snapshot": _snapshot_evidence(
            backup_path,
            str(resolved["snapshot_id"]),
        ),
        "state": "pointer",
    }


def _snapshot_evidence(
    backup_path: Path,
    snapshot_id: str,
) -> dict[str, object]:
    snapshot = backup_path / "last-good-snapshots" / snapshot_id
    manifest = snapshot / "SNAPSHOT_MANIFEST.json"
    env_path = snapshot / "rag-industry.env"
    state_path = snapshot / "state.json"
    for path, label in (
        (manifest, "snapshot manifest"),
        (env_path, "snapshot env"),
        (state_path, "snapshot state"),
    ):
        _require_private_file(path, label)
    return {
        "env_sha256": _sha256(env_path),
        "manifest_sha256": _sha256(manifest),
        "snapshot_id": snapshot_id,
        "state_sha256": _sha256(state_path),
    }


def _trusted_last_good_revisions(
    manifest_path: Path,
    source_revision: str,
) -> set[str]:
    _require_private_file(manifest_path, "update manifest")
    manifest = _load_object(manifest_path, "update manifest")
    compatibility = manifest.get("source_compatibility")
    values = (
        compatibility.get("trusted_last_good_revisions")
        if isinstance(compatibility, dict)
        else None
    )
    if (
        not isinstance(values, list)
        or not values
        or source_revision not in values
        or len(set(values)) != len(values)
        or any(
            not isinstance(value, str)
            or _REVISION.fullmatch(value) is None
            for value in values
        )
    ):
        raise LastGoodError("TRUSTED_LAST_GOOD_REVISIONS_INVALID")
    return set(values)


def _evidence_identity(path: Path) -> dict[str, object]:
    value = path.stat()
    return {
        "bytes": value.st_size,
        "gid": value.st_gid,
        "mode": f"{stat.S_IMODE(value.st_mode):04o}",
        "sha256": _sha256(path),
        "uid": value.st_uid,
    }


def _exists_or_symlink(path: Path) -> bool:
    return path.exists() or path.is_symlink()


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
    if stat.S_IMODE(path.stat().st_mode) != _PRIVATE_MODE:
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
    inspect = commands.add_parser("inspect")
    inspect.add_argument("backup_path", type=Path)
    checkpoint = commands.add_parser("checkpoint-source")
    checkpoint.add_argument("backup_path", type=Path)
    checkpoint.add_argument("env_path", type=Path)
    checkpoint.add_argument("state_path", type=Path)
    checkpoint.add_argument("revision")
    checkpoint.add_argument("inspection_path", type=Path)
    checkpoint.add_argument("update_manifest_path", type=Path)
    finalize = commands.add_parser("finalize-target")
    finalize.add_argument("backup_path", type=Path)
    finalize.add_argument("target_env_path", type=Path)
    finalize.add_argument("verified_state_path", type=Path)
    finalize.add_argument("target_revision")
    finalize.add_argument("inspection_path", type=Path)
    finalize.add_argument("source_state_path", type=Path)
    finalize.add_argument("source_checkpoint_path", type=Path)
    restore = commands.add_parser("restore-source-pointer")
    restore.add_argument("backup_path", type=Path)
    restore.add_argument("source_checkpoint_path", type=Path)
    restore.add_argument("source_state_path", type=Path)
    restore.add_argument("source_env_path", type=Path)
    restore.add_argument("update_manifest_path", type=Path)
    restore.add_argument("target_env_path", type=Path)
    restore.add_argument("verified_state_path", type=Path)
    restore.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    """执行 last-good 检查、封存、晋升、解析或旧格式迁移。

    Args:
        无参数；命令行选项从当前进程读取。

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
        elif arguments.command == "migrate":
            value = migrate_legacy_last_good(arguments.backup_path)
        elif arguments.command == "inspect":
            value = inspect_last_good(arguments.backup_path)
        elif arguments.command == "checkpoint-source":
            value = checkpoint_source_last_good(
                arguments.backup_path,
                arguments.env_path,
                arguments.state_path,
                arguments.revision,
                arguments.inspection_path,
                arguments.update_manifest_path,
            )
        elif arguments.command == "finalize-target":
            value = finalize_target_last_good(
                arguments.backup_path,
                arguments.target_env_path,
                arguments.verified_state_path,
                arguments.target_revision,
                arguments.inspection_path,
                arguments.source_state_path,
                arguments.source_checkpoint_path,
            )
        else:
            value = restore_source_pointer(
                arguments.backup_path,
                arguments.source_checkpoint_path,
                arguments.source_state_path,
                arguments.source_env_path,
                arguments.update_manifest_path,
                arguments.target_env_path,
                arguments.verified_state_path,
                validate_only=arguments.validate_only,
            )
    except LastGoodError as error:
        print(f"RAG_INDUSTRY_LAST_GOOD_FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
