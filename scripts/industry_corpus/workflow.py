"""编排工业旧版 DOC 到审计 DOCX corpus 的原子发布流程。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.industry_corpus.ooxml import OoxmlAudit, clean_docx
from scripts.offline_bundle import publish_directory

__all__ = [
    "EXPECTED_INVENTORY",
    "CorpusPreparationError",
    "PreparedCorpus",
    "SourceSpec",
    "load_expected_inventory",
    "prepare_industry_corpus",
]

_CORPUS_NAME = "husky-industry-management"
_SCHEMA_VERSION = "1"
_PREPROCESSING_REVISION = "industry-corpus-v1"
_MANIFEST_NAME = "industry-corpus-manifest.json"
_AUDIT_NAME = "industry-corpus-audit.json"
_HASH_BLOCK_BYTES = 1024 * 1024
_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXPECTED_FIELDS = {"canonical_name", "role"}
_PROXY_ENVIRONMENT_NAMES = {
    "ALL_PROXY",
    "FTP_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "all_proxy",
    "ftp_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


class CorpusPreparationError(RuntimeError):
    """表示工业语料无法安全、完整地发布。"""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """预期旧版 Word 文件及其部署角色。"""

    canonical_name: str
    role: str = "active"


EXPECTED_INVENTORY = tuple(
    SourceSpec(name)
    for name in (
        "GM-01 质量管理机构图.doc",
        "GM-02 岗位职责规定.doc",
        "GM-03 质量管理制度.doc",
        "GM-04 质量管理制度及考核办法.doc",
        "GM-05 劳保用品发放管理制度.doc",
        "GM-06 产品质量检验管理制度.doc",
        "GM-07 技术文件管理规定.doc",
        "GM-08 安全生产管理制度.doc",
        "GM-09 仓库管理制度.doc",
        "GM-10 计量器具管理制度.doc",
    )
)


@dataclass(frozen=True, slots=True)
class _SourceIdentity:
    canonical_name: str
    actual_name: str
    role: str
    size: int
    mtime_ns: int
    sha256: str
    path: Path

    def public_dict(self) -> dict[str, object]:
        """返回不含本机绝对路径的源身份。

        Args:
            无参数；字段取自当前源文件身份。

        Returns:
            文件名、角色、大小、mtime 和摘要。

        """
        return {
            "actual_name": self.actual_name,
            "canonical_name": self.canonical_name,
            "mtime_ns": self.mtime_ns,
            "role": self.role,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class _ConverterInfo:
    name: str
    version: str
    network_namespace: bool


@dataclass(frozen=True, slots=True)
class _PreparedDocument:
    source: _SourceIdentity
    target_relative_path: str
    size: int
    sha256: str
    audit: OoxmlAudit

    def manifest_dict(self) -> dict[str, object]:
        """返回 release manifest 可公开的单文档身份。

        Args:
            无参数；字段取自当前已处理文档。

        Returns:
            源身份、目标路径、摘要和结构计数。

        """
        value = self.source.public_dict()
        value.update(
            {
                "target_relative_path": self.target_relative_path,
                "target_sha256": self.sha256,
                "target_size": self.size,
                **self.audit.as_dict(),
            }
        )
        return value


@dataclass(frozen=True, slots=True)
class PreparedCorpus:
    """原子发布后的工业 corpus 身份。"""

    root: Path
    corpus_revision: str
    corpus_sha256: str
    active_document_count: int
    reference_document_count: int
    manifest_path: Path
    audit_path: Path


def load_expected_inventory(path: Path) -> tuple[SourceSpec, ...]:
    """读取可选的严格 expected inventory JSON。

    Args:
        path: 只含 `canonical_name` 和 `role` 的 JSON 数组。

    Returns:
        保持文件顺序的预期源文件集合。

    Raises:
        CorpusPreparationError: 文件 schema、角色或文件名无效。

    """
    if not path.is_file() or path.is_symlink():
        raise CorpusPreparationError("expected inventory 必须是普通文件。")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusPreparationError(
            "expected inventory 不是有效 JSON。"
        ) from error
    if not isinstance(value, list) or not value:
        raise CorpusPreparationError("expected inventory 必须是非空数组。")
    specs: list[SourceSpec] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _EXPECTED_FIELDS:
            raise CorpusPreparationError("expected inventory schema 无效。")
        name = item["canonical_name"]
        role = item["role"]
        if (
            not isinstance(name, str)
            or not isinstance(role, str)
            or role not in {"active", "reference"}
            or Path(name).name != name
            or not name.casefold().endswith(".doc")
        ):
            raise CorpusPreparationError(
                "expected inventory 文件名或角色无效。"
            )
        specs.append(SourceSpec(canonical_name=name, role=role))
    _validate_specs(tuple(specs))
    return tuple(specs)


def prepare_industry_corpus(  # noqa: PLR0913
    *,
    source_dir: Path,
    output_root: Path,
    libreoffice_path: Path,
    source_date_epoch: int,
    generated_from_git_sha: str,
    timeout_seconds: float = 120.0,
    expected_inventory: Sequence[SourceSpec] = EXPECTED_INVENTORY,
    manifest_name: str = _MANIFEST_NAME,
    audit_name: str = _AUDIT_NAME,
) -> PreparedCorpus:
    """转换、审计并原子发布完整工业 DOCX corpus。

    Args:
        source_dir: 只含预期旧版 `.doc` 的真实目录。
        output_root: 用于发布 `<corpus-revision>` 子目录的父目录。
        libreoffice_path: 已安装 LibreOffice/soffice 可执行文件。
        source_date_epoch: 固定 manifest 与 ZIP metadata 的 Unix 时间。
        generated_from_git_sha: 当前预处理代码对应的完整 Git SHA。
        timeout_seconds: 每个转换进程的硬超时秒数。
        expected_inventory: 严格预期文件与 active/reference 角色。
        manifest_name: corpus 根目录中的 manifest 文件名。
        audit_name: corpus 根目录中的 audit 文件名。

    Returns:
        已发布目录和 corpus 身份。

    Raises:
        CorpusPreparationError: 任一输入、转换、审计或发布门禁失败。

    """
    specs = tuple(expected_inventory)
    _validate_inputs(
        source_date_epoch=source_date_epoch,
        generated_from_git_sha=generated_from_git_sha,
        timeout_seconds=timeout_seconds,
        manifest_name=manifest_name,
        audit_name=audit_name,
    )
    _validate_specs(specs)
    sources = _inventory_sources(source_dir, specs)
    converter = _inspect_converter(libreoffice_path)
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink() or not output_root.is_dir():
        raise CorpusPreparationError("output root 必须是真实目录。")
    with tempfile.TemporaryDirectory(
        dir=output_root,
        prefix=".industry-work-",
    ) as work_name, tempfile.TemporaryDirectory(
        dir=output_root,
        prefix=".industry-stage-",
    ) as stage_name:
        work = Path(work_name)
        stage = Path(stage_name)
        docs = stage / "docs"
        reference = stage / "reference"
        docs.mkdir(parents=True)
        reference.mkdir()
        prepared = tuple(
            _prepare_document(
                source=source,
                stage=stage,
                work=work,
                converter_path=libreoffice_path.resolve(strict=True),
                converter=converter,
                timeout_seconds=timeout_seconds,
                source_date_epoch=source_date_epoch,
            )
            for source in sources
        )
        _verify_output_set(stage, prepared, specs)
        corpus_sha256 = _corpus_digest(prepared)
        corpus_revision = corpus_sha256[:16]
        generated_at = datetime.fromtimestamp(
            source_date_epoch,
            tz=UTC,
        ).isoformat().replace("+00:00", "Z")
        manifest = _build_manifest(
            sources=sources,
            prepared=prepared,
            converter=converter,
            corpus_revision=corpus_revision,
            corpus_sha256=corpus_sha256,
            generated_at=generated_at,
            source_date_epoch=source_date_epoch,
            generated_from_git_sha=generated_from_git_sha,
        )
        audit = _build_audit(
            prepared=prepared,
            converter=converter,
            corpus_revision=corpus_revision,
            generated_at=generated_at,
        )
        _write_canonical_json(stage / manifest_name, manifest)
        _write_canonical_json(stage / audit_name, audit)
        _verify_public_json(stage / manifest_name)
        _verify_public_json(stage / audit_name)
        final = output_root / corpus_revision
        try:
            publish_directory(stage, final)
        except (FileExistsError, OSError) as error:
            raise CorpusPreparationError("corpus 原子发布失败。") from error
    return PreparedCorpus(
        root=final,
        corpus_revision=corpus_revision,
        corpus_sha256=corpus_sha256,
        active_document_count=sum(
            item.source.role == "active" for item in prepared
        ),
        reference_document_count=sum(
            item.source.role == "reference" for item in prepared
        ),
        manifest_path=final / manifest_name,
        audit_path=final / audit_name,
    )


def _validate_inputs(
    *,
    source_date_epoch: int,
    generated_from_git_sha: str,
    timeout_seconds: float,
    manifest_name: str,
    audit_name: str,
) -> None:
    if type(source_date_epoch) is not int or source_date_epoch < 0:
        raise CorpusPreparationError("source-date-epoch 必须是非负整数。")
    if _FULL_REVISION.fullmatch(generated_from_git_sha) is None:
        raise CorpusPreparationError("generated Git SHA 必须是完整小写 SHA。")
    if timeout_seconds <= 0:
        raise CorpusPreparationError("timeout 必须大于零。")
    for name in (manifest_name, audit_name):
        if (
            _SAFE_OUTPUT_NAME.fullmatch(name) is None
            or not name.endswith(".json")
        ):
            raise CorpusPreparationError(
                "manifest/audit 输出名必须是安全 JSON basename。"
            )
    if manifest_name == audit_name:
        raise CorpusPreparationError("manifest 与 audit 输出名必须不同。")


def _validate_specs(specs: tuple[SourceSpec, ...]) -> None:
    if not specs:
        raise CorpusPreparationError("expected inventory 不能为空。")
    keys: set[str] = set()
    canonical_names: set[str] = set()
    for spec in specs:
        if (
            spec.role not in {"active", "reference"}
            or Path(spec.canonical_name).name != spec.canonical_name
            or not spec.canonical_name.casefold().endswith(".doc")
        ):
            raise CorpusPreparationError("expected inventory 条目无效。")
        key = _inventory_key(spec.canonical_name)
        if key in keys or spec.canonical_name.casefold() in canonical_names:
            raise CorpusPreparationError("expected inventory 含重复文件。")
        keys.add(key)
        canonical_names.add(spec.canonical_name.casefold())


def _inventory_sources(
    source_dir: Path,
    specs: tuple[SourceSpec, ...],
) -> tuple[_SourceIdentity, ...]:
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise CorpusPreparationError("source dir 必须是真实目录。")
    expected_by_key = {
        _inventory_key(spec.canonical_name): spec for spec in specs
    }
    actual_by_key: dict[str, Path] = {}
    with os.scandir(source_dir) as entries:
        for entry in entries:
            path = Path(entry.path)
            mode = entry.stat(follow_symlinks=False).st_mode
            if entry.is_symlink() or not stat.S_ISREG(mode):
                raise CorpusPreparationError(
                    "source dir 含 symlink 或特殊文件。"
                )
            if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                raise CorpusPreparationError("source dir 含意外可执行文件。")
            if path.suffix.casefold() != ".doc":
                raise CorpusPreparationError("source dir 含预期外文件类型。")
            key = _inventory_key(path.name)
            if key in actual_by_key:
                raise CorpusPreparationError("source dir 含语义重复文件名。")
            actual_by_key[key] = path
    if set(actual_by_key) != set(expected_by_key):
        missing = len(set(expected_by_key) - set(actual_by_key))
        extra = len(set(actual_by_key) - set(expected_by_key))
        raise CorpusPreparationError(
            f"SOURCE_INVENTORY_MISMATCH: missing={missing}, extra={extra}"
        )
    identities: list[_SourceIdentity] = []
    for spec in specs:
        path = actual_by_key[_inventory_key(spec.canonical_name)]
        metadata = path.stat(follow_symlinks=False)
        identities.append(
            _SourceIdentity(
                canonical_name=spec.canonical_name,
                actual_name=path.name,
                role=spec.role,
                size=metadata.st_size,
                mtime_ns=metadata.st_mtime_ns,
                sha256=_sha256(path),
                path=path,
            )
        )
    return tuple(identities)


def _inventory_key(name: str) -> str:
    return "".join(name.split()).casefold()


def _inspect_converter(path: Path) -> _ConverterInfo:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CorpusPreparationError("LIBREOFFICE_NOT_FOUND") from error
    if (
        not resolved.is_file()
        or not os.access(resolved, os.X_OK)
        or resolved.name.casefold()
        not in {"libreoffice", "soffice", "soffice.bin", "lowriter"}
    ):
        raise CorpusPreparationError("LIBREOFFICE_EXECUTABLE_INVALID")
    try:
        completed = subprocess.run(  # noqa: S603
            [str(resolved), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=_converter_environment(source_date_epoch=0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CorpusPreparationError("LIBREOFFICE_VERSION_FAILED") from error
    version_line = (completed.stdout or completed.stderr).splitlines()
    if not version_line or "libreoffice" not in version_line[0].casefold():
        raise CorpusPreparationError("LIBREOFFICE_VERSION_INVALID")
    version = " ".join(version_line[0].split())[:160]
    return _ConverterInfo(
        name="LibreOffice",
        version=version,
        network_namespace=_network_namespace_available(),
    )


def _network_namespace_available() -> bool:
    executable = shutil.which("unshare")
    if executable is None:
        return False
    try:
        completed = subprocess.run(  # noqa: S603
            [executable, "--net", "--", "true"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _prepare_document(  # noqa: PLR0913
    *,
    source: _SourceIdentity,
    stage: Path,
    work: Path,
    converter_path: Path,
    converter: _ConverterInfo,
    timeout_seconds: float,
    source_date_epoch: int,
) -> _PreparedDocument:
    item_root = Path(tempfile.mkdtemp(dir=work, prefix="convert-"))
    profile = item_root / "profile"
    converted = item_root / "output"
    profile.mkdir(mode=0o700)
    converted.mkdir(mode=0o700)
    _run_converter(
        source=source.path,
        output_dir=converted,
        profile_dir=profile,
        converter_path=converter_path,
        network_namespace=converter.network_namespace,
        timeout_seconds=timeout_seconds,
        source_date_epoch=source_date_epoch,
    )
    outputs = tuple(converted.iterdir())
    if (
        len(outputs) != 1
        or not outputs[0].is_file()
        or outputs[0].is_symlink()
        or outputs[0].suffix.casefold() != ".docx"
        or outputs[0].stem.casefold() != source.path.stem.casefold()
    ):
        raise CorpusPreparationError("LIBREOFFICE_OUTPUT_SET_INVALID")
    target_name = Path(source.canonical_name).with_suffix(".docx").name
    relative = (
        Path("docs" if source.role == "active" else "reference")
        / target_name
    )
    target = stage / relative
    try:
        ooxml_audit = clean_docx(
            source=outputs[0],
            destination=target,
            canonical_name=target_name,
            source_date_epoch=source_date_epoch,
        )
    except (OSError, ValueError) as error:
        raise CorpusPreparationError("DOCX_AUDIT_FAILED") from error
    return _PreparedDocument(
        source=source,
        target_relative_path=relative.as_posix(),
        size=target.stat().st_size,
        sha256=_sha256(target),
        audit=ooxml_audit,
    )


def _run_converter(  # noqa: PLR0913
    *,
    source: Path,
    output_dir: Path,
    profile_dir: Path,
    converter_path: Path,
    network_namespace: bool,
    timeout_seconds: float,
    source_date_epoch: int,
) -> None:
    command: list[str] = []
    if network_namespace:
        unshare = shutil.which("unshare")
        if unshare is None:
            raise CorpusPreparationError("NETWORK_NAMESPACE_STATE_DRIFT")
        command.extend((unshare, "--net", "--"))
    command.extend(
        (
            str(converter_path),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            "docx:Office Open XML Text",
            "--outdir",
            str(output_dir.resolve()),
            str(source.resolve(strict=True)),
        )
    )
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=_converter_environment(source_date_epoch=source_date_epoch),
        )
    except OSError as error:
        raise CorpusPreparationError("LIBREOFFICE_START_FAILED") from error
    try:
        process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise CorpusPreparationError("LIBREOFFICE_TIMEOUT") from error
    if process.returncode != 0:
        raise CorpusPreparationError("LIBREOFFICE_NONZERO_EXIT")


def _converter_environment(*, source_date_epoch: int) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _PROXY_ENVIRONMENT_NAMES
    }
    environment.update(
        {
            "SAL_USE_VCLPLUGIN": "svp",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
        }
    )
    return environment


def _verify_output_set(
    stage: Path,
    prepared: tuple[_PreparedDocument, ...],
    specs: tuple[SourceSpec, ...],
) -> None:
    expected = {
        (
            Path("docs" if spec.role == "active" else "reference")
            / Path(spec.canonical_name).with_suffix(".docx").name
        ).as_posix()
        for spec in specs
    }
    actual = {
        path.relative_to(stage).as_posix()
        for directory in (stage / "docs", stage / "reference")
        for path in directory.iterdir()
        if path.is_file()
    }
    prepared_paths = {item.target_relative_path for item in prepared}
    if actual != expected or prepared_paths != expected:
        raise CorpusPreparationError("PREPARED_CORPUS_EXACT_SET_INVALID")
    for directory in (stage / "docs", stage / "reference"):
        for path in directory.iterdir():
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix.casefold() != ".docx"
                or path.name.startswith(".")
            ):
                raise CorpusPreparationError("PREPARED_CORPUS_MEMBER_INVALID")


def _build_manifest(  # noqa: PLR0913
    *,
    sources: tuple[_SourceIdentity, ...],
    prepared: tuple[_PreparedDocument, ...],
    converter: _ConverterInfo,
    corpus_revision: str,
    corpus_sha256: str,
    generated_at: str,
    source_date_epoch: int,
    generated_from_git_sha: str,
) -> dict[str, object]:
    active = tuple(
        item.target_relative_path
        for item in prepared
        if item.source.role == "active"
    )
    reference = tuple(
        item.target_relative_path
        for item in prepared
        if item.source.role == "reference"
    )
    return {
        "active_document_count": len(active),
        "active_documents": active,
        "authority_basis": (
            "verified override: user-provided company management corpus; "
            "not independently authenticated"
        ),
        "converter_name": converter.name,
        "converter_version": converter.version,
        "corpus_name": _CORPUS_NAME,
        "corpus_revision": corpus_revision,
        "corpus_sha256": corpus_sha256,
        "documents": tuple(item.manifest_dict() for item in prepared),
        "generated_at": generated_at,
        "generated_from_git_sha": generated_from_git_sha,
        "preprocessing_revision": _PREPROCESSING_REVISION,
        "reference_document_count": len(reference),
        "reference_documents": reference,
        "schema_version": _SCHEMA_VERSION,
        "source_date_epoch": source_date_epoch,
        "source_directory_sha256": _source_directory_digest(sources),
        "source_inventory_sha256": _source_inventory_digest(sources),
        "status_basis": "active is an explicit deployment decision",
    }


def _build_audit(
    *,
    prepared: tuple[_PreparedDocument, ...],
    converter: _ConverterInfo,
    corpus_revision: str,
    generated_at: str,
) -> dict[str, object]:
    warnings = (
        ()
        if converter.network_namespace
        else ("NETWORK_NAMESPACE_UNAVAILABLE",)
    )
    return {
        "converter_name": converter.name,
        "converter_version": converter.version,
        "corpus_revision": corpus_revision,
        "documents": tuple(
            {
                "target_relative_path": item.target_relative_path,
                **item.audit.as_dict(),
            }
            for item in prepared
        ),
        "generated_at": generated_at,
        "network_namespace_enabled": converter.network_namespace,
        "preprocessing_revision": _PREPROCESSING_REVISION,
        "schema_version": _SCHEMA_VERSION,
        "warnings": warnings,
    }


def _source_directory_digest(sources: tuple[_SourceIdentity, ...]) -> str:
    value = [
        {
            "actual_name": item.actual_name,
            "sha256": item.sha256,
            "size": item.size,
        }
        for item in sources
    ]
    return _canonical_digest(value)


def _source_inventory_digest(sources: tuple[_SourceIdentity, ...]) -> str:
    return _canonical_digest([item.public_dict() for item in sources])


def _corpus_digest(prepared: tuple[_PreparedDocument, ...]) -> str:
    value = [
        {
            "path": item.target_relative_path,
            "sha256": item.sha256,
            "size": item.size,
        }
        for item in prepared
    ]
    return _canonical_digest(value)


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_canonical_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json(value))
    path.chmod(0o600)


def _verify_public_json(path: Path) -> None:
    payload = path.read_bytes()
    forbidden = (
        b"Authorization:",
        b"BEGIN PRIVATE KEY",
        b"/home/",
        b"\\\\wsl.localhost",
        b"C:\\Users\\",
        b"http://",
        b"https://",
    )
    if any(pattern in payload for pattern in forbidden):
        raise CorpusPreparationError("PUBLIC_AUDIT_CONTAINS_PRIVATE_DATA")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()
