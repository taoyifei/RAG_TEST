"""在无网络条件下校验 Industry release 与 corpus 归档。"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

_SHA_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_EXECUTABLE_MODE = 0o700
_PRIVATE_DIRECTORY_MODE = 0o700
_UNSAFE_MODE_MASK = (
    stat.S_ISUID
    | stat.S_ISGID
    | stat.S_ISVTX
    | stat.S_IWGRP
    | stat.S_IWOTH
)
_EXECUTE_MODE_MASK = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_HASH_BLOCK_BYTES = 1024 * 1024
_EXPECTED_ACTIVE_COUNT = 10
_CORPUS_METADATA_FIELDS = {
    "authority_level",
    "document_status",
    "effective_from",
    "effective_to",
}
_FULL_RELEASE_KIND = "industry-first-deploy"
_REUSE_RELEASE_KIND = "industry-first-deploy-reuse-images"
_CONTRACT_IDENTITIES: dict[str, tuple[str, set[str]]] = {
    _FULL_RELEASE_KIND: (
        "industry-package-v1",
        {
            "app-image.tar.gz",
            "corpus.tar.gz",
            "ocr-image.tar.gz",
            "qdrant-image.tar.gz",
        },
    ),
    _REUSE_RELEASE_KIND: (
        "industry-package-reuse-images-v1",
        {"app-image.tar.gz", "corpus.tar.gz"},
    ),
}
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(rb"Authorization\s*:\s*Bearer\s+\S+", re.IGNORECASE),
    re.compile(
        rb"(?:api[_-]?key|token|password)\s*[=:]\s*"
        rb"[A-Za-z0-9+/=_-]{32,}",
        re.IGNORECASE,
    ),
    re.compile(rb"C:\\Users\\", re.IGNORECASE),
    re.compile(rb"\\\\wsl\.localhost\\", re.IGNORECASE),
    re.compile(rb"/home/[A-Za-z0-9._-]+/"),
)
_FORBIDDEN_NAMES = {
    ".bash_history",
    ".docker",
    ".git",
    ".git-credentials",
    ".netrc",
    ".ssh",
    ".venv",
    ".zsh_history",
    "__pycache__",
    ".env",
    "config.json",
    "known_hosts",
    "id_rsa",
    "id_ed25519",
}
_PACKAGE_CONTRACT_FIELDS = {
    "active_document_count",
    "compose_project",
    "git_branch",
    "package_contract_revision",
    "reference_document_count",
    "release_kind",
    "release_manifest_payload_excludes",
    "required_archives",
    "required_config",
    "required_files",
    "required_validation",
    "schema_version",
    "sha256sums_excludes",
}


class PackageSelfcheckError(ValueError):
    """表示 Industry release 文件集合或安全边界无效。"""


def verify_release(
    root: Path,
    *,
    enforce_modes: bool = True,
) -> dict[str, object]:
    """验证 release exact set、摘要、manifest 与秘密扫描。

    Args:
        root: 已解包的 Industry release 根目录。
        enforce_modes: 是否要求文件权限已恢复为 manifest 声明值。

    Returns:
        不含路径和业务内容的校验计数。

    Raises:
        PackageSelfcheckError: 文件、摘要、manifest 或安全扫描失败。

    """
    resolved = _require_real_directory(root)
    files = _release_files(resolved)
    contract = _load_object(resolved / "package-contract.json")
    _verify_package_contract(files, contract)
    sums = _load_sha256sums(resolved / "SHA256SUMS")
    expected_sum_paths = set(files) - {PurePosixPath("SHA256SUMS")}
    if set(sums) != expected_sum_paths:
        raise PackageSelfcheckError("SHA256SUMS 与 release exact set 不一致。")
    for relative, digest in sums.items():
        if _sha256(resolved.joinpath(*relative.parts)) != digest:
            raise PackageSelfcheckError(f"release SHA256 不一致：{relative}")
    manifest = _load_object(resolved / "RELEASE_MANIFEST.json")
    _verify_release_manifest(
        resolved,
        files,
        manifest,
        contract=contract,
        enforce_modes=enforce_modes,
    )
    _verify_archive_sidecars(resolved, contract["required_archives"])
    _scan_release_safety(resolved, files)
    return {
        "file_count": len(files),
        "payload_count": len(manifest["payload_files"]),
        "release_kind": manifest["release_kind"],
        "schema_version": manifest["schema_version"],
    }


def _verify_package_contract(
    files: set[PurePosixPath],
    contract: dict[str, Any],
) -> None:
    required_files = contract.get("required_files")
    release_kind = contract.get("release_kind")
    identity = (
        _CONTRACT_IDENTITIES.get(release_kind)
        if isinstance(release_kind, str)
        else None
    )
    if (
        set(contract) != _PACKAGE_CONTRACT_FIELDS
        or contract.get("schema_version") != "1"
        or identity is None
        or contract.get("package_contract_revision") != identity[0]
        or contract.get("git_branch") != "Industry"
        or contract.get("compose_project") != "rag-industry"
        or contract.get("active_document_count") != _EXPECTED_ACTIVE_COUNT
        or contract.get("reference_document_count") != 0
        or contract.get("sha256sums_excludes") != ["SHA256SUMS"]
        or contract.get("release_manifest_payload_excludes")
        != ["RELEASE_MANIFEST.json", "SHA256SUMS"]
        or not isinstance(required_files, list)
        or any(not isinstance(item, str) for item in required_files)
    ):
        raise PackageSelfcheckError("package contract schema 无效。")
    expected = {_safe_relative(item) for item in required_files}
    if len(expected) != len(required_files) or files != expected:
        raise PackageSelfcheckError(
            "release 与 package contract exact set 不一致。"
        )
    expected_groups = {
        "required_archives": identity[1],
        "required_config": {
            "config/corpus-policy.json",
            "config/intent-router-calibration.json",
            "config/intent-router.json",
            "config/pipeline.json",
            "config/retrieval.json",
        },
        "required_validation": {
            "validation/expected-corpus.json",
            "validation/industry-smoke.jsonl",
        },
    }
    for field, values in expected_groups.items():
        actual = contract.get(field)
        if (
            not isinstance(actual, list)
            or any(not isinstance(item, str) for item in actual)
            or set(actual) != values
        ):
            raise PackageSelfcheckError(
                "package contract required group 无效。"
            )


def extract_corpus(
    archive: Path,
    sidecar: Path,
    destination: Path,
) -> Path:
    """安全解包并验证 corpus exact set 与公开 manifest。

    Args:
        archive: `corpus.tar.gz`。
        sidecar: 同目录 basename sidecar。
        destination: 尚不存在的最终 corpus 目录。

    Returns:
        已原子发布的 corpus 根目录。

    Raises:
        FileExistsError: 最终目录已存在。
        PackageSelfcheckError: 摘要、成员或 corpus manifest 无效。

    """
    _verify_sidecar(archive, sidecar)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("corpus 解包目标已存在。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=destination.parent,
        prefix=".industry-corpus-extract-",
    ) as temporary_name:
        temporary = Path(temporary_name)
        with tarfile.open(archive, mode="r:gz") as bundle:
            members = bundle.getmembers()
            member_modes = _validate_corpus_members(members)
            _extract_members(bundle, members, temporary)
        _verify_extracted_corpus(temporary)
        _restore_modes(temporary, member_modes)
        _publish_directory(temporary, destination)
    return destination


def verify_outer_archive(
    archive: Path,
    sidecar: Path,
    destination: Path,
) -> Path:
    """安全解包外层唯一 release 归档并执行完整 selfcheck。

    Args:
        archive: 最终上传 tar.gz。
        sidecar: 与归档同名的 SHA256 sidecar。
        destination: 尚不存在的解包父目录。

    Returns:
        已验证的唯一 release 根目录。

    Raises:
        PackageSelfcheckError: sidecar、tar 或 release 无效。

    """
    _verify_sidecar(archive, sidecar)
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        top_levels = {
            _safe_relative(member.name).parts[0] for member in members
        }
        if len(top_levels) != 1:
            raise PackageSelfcheckError("外层归档必须只有一个顶层目录。")
        member_modes = _validate_generic_members(members)
        _extract_members(bundle, members, destination)
    release = destination / next(iter(top_levels))
    verify_release(release, enforce_modes=False)
    _restore_modes(destination, member_modes)
    verify_release(release)
    return release


def publish_directory(source: Path, destination: Path) -> None:
    """以 Linux no-replace rename 原子发布目录。

    Args:
        source: 与目标位于同一真实父目录的 staging 目录。
        destination: 尚不存在的最终目录。

    Returns:
        无返回值。

    Raises:
        FileExistsError: 目标已存在。
        PackageSelfcheckError: 源或父目录边界无效。

    """
    if not source.is_dir() or source.is_symlink():
        raise PackageSelfcheckError("原子发布源必须是真实目录。")
    _publish_directory(source, destination)


def _verify_release_manifest(
    root: Path,
    files: set[PurePosixPath],
    manifest: dict[str, Any],
    *,
    contract: dict[str, Any],
    enforce_modes: bool,
) -> None:
    required = {
        "schema_version",
        "release_kind",
        "git_branch",
        "git_sha",
        "dirty",
        "payload_files",
        "images",
        "corpus",
        "config_sha256",
        "compose",
        "package_contract_revision",
        "builder_revision",
    }
    if not required.issubset(manifest):
        raise PackageSelfcheckError("RELEASE_MANIFEST 缺少必要字段。")
    if (
        manifest["schema_version"] != "1"
        or manifest["release_kind"] != contract["release_kind"]
        or manifest["package_contract_revision"]
        != contract["package_contract_revision"]
        or manifest["git_branch"] != "Industry"
        or manifest["dirty"] is not False
        or not isinstance(manifest["git_sha"], str)
        or re.fullmatch(r"[0-9a-f]{40}", manifest["git_sha"]) is None
    ):
        raise PackageSelfcheckError("RELEASE_MANIFEST release 身份无效。")
    expected_payload = files - {
        PurePosixPath("RELEASE_MANIFEST.json"),
        PurePosixPath("SHA256SUMS"),
    }
    payload = manifest["payload_files"]
    if not isinstance(payload, list):
        raise PackageSelfcheckError("RELEASE_MANIFEST payload_files 无效。")
    actual_payload: set[PurePosixPath] = set()
    for item in payload:
        if not isinstance(item, dict) or set(item) != {
            "mode",
            "path",
            "sha256",
            "size",
        }:
            raise PackageSelfcheckError("RELEASE_MANIFEST payload item 无效。")
        relative = _safe_relative(item["path"])
        path = root.joinpath(*relative.parts)
        if (
            relative in actual_payload
            or not isinstance(item["size"], int)
            or not isinstance(item["mode"], int)
            or not isinstance(item["sha256"], str)
            or _SHA256.fullmatch(item["sha256"]) is None
            or path.stat().st_size != item["size"]
            or (
                enforce_modes
                and stat.S_IMODE(path.stat().st_mode) != item["mode"]
            )
            or _sha256(path) != item["sha256"]
        ):
            raise PackageSelfcheckError("RELEASE_MANIFEST payload 身份不一致。")
        actual_payload.add(relative)
    if actual_payload != expected_payload:
        raise PackageSelfcheckError(
            "RELEASE_MANIFEST payload exact set 不一致。"
        )
    _verify_image_identities(root, manifest["images"], manifest["release_kind"])
    _verify_config_identity(root, manifest, contract)


def _verify_config_identity(
    root: Path,
    manifest: dict[str, Any],
    contract: dict[str, Any],
) -> None:
    config_paths = [
        _safe_relative(value) for value in contract["required_config"]
    ]
    actual = {
        relative.name: _sha256(root.joinpath(*relative.parts))
        for relative in config_paths
    }
    if manifest.get("config_sha256") != actual:
        raise PackageSelfcheckError(
            "RELEASE_MANIFEST config SHA256 不一致。"
        )
    pipeline = _load_object(root / "config/pipeline.json")
    policy_sha256 = _corpus_policy_semantic_sha256(
        root / "config/corpus-policy.json"
    )
    if pipeline.get("corpus_policy_sha256") != policy_sha256:
        raise PackageSelfcheckError(
            "pipeline corpus policy SHA256 不一致。"
        )
    _verify_metadata_compatibility(root)


def _verify_metadata_compatibility(root: Path) -> None:
    policy = _load_object(root / "config/corpus-policy.json")
    retrieval = _load_object(root / "config/retrieval.json")
    overrides = policy.get("overrides")
    allowed_statuses = retrieval.get("allowed_statuses")
    allowed_authorities = retrieval.get("allowed_authority_levels")
    if (
        not isinstance(overrides, list)
        or len(overrides) != _EXPECTED_ACTIVE_COUNT
        or not isinstance(allowed_statuses, list)
        or not allowed_statuses
        or not all(isinstance(item, str) for item in allowed_statuses)
        or not isinstance(allowed_authorities, list)
        or not allowed_authorities
        or not all(isinstance(item, str) for item in allowed_authorities)
    ):
        raise PackageSelfcheckError("corpus/retrieval 元数据配置无效。")
    incompatible = [
        item
        for item in overrides
        if not isinstance(item, dict)
        or item.get("document_status") not in allowed_statuses
        or item.get("authority_level") not in allowed_authorities
    ]
    if len(incompatible) == len(overrides):
        raise PackageSelfcheckError(
            "corpus policy 元数据被 retrieval 全量过滤。"
        )
    if incompatible:
        raise PackageSelfcheckError(
            "corpus policy 元数据被 retrieval 部分过滤。"
        )


def _corpus_policy_semantic_sha256(path: Path) -> str:
    policy = _load_object(path)
    defaults = policy.get("defaults")
    overrides = policy.get("overrides")
    if (
        set(policy) != {"defaults", "overrides", "schema_version"}
        or policy.get("schema_version") != "1"
        or not isinstance(defaults, dict)
        or set(defaults) != _CORPUS_METADATA_FIELDS
        or not isinstance(overrides, list)
        or any(
            not isinstance(item, dict)
            or set(item) != _CORPUS_METADATA_FIELDS | {"path"}
            or not isinstance(item.get("path"), str)
            for item in overrides
        )
    ):
        raise PackageSelfcheckError("corpus policy schema 无效。")
    semantic = {
        "defaults": defaults,
        "overrides": sorted(overrides, key=lambda item: item["path"]),
        "schema_version": "1",
    }
    canonical = json.dumps(
        semantic,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_image_identities(
    root: Path,
    images: object,
    release_kind: str,
) -> None:
    if not isinstance(images, dict) or set(images) != {"app", "ocr", "qdrant"}:
        raise PackageSelfcheckError("RELEASE_MANIFEST images 无效。")
    expected_delivery = {
        "app": "archive",
        "ocr": (
            "server-existing"
            if release_kind == _REUSE_RELEASE_KIND
            else "archive"
        ),
        "qdrant": (
            "server-existing"
            if release_kind == _REUSE_RELEASE_KIND
            else "archive"
        ),
    }
    archive_fields = {
        "archive_name",
        "archive_sha256",
        "config_digest",
        "delivery",
        "id",
        "manifest_digest",
        "name",
        "platform",
        "ref",
        "revision",
    }
    existing_fields = {
        "delivery",
        "id",
        "name",
        "platform",
        "ref",
        "revision",
    }
    for name, delivery in expected_delivery.items():
        image = images[name]
        expected_fields = (
            archive_fields if delivery == "archive" else existing_fields
        )
        if (
            not isinstance(image, dict)
            or set(image) != expected_fields
            or image["name"] != name
            or image["delivery"] != delivery
            or image["platform"] != "linux/amd64"
            or not isinstance(image["ref"], str)
            or not image["ref"]
            or not isinstance(image["id"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image["id"]) is None
        ):
            raise PackageSelfcheckError("RELEASE_MANIFEST image 身份无效。")
        revision = image["revision"]
        if revision is not None and (
            not isinstance(revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        ):
            raise PackageSelfcheckError(
                "RELEASE_MANIFEST image revision 无效。"
            )
        if name in {"app", "ocr"} and revision is None:
            raise PackageSelfcheckError(
                "RELEASE_MANIFEST image revision 缺失。"
            )
        if delivery != "archive":
            continue
        archive_name = image["archive_name"]
        if (
            not isinstance(archive_name, str)
            or archive_name != f"{name}-image.tar.gz"
            or not isinstance(image["archive_sha256"], str)
            or _SHA256.fullmatch(image["archive_sha256"]) is None
            or _sha256(root / archive_name) != image["archive_sha256"]
            or not isinstance(image["manifest_digest"], str)
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}", image["manifest_digest"]
            )
            is None
            or not isinstance(image["config_digest"], str)
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}", image["config_digest"]
            )
            is None
        ):
            raise PackageSelfcheckError("RELEASE_MANIFEST archive 身份无效。")


def _verify_archive_sidecars(root: Path, names: object) -> None:
    if not isinstance(names, list) or any(
        not isinstance(name, str) for name in names
    ):
        raise PackageSelfcheckError("package contract archives 无效。")
    for name in names:
        _verify_sidecar(root / name, root / f"{name}.sha256")


def _verify_sidecar(archive: Path, sidecar: Path) -> None:
    if (
        not archive.is_file()
        or archive.is_symlink()
        or not sidecar.is_file()
        or sidecar.is_symlink()
    ):
        raise PackageSelfcheckError("归档或 sidecar 不是普通文件。")
    lines = sidecar.read_text(encoding="ascii").splitlines()
    if len(lines) != 1:
        raise PackageSelfcheckError("SHA256 sidecar 必须恰有一行。")
    match = _SHA_LINE.fullmatch(lines[0])
    if (
        match is None
        or match.group(2) != archive.name
        or match.group(1) != _sha256(archive)
    ):
        raise PackageSelfcheckError("SHA256 sidecar basename 或摘要无效。")


def _load_sha256sums(path: Path) -> dict[PurePosixPath, str]:
    if not path.is_file() or path.is_symlink():
        raise PackageSelfcheckError("release 缺少 SHA256SUMS。")
    values: dict[PurePosixPath, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        match = _SHA_LINE.fullmatch(line)
        if match is None:
            raise PackageSelfcheckError("SHA256SUMS 行格式无效。")
        relative = _safe_relative(match.group(2))
        if relative in values or relative == PurePosixPath("SHA256SUMS"):
            raise PackageSelfcheckError("SHA256SUMS 含重复或自身。")
        values[relative] = match.group(1)
    if not values:
        raise PackageSelfcheckError("SHA256SUMS 不能为空。")
    return values


def _scan_release_safety(
    root: Path,
    files: set[PurePosixPath],
) -> None:
    for relative in files:
        path = root.joinpath(*relative.parts)
        folded_parts = {part.casefold() for part in relative.parts}
        if folded_parts & _FORBIDDEN_NAMES:
            raise PackageSelfcheckError("release 含禁止文件或目录。")
        if (
            relative.suffix.casefold() in {".doc", ".pyc"}
            or "zone.identifier" in relative.as_posix().casefold()
            or path.stat().st_mode & _UNSAFE_MODE_MASK
            or any(
                part.startswith(".env.") and part != ".env.example"
                for part in folded_parts
            )
        ):
            raise PackageSelfcheckError(
                "release 含不安全文件、权限或原始 DOC。"
            )
        if relative.suffix.casefold() in {
            ".json",
            ".yaml",
            ".yml",
            ".sh",
            ".md",
            ".txt",
            ".example",
            ".jsonl",
            ".py",
        }:
            payload = path.read_bytes()
            if _contains_secret(payload):
                raise PackageSelfcheckError(
                    "release 文本文件命中 secret/path 扫描。"
                )


def _contains_secret(payload: bytes) -> bool:
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(payload):
            if b"REPLACE_" not in match.group(0):
                return True
    return False


def _release_files(root: Path) -> set[PurePosixPath]:
    files: set[PurePosixPath] = set()
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise PackageSelfcheckError("release 不能含 symlink。")
        if path == root or path.is_dir():
            continue
        if not path.is_file():
            raise PackageSelfcheckError("release 不能含特殊文件。")
        relative = PurePosixPath(path.relative_to(root).as_posix())
        _safe_relative(relative.as_posix())
        files.add(relative)
    if not files:
        raise PackageSelfcheckError("release 不能为空。")
    return files


def _validate_corpus_members(
    members: list[tarfile.TarInfo],
) -> dict[PurePosixPath, int]:
    modes = _validate_generic_members(members)
    allowed_roots = {
        "docs",
        "reference",
        "industry-corpus-manifest.json",
        "industry-corpus-audit.json",
    }
    if {
        _safe_relative(member.name).parts[0] for member in members
    } - allowed_roots:
        raise PackageSelfcheckError("corpus 归档含预期外顶层成员。")
    return modes


def _validate_generic_members(
    members: list[tarfile.TarInfo],
) -> dict[PurePosixPath, int]:
    if not members:
        raise PackageSelfcheckError("tar 归档不能为空。")
    modes: dict[PurePosixPath, int] = {}
    for member in members:
        relative = _safe_relative(member.name)
        if relative in modes:
            raise PackageSelfcheckError("tar 归档含重复成员。")
        if not (member.isdir() or member.isfile()):
            raise PackageSelfcheckError("tar 只允许普通文件和目录。")
        if member.mode & _UNSAFE_MODE_MASK:
            raise PackageSelfcheckError("tar 成员权限不安全。")
        modes[relative] = member.mode
    return modes


def _extract_members(
    bundle: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    destination: Path,
) -> None:
    root = destination.resolve(strict=True)
    for member in members:
        relative = _safe_relative(member.name)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(root):
            raise PackageSelfcheckError("tar 解包路径越界。")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        source = bundle.extractfile(member)
        if source is None:
            raise PackageSelfcheckError("tar 普通文件无法读取。")
        with source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=_HASH_BLOCK_BYTES)


def _verify_extracted_corpus(root: Path) -> None:
    manifest = _load_object(root / "industry-corpus-manifest.json")
    documents = manifest.get("documents")
    active = manifest.get("active_documents")
    reference = manifest.get("reference_documents")
    if (
        manifest.get("schema_version") != "1"
        or not isinstance(documents, list)
        or not isinstance(active, list)
        or not isinstance(reference, list)
    ):
        raise PackageSelfcheckError("corpus manifest schema 无效。")
    expected: set[PurePosixPath] = set()
    for item in documents:
        if not isinstance(item, dict):
            raise PackageSelfcheckError("corpus document manifest 无效。")
        relative = _safe_relative(item.get("target_relative_path"))
        digest = item.get("target_sha256")
        size = item.get("target_size")
        path = root.joinpath(*relative.parts)
        if (
            relative in expected
            or relative.suffix.casefold() != ".docx"
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(size, int)
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != size
            or _sha256(path) != digest
        ):
            raise PackageSelfcheckError("corpus DOCX 身份无效。")
        expected.add(relative)
    listed = {
        _safe_relative(value)
        for value in (*active, *reference)
        if isinstance(value, str)
    }
    actual = {
        PurePosixPath(path.relative_to(root).as_posix())
        for directory in (root / "docs", root / "reference")
        for path in directory.iterdir()
        if path.is_file()
    }
    if expected != listed or expected != actual:
        raise PackageSelfcheckError(
            "corpus active/reference exact set 不一致。"
        )


def _restore_modes(root: Path, modes: dict[PurePosixPath, int]) -> None:
    for relative, original in modes.items():
        path = root.joinpath(*relative.parts)
        if path.is_dir():
            path.chmod(_PRIVATE_DIRECTORY_MODE)
        elif original & _EXECUTE_MODE_MASK:
            path.chmod(_PRIVATE_EXECUTABLE_MODE)
        else:
            path.chmod(_PRIVATE_FILE_MODE)


def _publish_directory(source: Path, destination: Path) -> None:
    if source.parent.resolve(strict=True) != destination.parent.resolve(
        strict=True
    ):
        raise PackageSelfcheckError("原子发布要求相同真实父目录。")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError("原子发布目标已存在。")
    raise OSError(error_number, os.strerror(error_number), destination)


def _safe_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise PackageSelfcheckError("相对路径类型无效。")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "." in path.parts
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
    ):
        raise PackageSelfcheckError("归档路径越界或不规范。")
    return path


def _load_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise PackageSelfcheckError(f"缺少普通 JSON 文件：{path.name}")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageSelfcheckError(f"JSON 无效：{path.name}") from error
    if not isinstance(value, dict):
        raise PackageSelfcheckError(f"JSON 顶层必须是对象：{path.name}")
    return value


def _require_real_directory(path: Path) -> Path:
    if not path.is_dir() or path.is_symlink():
        raise PackageSelfcheckError("release root 必须是真实目录。")
    return path.resolve(strict=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser("release")
    release.add_argument("root", type=Path)
    corpus = commands.add_parser("extract-corpus")
    corpus.add_argument("archive", type=Path)
    corpus.add_argument("sidecar", type=Path)
    corpus.add_argument("destination", type=Path)
    outer = commands.add_parser("outer")
    outer.add_argument("archive", type=Path)
    outer.add_argument("sidecar", type=Path)
    outer.add_argument("destination", type=Path)
    publish = commands.add_parser("publish")
    publish.add_argument("source", type=Path)
    publish.add_argument("destination", type=Path)
    return parser.parse_args()


def main() -> int:
    """执行无网络 package selfcheck 或安全解包。

    Returns:
        校验通过返回 0；异常由调用方获得非零退出。

    """
    arguments = _arguments()
    if arguments.command == "release":
        print(
            json.dumps(
                verify_release(arguments.root),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    elif arguments.command == "extract-corpus":
        print(
            extract_corpus(
                arguments.archive,
                arguments.sidecar,
                arguments.destination,
            )
        )
    elif arguments.command == "outer":
        print(
            verify_outer_archive(
                arguments.archive,
                arguments.sidecar,
                arguments.destination,
            )
        )
    else:
        publish_directory(arguments.source, arguments.destination)
        print(f"published={arguments.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
