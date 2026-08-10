#!/usr/bin/env python3
"""离线验证并安全解包 Industry serving app update。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import IO

PACKAGE_FILES = frozenset(
    {
        "SERVER_UPDATE_COMMANDS.txt",
        "UPDATE_MANIFEST.json",
        "app-image.tar.gz",
        "app-image.tar.gz.sha256",
        "package_selfcheck.py",
        "serving-runtime.tar.gz",
        "serving-runtime.tar.gz.sha256",
        "update-app.sh",
    }
)
_DIRECTORY_MODE = 0o755
_UI_SESSION_TTL_SECONDS = 1800
_TRACE_QUESTION_RETENTION_SECONDS = 604800
_TRACE_SCHEMA_VERSION = 2
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_RUNTIME_ROOT = re.compile(r"serving-runtime/[0-9a-f]{12}")
_RUNTIME_FILES = {
    "RUNTIME_MANIFEST.json",
    "compose_check.py",
    "compose.yaml",
    "config/corpus-policy.json",
    "config/intent-router-calibration.json",
    "config/intent-router.json",
    "config/pipeline.json",
    "config/retrieval.json",
    "last_good.py",
    "lib.sh",
    "rollback-app-update.sh",
    "runtime_check.py",
    "ui_contract_check.py",
    "validation/expected-corpus.json",
    "validation/industry-smoke.jsonl",
    "validation_check.py",
    "verify-app-update.sh",
}
_TOP_HASHED_FILES = PACKAGE_FILES - {"UPDATE_MANIFEST.json"}


class PackageSelfcheckError(RuntimeError):
    """表示更新包 exact set、身份或归档安全合同失败。"""


def verify_package(package_root: Path) -> dict[str, object]:
    """离线验证顶层包、sidecar、manifest 与 runtime archive。

    Args:
        package_root: fresh extraction 后的更新包目录。

    Returns:
        不含路径、secret 或正文的包身份摘要。

    Raises:
        PackageSelfcheckError: 任一 exact set、SHA 或安全合同失败。

    """
    root = package_root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise PackageSelfcheckError("PACKAGE_ROOT_INVALID")
    entries = list(root.iterdir())
    if {path.name for path in entries} != PACKAGE_FILES:
        raise PackageSelfcheckError("PACKAGE_EXACT_SET_INVALID")
    if any(not path.is_file() or path.is_symlink() for path in entries):
        raise PackageSelfcheckError("PACKAGE_FILE_TYPE_INVALID")
    manifest = _load_object(root / "UPDATE_MANIFEST.json", "update manifest")
    _validate_update_manifest(manifest)
    files = _string_mapping(manifest, "files")
    if set(files) != _TOP_HASHED_FILES:
        raise PackageSelfcheckError("PACKAGE_FILE_MANIFEST_INVALID")
    for name, expected in files.items():
        if _file_sha256(root / name) != expected:
            raise PackageSelfcheckError("PACKAGE_FILE_SHA256_MISMATCH")
    _verify_sidecar(root, "app-image.tar.gz")
    _verify_sidecar(root, "serving-runtime.tar.gz")
    runtime = _object(manifest, "runtime")
    archive_sha = _required_sha256(runtime, "archive_sha256")
    if archive_sha != _file_sha256(root / "serving-runtime.tar.gz"):
        raise PackageSelfcheckError("RUNTIME_ARCHIVE_SHA256_MISMATCH")
    runtime_summary = verify_runtime_archive(
        root / "serving-runtime.tar.gz", manifest
    )
    if runtime_summary["canonical_digest"] != runtime["canonical_digest"]:
        raise PackageSelfcheckError("RUNTIME_CANONICAL_DIGEST_MISMATCH")
    return {
        "index_fingerprint": _object(manifest, "index_fingerprint")["target"],
        "package_contract_revision": manifest["package_contract_revision"],
        "revision": manifest["revision"],
        "runtime": runtime_summary,
        "schema_version": manifest["schema_version"],
    }


def verify_runtime_archive(  # noqa: PLR0912
    archive_path: Path,
    update_manifest: dict[str, object],
) -> dict[str, object]:
    """验证确定性 runtime tar 的成员类型、exact set、mode 与 SHA。

    Args:
        archive_path: gzip 压缩的 runtime tar。
        update_manifest: 已校验的顶层 update manifest。

    Returns:
        runtime 根目录、文件数和 canonical digest。

    Raises:
        PackageSelfcheckError: 归档含危险成员或内容不匹配。

    """
    runtime = _object(update_manifest, "runtime")
    root_name = _required_string(runtime, "root")
    if _RUNTIME_ROOT.fullmatch(root_name) is None:
        raise PackageSelfcheckError("RUNTIME_ROOT_INVALID")
    expected_files = _string_mapping(runtime, "files")
    if not expected_files:
        raise PackageSelfcheckError("RUNTIME_FILES_INVALID")
    expected_paths = {f"{root_name}/{name}" for name in expected_files}
    expected_directories = _parent_directories(expected_paths, root_name)
    seen: set[str] = set()
    file_digests: dict[str, str] = {}
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                _validate_member_name(name, root_name)
                if name in seen:
                    raise PackageSelfcheckError("RUNTIME_DUPLICATE_MEMBER")
                seen.add(name)
                mode = stat.S_IMODE(member.mode)
                if (
                    member.uid != 0
                    or member.gid != 0
                    or member.uname
                    or member.gname
                    or member.mtime != runtime["source_date_epoch"]
                ):
                    raise PackageSelfcheckError(
                        "RUNTIME_MEMBER_IDENTITY_INVALID"
                    )
                if mode & (stat.S_ISUID | stat.S_ISGID | 0o022):
                    raise PackageSelfcheckError("RUNTIME_MEMBER_MODE_INVALID")
                if member.isdir():
                    if (
                        name not in expected_directories
                        or mode != _DIRECTORY_MODE
                    ):
                        raise PackageSelfcheckError("RUNTIME_DIRECTORY_INVALID")
                    continue
                if not member.isfile() or name not in expected_paths:
                    raise PackageSelfcheckError("RUNTIME_MEMBER_TYPE_INVALID")
                relative = name.removeprefix(f"{root_name}/")
                expected_mode = _runtime_file_mode(relative)
                if mode != expected_mode:
                    raise PackageSelfcheckError("RUNTIME_FILE_MODE_INVALID")
                stream = archive.extractfile(member)
                if stream is None:
                    raise PackageSelfcheckError("RUNTIME_FILE_MISSING")
                file_digests[relative] = _stream_sha256(stream)
    except (OSError, tarfile.TarError) as error:
        raise PackageSelfcheckError("RUNTIME_ARCHIVE_INVALID") from error
    if seen != expected_paths | expected_directories:
        raise PackageSelfcheckError("RUNTIME_ARCHIVE_EXACT_SET_INVALID")
    if file_digests != expected_files:
        raise PackageSelfcheckError("RUNTIME_FILE_SHA256_MISMATCH")
    runtime_manifest_bytes = _read_archive_member(
        archive_path, f"{root_name}/RUNTIME_MANIFEST.json"
    )
    runtime_manifest = _json_object(runtime_manifest_bytes, "runtime manifest")
    _validate_runtime_manifest(runtime_manifest, root_name, expected_files)
    canonical = json.dumps(
        file_digests, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "canonical_digest": hashlib.sha256(canonical).hexdigest(),
        "file_count": len(file_digests),
        "root": root_name,
    }


def safe_extract_runtime(
    package_root: Path,
    destination: Path,
) -> Path:
    """在完整 selfcheck 后逐成员创建 runtime，不调用 extractall。

    Args:
        package_root: 更新包目录。
        destination: 必须为空或不存在的临时解包目录。

    Returns:
        解包后的版本化 runtime 根目录。

    Raises:
        PackageSelfcheckError: 目标非空、成员漂移或写入失败。

    """
    verify_package(package_root)
    manifest = _load_object(
        package_root / "UPDATE_MANIFEST.json", "update manifest"
    )
    runtime = _object(manifest, "runtime")
    root_name = _required_string(runtime, "root")
    expected_files = _string_mapping(runtime, "files")
    if destination.exists():
        if destination.is_symlink() or any(destination.iterdir()):
            raise PackageSelfcheckError("EXTRACTION_DESTINATION_INVALID")
    else:
        destination.mkdir(parents=True, mode=0o755)
    archive_path = package_root / "serving-runtime.tar.gz"
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = sorted(archive.getmembers(), key=lambda item: item.name)
        for member in members:
            name = member.name.rstrip("/")
            _validate_member_name(name, root_name)
            target = destination.joinpath(*PurePosixPath(name).parts)
            resolved_parent = target.parent.resolve()
            if destination.resolve() not in (
                resolved_parent,
                *resolved_parent.parents,
            ):
                raise PackageSelfcheckError("RUNTIME_PATH_TRAVERSAL")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                target.chmod(0o755)
                continue
            relative = name.removeprefix(f"{root_name}/")
            if relative not in expected_files or not member.isfile():
                raise PackageSelfcheckError("RUNTIME_MEMBER_TYPE_INVALID")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if target.exists() or target.is_symlink():
                raise PackageSelfcheckError("RUNTIME_EXTRACTION_COLLISION")
            source = archive.extractfile(member)
            if source is None:
                raise PackageSelfcheckError("RUNTIME_FILE_MISSING")
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _runtime_file_mode(relative),
            )
            with source, os.fdopen(descriptor, "wb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            target.chmod(_runtime_file_mode(relative))
    extracted_root = destination.joinpath(*PurePosixPath(root_name).parts)
    for relative, expected in expected_files.items():
        if _file_sha256(extracted_root / relative) != expected:
            raise PackageSelfcheckError("EXTRACTED_RUNTIME_SHA256_MISMATCH")
    return extracted_root


def _validate_update_manifest(  # noqa: PLR0912, PLR0915
    value: dict[str, object],
) -> None:
    required = {
        "branch",
        "config_files",
        "files",
        "image",
        "index_fingerprint",
        "package_contract_revision",
        "revision",
        "runtime",
        "schema_version",
        "serving_fingerprint",
        "source_compatibility",
        "target",
        "trace",
        "ui",
    }
    if set(value) != required:
        raise PackageSelfcheckError("UPDATE_MANIFEST_FIELDS_INVALID")
    if (
        value.get("schema_version") != "2"
        or value.get("package_contract_revision")
        != "industry-serving-update-v2"
        or value.get("branch") != "Industry"
    ):
        raise PackageSelfcheckError("UPDATE_MANIFEST_CONTRACT_INVALID")
    revision = value.get("revision")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise PackageSelfcheckError("UPDATE_REVISION_INVALID")
    index = _object(value, "index_fingerprint")
    if index.get("reindex_required") is not False:
        raise PackageSelfcheckError("REINDEX_REQUIRED_INVALID")
    _required_prefixed_sha256(index, "source")
    _required_prefixed_sha256(index, "target")
    if index["source"] != index["target"]:
        raise PackageSelfcheckError("INDEX_FINGERPRINT_CHANGED")
    serving = _object(value, "serving_fingerprint")
    if set(serving) != {"source", "target"}:
        raise PackageSelfcheckError("SERVING_FINGERPRINT_FIELDS_INVALID")
    _required_prefixed_sha256(serving, "source")
    _required_prefixed_sha256(serving, "target")
    if serving["source"] == serving["target"]:
        raise PackageSelfcheckError("SERVING_FINGERPRINT_UNCHANGED")
    ui = _object(value, "ui")
    if (
        set(ui)
        != {
            "allow_insecure_http",
            "cookie_secure",
            "query_auth_mode",
            "session_ttl_seconds",
        }
        or ui.get("allow_insecure_http") is not True
        or ui.get("cookie_secure") is not False
        or ui.get("query_auth_mode") != "same_origin_session"
        or not isinstance(ui.get("session_ttl_seconds"), int)
        or isinstance(ui.get("session_ttl_seconds"), bool)
        or ui.get("session_ttl_seconds") != _UI_SESSION_TTL_SECONDS
    ):
        raise PackageSelfcheckError("UI_CONTRACT_INVALID")
    trace = _object(value, "trace")
    if (
        set(trace)
        != {
            "question_capture",
            "question_retention_seconds",
            "schema_version",
        }
        or trace.get("question_capture") != "plaintext"
        or not isinstance(trace.get("question_retention_seconds"), int)
        or isinstance(trace.get("question_retention_seconds"), bool)
        or trace.get("question_retention_seconds")
        != _TRACE_QUESTION_RETENTION_SECONDS
        or not isinstance(trace.get("schema_version"), int)
        or isinstance(trace.get("schema_version"), bool)
        or trace.get("schema_version") != _TRACE_SCHEMA_VERSION
    ):
        raise PackageSelfcheckError("TRACE_CONTRACT_INVALID")
    config_files = _string_mapping(value, "config_files")
    if set(config_files) != {
        "corpus-policy.json",
        "intent-router-calibration.json",
        "intent-router.json",
        "pipeline.json",
        "retrieval.json",
    }:
        raise PackageSelfcheckError("CONFIG_EXACT_SET_INVALID")
    source = _object(value, "source_compatibility")
    if source != {
        "compatible_revisions": ["2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"],
        "old_app_runtime_state_required": False,
        "required_index_fingerprint": index["source"],
        "trace_v2_read_compatible": True,
    }:
        raise PackageSelfcheckError("SOURCE_COMPATIBILITY_INVALID")
    target = _object(value, "target")
    if target != {
        "alias": "rag-industry-active",
        "project": "rag-industry",
        "service": "rag-industry-app",
    }:
        raise PackageSelfcheckError("TARGET_IDENTITY_INVALID")
    image = _object(value, "image")
    if set(image) != {
        "archive_sha256",
        "config_digest",
        "id",
        "manifest_digest",
        "platform",
        "ref",
        "revision",
    }:
        raise PackageSelfcheckError("IMAGE_FIELDS_INVALID")
    archive_sha = _required_sha256(image, "archive_sha256")
    if archive_sha != _string_mapping(value, "files").get("app-image.tar.gz"):
        raise PackageSelfcheckError("IMAGE_ARCHIVE_SHA256_MISMATCH")
    for key in ("config_digest", "id", "manifest_digest"):
        _required_prefixed_sha256(image, key)
    if (
        image.get("platform") != "linux/amd64"
        or image.get("revision") != revision
        or image.get("ref") != f"docx-rag:{revision[:12]}"
    ):
        raise PackageSelfcheckError("IMAGE_IDENTITY_INVALID")
    runtime = _object(value, "runtime")
    if set(runtime) != {
        "archive_sha256",
        "canonical_digest",
        "files",
        "root",
        "source_date_epoch",
    }:
        raise PackageSelfcheckError("RUNTIME_FIELDS_INVALID")
    _required_sha256(runtime, "archive_sha256")
    _required_sha256(runtime, "canonical_digest")
    source_date_epoch = runtime.get("source_date_epoch")
    if (
        not isinstance(source_date_epoch, int)
        or isinstance(source_date_epoch, bool)
        or source_date_epoch <= 0
    ):
        raise PackageSelfcheckError("RUNTIME_SOURCE_DATE_EPOCH_INVALID")
    runtime_files = _string_mapping(runtime, "files")
    if set(runtime_files) != _RUNTIME_FILES:
        raise PackageSelfcheckError("RUNTIME_FILES_EXACT_SET_INVALID")
    if runtime.get("root") != f"serving-runtime/{revision[:12]}":
        raise PackageSelfcheckError("RUNTIME_REVISION_ROOT_INVALID")
    for name, digest in config_files.items():
        if runtime_files.get(f"config/{name}") != digest:
            raise PackageSelfcheckError("CONFIG_RUNTIME_SHA256_MISMATCH")


def _validate_runtime_manifest(
    value: dict[str, object],
    root_name: str,
    update_files: dict[str, str],
) -> None:
    if set(value) != {"files", "revision", "root", "schema_version"}:
        raise PackageSelfcheckError("RUNTIME_MANIFEST_FIELDS_INVALID")
    if value.get("schema_version") != "1" or value.get("root") != root_name:
        raise PackageSelfcheckError("RUNTIME_MANIFEST_INVALID")
    files = value.get("files")
    if not isinstance(files, dict):
        raise PackageSelfcheckError("RUNTIME_MANIFEST_FILES_INVALID")
    expected = {
        name: digest
        for name, digest in update_files.items()
        if name != "RUNTIME_MANIFEST.json"
    }
    parsed: dict[str, str] = {}
    for name, identity in files.items():
        if not isinstance(name, str) or not isinstance(identity, dict):
            raise PackageSelfcheckError("RUNTIME_MANIFEST_FILES_INVALID")
        if set(identity) != {"mode", "sha256"}:
            raise PackageSelfcheckError("RUNTIME_MANIFEST_FILES_INVALID")
        digest = identity.get("sha256")
        mode = identity.get("mode")
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or mode != f"{_runtime_file_mode(name):04o}"
        ):
            raise PackageSelfcheckError("RUNTIME_MANIFEST_FILES_INVALID")
        parsed[name] = digest
    if parsed != expected:
        raise PackageSelfcheckError("RUNTIME_MANIFEST_EXACT_SET_INVALID")


def _validate_member_name(name: str, root_name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or "\\" in name
        or "\x00" in name
        or not (
            name in {"serving-runtime", root_name}
            or name.startswith(f"{root_name}/")
        )
    ):
        raise PackageSelfcheckError("RUNTIME_MEMBER_PATH_INVALID")


def _parent_directories(paths: set[str], root_name: str) -> set[str]:
    directories = {root_name, "serving-runtime"}
    for name in paths:
        parent = PurePosixPath(name).parent
        while str(parent) not in {".", ""}:
            directories.add(str(parent))
            parent = parent.parent
    return directories


def _runtime_file_mode(relative: str) -> int:
    if relative.endswith(".sh") or relative in {
        "compose_check.py",
        "runtime_check.py",
        "ui_contract_check.py",
        "validation_check.py",
        "last_good.py",
    }:
        return 0o755
    return 0o644


def _verify_sidecar(root: Path, archive_name: str) -> None:
    sidecar = root / f"{archive_name}.sha256"
    expected = f"{_file_sha256(root / archive_name)}  {archive_name}\n"
    if sidecar.read_text(encoding="ascii") != expected:
        raise PackageSelfcheckError("ARCHIVE_SIDECAR_INVALID")


def _read_archive_member(path: Path, name: str) -> bytes:
    with tarfile.open(path, mode="r:gz") as archive:
        try:
            member = archive.getmember(name)
        except KeyError as error:
            raise PackageSelfcheckError("RUNTIME_MANIFEST_MISSING") from error
        stream = archive.extractfile(member)
        if stream is None:
            raise PackageSelfcheckError("RUNTIME_MANIFEST_MISSING")
        return stream.read()


def _load_object(path: Path, label: str) -> dict[str, object]:
    return _json_object(path.read_bytes(), label)


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageSelfcheckError(f"{label} JSON invalid") from error
    if not isinstance(value, dict):
        raise PackageSelfcheckError(f"{label} must be object")
    return value


def _object(value: dict[str, object], key: str) -> dict[str, object]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise PackageSelfcheckError(f"{key.upper()}_INVALID")
    return item


def _string_mapping(value: dict[str, object], key: str) -> dict[str, str]:
    item = _object(value, key)
    result: dict[str, str] = {}
    for name, digest in item.items():
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise PackageSelfcheckError(f"{key.upper()}_INVALID")
        result[name] = digest
    return result


def _required_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise PackageSelfcheckError(f"{key.upper()}_INVALID")
    return item


def _required_sha256(value: dict[str, object], key: str) -> str:
    item = _required_string(value, key)
    if _SHA256.fullmatch(item) is None:
        raise PackageSelfcheckError(f"{key.upper()}_INVALID")
    return item


def _required_prefixed_sha256(value: dict[str, object], key: str) -> str:
    item = _required_string(value, key)
    if not item.startswith("sha256:") or _SHA256.fullmatch(item[7:]) is None:
        raise PackageSelfcheckError(f"{key.upper()}_INVALID")
    return item


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stream_sha256(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument(
        "package_root", type=Path, nargs="?", default=Path.cwd()
    )
    extract = commands.add_parser("extract")
    extract.add_argument("package_root", type=Path)
    extract.add_argument("destination", type=Path)
    return parser.parse_args()


def main() -> int:
    """执行无网络 package selfcheck 或 safe extraction。

    Returns:
        成功返回 0；合同失败返回 1 与稳定错误码。

    """
    arguments = _arguments()
    try:
        if arguments.command == "verify":
            result = verify_package(arguments.package_root)
        else:
            extracted = safe_extract_runtime(
                arguments.package_root, arguments.destination
            )
            result = {"runtime_root": str(extracted)}
    except (OSError, PackageSelfcheckError) as error:
        print(
            f"RAG_INDUSTRY_PACKAGE_SELFCHECK_FAILED: {error}", file=sys.stderr
        )
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
