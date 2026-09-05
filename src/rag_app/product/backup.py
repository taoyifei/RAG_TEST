"""Product Runtime 的一致性备份、校验与非覆盖恢复。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from qdrant_client import QdrantClient

_DATABASE_NAME = "universal-rag.sqlite3"
_AUXILIARY_DATABASES = ("provider-budget.sqlite3", "p11-live-state.sqlite3")
_MANIFEST_NAME = "backup-manifest.json"
_COMPATIBILITY_NAME = "compatibility-manifest.json"
_FORMAT_VERSION = 1
_MIN_SECRET_LENGTH = 16
_MAX_SECRET_LENGTH = 4096
_BLOB_PATH_PARTS = 3
_SHA256_HEX_LENGTH = 64
_CAS_PREFIX_LENGTH = 2
_SECRET_NAMES = frozenset(
    {"master-key", "admin-bootstrap-token", "qdrant-api-key", "qdrant.yaml"}
)


@dataclass(frozen=True, slots=True)
class BackupReport:
    """不含路径内数据或 Secret 的备份结果。"""

    archive: str
    archive_sha256: str
    collection_count: int
    file_count: int
    sqlite_integrity: str


def create_backup(
    *,
    data_dir: Path,
    output: Path,
    compatibility_manifest: Path,
    qdrant_url: str,
    qdrant_api_key_file: Path,
) -> BackupReport:
    """创建 SQLite、Blob 和 Qdrant Collection 的统一备份。

    Args:
        data_dir: Product Runtime 持久数据根。
        output: 不允许预先存在的 tar.gz 输出文件。
        compatibility_manifest: 当前 Release 兼容清单。
        qdrant_url: 可访问的 Qdrant REST URL。
        qdrant_api_key_file: 权限严格为 0600 的 API Key 文件。

    Returns:
        仅含摘要和计数的安全报告。

    Raises:
        FileExistsError: 输出已经存在。
        ValueError: 输入路径、SQLite、Blob 或 Qdrant 合同无效。

    """
    root = _safe_directory(data_dir, label="数据目录")
    database = _safe_file(root / _DATABASE_NAME, label="SQLite 数据库")
    compatibility = _safe_file(
        compatibility_manifest, label="Compatibility Manifest"
    )
    if output.exists():
        raise FileExistsError("备份输出已存在，禁止覆盖。")
    output.parent.mkdir(parents=True, exist_ok=True)
    api_key = _read_private_secret(qdrant_api_key_file)
    with tempfile.TemporaryDirectory(prefix="rag-backup-") as temporary:
        staging = Path(temporary)
        sqlite_target = staging / "sqlite" / _DATABASE_NAME
        sqlite_target.parent.mkdir()
        _sqlite_snapshot(database, sqlite_target)
        auxiliary_databases = []
        for name in _AUXILIARY_DATABASES:
            source = root / name
            if source.exists():
                source = _safe_file(source, label="验收账本")
                _sqlite_snapshot(source, sqlite_target.parent / name)
                auxiliary_databases.append(name)
        shutil.copyfile(compatibility, staging / _COMPATIBILITY_NAME)
        _copy_blob_tree(root / "blobs", staging / "blobs")
        collections = _active_collections(sqlite_target)
        qdrant_files, qdrant_version = _snapshot_collections(
            collections,
            staging / "qdrant",
            qdrant_url=qdrant_url,
            api_key=api_key,
        )
        file_records = _file_records(staging)
        manifest = {
            "format_version": _FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "database": f"sqlite/{_DATABASE_NAME}",
            "auxiliary_databases": auxiliary_databases,
            "compatibility_manifest": _COMPATIBILITY_NAME,
            "qdrant_server_version": qdrant_version,
            "collections": qdrant_files,
            "files": file_records,
            "secrets_included": False,
        }
        (staging / _MANIFEST_NAME).write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        with tarfile.open(output, "x:gz") as archive:
            for path in sorted(staging.rglob("*")):
                archive.add(
                    path,
                    arcname=path.relative_to(staging).as_posix(),
                    recursive=False,
                )
    return verify_backup(output)


def verify_backup(archive_path: Path) -> BackupReport:
    """校验归档成员、SHA、SQLite 完整性和 Secret 排除。

    Args:
        archive_path: 待验证的统一备份归档。

    Returns:
        验证通过后的安全报告。

    Raises:
        ValueError: 归档结构、摘要或 SQLite 完整性失败。

    """
    archive = _safe_file(archive_path, label="备份归档")
    with tempfile.TemporaryDirectory(prefix="rag-verify-") as temporary:
        root = Path(temporary)
        _safe_extract(archive, root)
        manifest = _load_manifest(root / _MANIFEST_NAME)
        records = manifest.get("files")
        if not isinstance(records, list):
            raise ValueError("备份文件清单缺失。")
        expected: dict[str, tuple[str, int]] = {}
        for item in records:
            if not isinstance(item, dict):
                raise ValueError("备份文件清单条目无效。")
            relative = _safe_relative_path(str(item.get("path", "")))
            expected[relative.as_posix()] = (
                str(item.get("sha256", "")),
                int(item.get("size", -1)),
            )
        actual_files = {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and path.name != _MANIFEST_NAME
        }
        if set(expected) != set(actual_files):
            raise ValueError("备份实际文件与 Manifest 不一致。")
        for name, path in actual_files.items():
            digest, size = expected[name]
            if path.stat().st_size != size or _sha256(path) != digest:
                raise ValueError("备份文件摘要校验失败。")
            if path.name in _SECRET_NAMES:
                raise ValueError("普通备份禁止包含 Secret 文件。")
        database_name = str(manifest.get("database", ""))
        database = root / _safe_relative_path(database_name)
        integrity = _sqlite_integrity(database)
        if integrity != "ok":
            raise ValueError("备份 SQLite integrity_check 失败。")
        for name in _auxiliary_databases(manifest):
            if _sqlite_integrity(root / "sqlite" / name) != "ok":
                raise ValueError("备份预算或阶段账本 integrity_check 失败。")
        collections = manifest.get("collections")
        if not isinstance(collections, dict):
            raise ValueError("Qdrant Snapshot 清单缺失。")
        return BackupReport(
            archive=str(archive.resolve()),
            archive_sha256=_sha256(archive),
            collection_count=len(collections),
            file_count=len(actual_files),
            sqlite_integrity=integrity,
        )


def restore_backup(
    *,
    archive_path: Path,
    target_data_dir: Path,
    qdrant_url: str,
    qdrant_api_key_file: Path,
) -> BackupReport:
    """把已验证备份恢复到空目录和不存在的 Qdrant Collections。

    Args:
        archive_path: 已创建的统一备份。
        target_data_dir: 不存在或为空的恢复数据目录。
        qdrant_url: 目标 Qdrant REST URL。
        qdrant_api_key_file: 目标 Qdrant 的 0600 API Key 文件。

    Returns:
        恢复前重新验证得到的安全报告。

    Raises:
        ValueError: 目标非空、Collection 已存在或恢复失败。

    """
    report = verify_backup(archive_path)
    target = _empty_target(target_data_dir)
    api_key = _read_private_secret(qdrant_api_key_file)
    with tempfile.TemporaryDirectory(prefix="rag-restore-") as temporary:
        root = Path(temporary)
        _safe_extract(archive_path, root)
        manifest = _load_manifest(root / _MANIFEST_NAME)
        collections = manifest["collections"]
        if not isinstance(collections, dict):
            raise ValueError("Qdrant Snapshot 清单无效。")
        if collections:
            client = QdrantClient(
                url=qdrant_url,
                api_key=api_key,
                check_compatibility=False,
            )
            try:
                for collection_name in collections:
                    if client.collection_exists(collection_name):
                        raise ValueError(
                            "目标 Qdrant Collection 已存在，禁止覆盖。"
                        )
            finally:
                client.close()
        created: list[str] = []
        try:
            for collection_name, snapshot_path in collections.items():
                snapshot = root / _safe_relative_path(str(snapshot_path))
                created.append(collection_name)
                _upload_snapshot(
                    collection_name,
                    snapshot,
                    qdrant_url=qdrant_url,
                    api_key=api_key,
                )
            shutil.copyfile(
                root / "sqlite" / _DATABASE_NAME,
                target / _DATABASE_NAME,
            )
            for name in _auxiliary_databases(manifest):
                shutil.copyfile(root / "sqlite" / name, target / name)
            # 首绑前备份也可能缺少后来消费；所有恢复都必须先对账。
            (target / "provider-budget.restore-blocked").write_text(
                "RECONCILE_WITH_AUTHORITATIVE_CAMPAIGN_BEFORE_LIVE\n",
                encoding="utf-8",
            )
            source_blobs = root / "blobs"
            if source_blobs.is_dir():
                shutil.copytree(source_blobs, target / "blobs")
            shutil.copyfile(
                root / _COMPATIBILITY_NAME,
                target / _COMPATIBILITY_NAME,
            )
        except Exception:
            _remove_created_collections(
                created, qdrant_url=qdrant_url, api_key=api_key
            )
            shutil.rmtree(target)
            raise
    return report


def _sqlite_snapshot(source: Path, target: Path) -> None:
    with (
        sqlite3.connect(source) as source_connection,
        sqlite3.connect(target) as target_connection,
    ):
        source_connection.backup(target_connection)
    if _sqlite_integrity(target) != "ok":
        raise ValueError("SQLite 一致性快照完整性失败。")


def _auxiliary_databases(manifest: dict[str, Any]) -> tuple[str, ...]:
    names = manifest.get("auxiliary_databases", [])
    if (
        not isinstance(names, list)
        or any(name not in _AUXILIARY_DATABASES for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("备份包含未知或重复辅助数据库。")
    return tuple(names)


def _sqlite_integrity(path: Path) -> str:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise ValueError("SQLite 备份无法只读打开。") from error
    return "" if row is None else str(row[0])


def _active_collections(database: Path) -> tuple[str, ...]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT DISTINCT r.physical_vector_namespace "
            "FROM knowledge_bases k JOIN index_revisions r "
            "ON r.index_revision_id=k.active_revision_id "
            "WHERE k.deleted_at IS NULL ORDER BY r.physical_vector_namespace"
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _snapshot_collections(
    collections: tuple[str, ...],
    target: Path,
    *,
    qdrant_url: str,
    api_key: str,
) -> tuple[dict[str, str], str]:
    target.mkdir()
    client = QdrantClient(
        url=qdrant_url,
        api_key=api_key,
        check_compatibility=False,
    )
    downloaded: dict[str, str] = {}
    try:
        version = _qdrant_version(qdrant_url, api_key)
        for collection in collections:
            if not client.collection_exists(collection):
                raise ValueError("Active Revision 的 Qdrant Collection 缺失。")
            description = client.create_snapshot(collection_name=collection)
            if description is None or not description.name:
                raise ValueError("Qdrant 未返回 Snapshot 名称。")
            destination = target / f"{collection}.snapshot"
            try:
                _download_snapshot(
                    collection,
                    description.name,
                    destination,
                    qdrant_url=qdrant_url,
                    api_key=api_key,
                )
            finally:
                client.delete_snapshot(
                    collection_name=collection,
                    snapshot_name=description.name,
                )
            downloaded[collection] = destination.relative_to(
                target.parent
            ).as_posix()
    finally:
        client.close()
    return downloaded, version


def _download_snapshot(
    collection: str,
    snapshot_name: str,
    destination: Path,
    *,
    qdrant_url: str,
    api_key: str,
) -> None:
    url = (
        f"{qdrant_url.rstrip('/')}/collections/{quote(collection, safe='')}"
        f"/snapshots/{quote(snapshot_name, safe='')}"
    )
    with httpx.Client(timeout=120.0) as client:
        response = client.get(url, headers={"api-key": api_key})
        response.raise_for_status()
        destination.write_bytes(response.content)
    if destination.stat().st_size == 0:
        raise ValueError("Qdrant Snapshot 下载为空。")


def _upload_snapshot(
    collection: str,
    snapshot: Path,
    *,
    qdrant_url: str,
    api_key: str,
) -> None:
    url = (
        f"{qdrant_url.rstrip('/')}/collections/{quote(collection, safe='')}"
        "/snapshots/upload?wait=true&priority=snapshot"
    )
    with snapshot.open("rb") as stream, httpx.Client(timeout=300.0) as client:
        response = client.post(
            url,
            headers={"api-key": api_key},
            files={"snapshot": (snapshot.name, stream)},
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("result") is not True:
        raise ValueError("Qdrant Snapshot 恢复响应无效。")


def _qdrant_version(qdrant_url: str, api_key: str) -> str:
    with httpx.Client(timeout=10.0) as client:
        response = client.get(
            qdrant_url.rstrip("/") + "/",
            headers={"api-key": api_key},
        )
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(
        payload.get("version"), str
    ):
        raise ValueError("Qdrant Version 响应无效。")
    return str(payload["version"])


def _remove_created_collections(
    collections: list[str], *, qdrant_url: str, api_key: str
) -> None:
    client = QdrantClient(
        url=qdrant_url,
        api_key=api_key,
        check_compatibility=False,
    )
    try:
        for collection in collections:
            client.delete_collection(collection)
    finally:
        client.close()


def _copy_blob_tree(source: Path, target: Path) -> None:
    if not source.exists():
        return
    _safe_directory(source, label="Blob 目录")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Blob 备份禁止 symlink。")
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _validate_blob_file(path, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        else:
            raise ValueError("Blob 备份只允许普通文件和目录。")


def _validate_blob_file(path: Path, relative: Path) -> None:
    parts = relative.parts
    if len(parts) != _BLOB_PATH_PARTS or parts[0] != "sha256":
        raise ValueError("Blob 备份发现异常 CAS 布局。")
    prefix, digest = parts[1], parts[2]
    if (
        len(prefix) != _CAS_PREFIX_LENGTH
        or len(digest) != _SHA256_HEX_LENGTH
        or digest[:_CAS_PREFIX_LENGTH] != prefix
        or any(character not in "0123456789abcdef" for character in digest)
        or _sha256(path) != digest
    ):
        raise ValueError("Blob 备份发现内容寻址摘要漂移。")


def _file_records(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _safe_extract(archive_path: Path, target: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            relative = _safe_relative_path(member.name)
            if (
                member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("备份归档含不安全成员。")
            destination = target / relative
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("备份归档文件无法读取。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Backup Manifest 无法读取。") from error
    if (
        not isinstance(payload, dict)
        or payload.get("format_version") != _FORMAT_VERSION
        or payload.get("secrets_included") is not False
        or payload.get("compatibility_manifest") != _COMPATIBILITY_NAME
    ):
        raise ValueError("Backup Manifest 版本或安全声明无效。")
    return payload


def _safe_relative_path(value: str) -> Path:
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts:
        raise ValueError("备份包含不安全相对路径。")
    return Path(*pure.parts)


def _safe_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label}必须是现有非 symlink 目录。")
    return path.resolve()


def _safe_file(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}必须是现有非 symlink 普通文件。")
    return path.resolve()


def _empty_target(path: Path) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("恢复目标必须是普通目录。")
    if path.exists() and any(path.iterdir()):
        raise ValueError("恢复目标必须为空，禁止覆盖现有数据。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path.resolve()


def _read_private_secret(path: Path) -> str:
    secret = _safe_file(path, label="Qdrant API Key")
    if stat.S_IMODE(secret.stat().st_mode) != stat.S_IRUSR | stat.S_IWUSR:
        raise ValueError("Qdrant API Key 文件权限必须严格为 0600。")
    value = secret.read_text(encoding="utf-8").rstrip("\r\n")
    if not _MIN_SECRET_LENGTH <= len(value) <= _MAX_SECRET_LENGTH:
        raise ValueError("Qdrant API Key 长度必须为 16 到 4096。")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def report_json(report: BackupReport) -> str:
    """把安全报告编码为稳定 JSON。

    Args:
        report: 已验证的备份结果。

    Returns:
        可直接输出且不含 Secret 的 JSON。

    """
    return json.dumps(
        asdict(report),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "BackupReport",
    "create_backup",
    "report_json",
    "restore_backup",
    "verify_backup",
]
