"""组装、校验并可重复打包 Industry 首次部署 release。"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from deployment.industry.package_selfcheck import (
    verify_outer_archive,
    verify_release,
)
from rag_app.corpus_policy import CorpusPolicy
from rag_app.runtime import load_pipeline
from rag_app.settings import RetrievalSettings
from scripts.build_simple_bundle import write_sha256_sidecar
from scripts.industry_bundle.images import ExistingImageIdentity, ImageArtifact
from scripts.offline_bundle import publish_directory

__all__ = [
    "IndustryReleaseError",
    "ReleaseIdentity",
    "assemble_release",
    "build_outer_upload",
]

_BUILDER_REVISION = "industry-bundle-v6"
_PACKAGE_CONTRACT_REVISION = "industry-package-v1"
_REUSE_BUILDER_REVISION = "industry-bundle-reuse-images-v6"
_REUSE_CONTRACT_REVISION = "industry-package-reuse-images-v1"
_FULL_RELEASE_KIND = "industry-first-deploy"
_REUSE_RELEASE_KIND = "industry-first-deploy-reuse-images"
_IMAGE_NAMES = {"app", "ocr", "qdrant"}
_HASH_BLOCK_BYTES = 1024 * 1024
_EXPECTED_ACTIVE_COUNT = 10
_RELEASE_FILES = (
    "compose.yaml",
    ".env.example",
    "DEPLOYMENT_GUIDE.md",
    "SERVER_UPLOAD_COMMANDS.txt",
    "preflight.sh",
    "install.sh",
    "deploy.sh",
    "run-index.sh",
    "verify.sh",
    "rollback.sh",
    "generate-secrets.sh",
    "lib.sh",
    "package-contract.json",
    "package_selfcheck.py",
    "preflight_endpoints.py",
    "runtime_check.py",
    "last_good.py",
)
_EXECUTABLE_FILES = {
    "preflight.sh",
    "install.sh",
    "deploy.sh",
    "run-index.sh",
    "verify.sh",
    "rollback.sh",
    "generate-secrets.sh",
    "last_good.py",
}
_CONFIG_FILES = (
    "pipeline.json",
    "retrieval.json",
    "intent-router.json",
    "intent-router-calibration.json",
    "corpus-policy.json",
)
_CORPUS_MANIFEST_FIELDS = {
    "active_document_count",
    "active_documents",
    "authority_basis",
    "converter_name",
    "converter_version",
    "corpus_name",
    "corpus_revision",
    "corpus_sha256",
    "documents",
    "generated_at",
    "generated_from_git_sha",
    "preprocessing_revision",
    "reference_document_count",
    "reference_documents",
    "schema_version",
    "source_date_epoch",
    "source_directory_sha256",
    "source_inventory_sha256",
    "status_basis",
}
_CORPUS_DOCUMENT_FIELDS = {
    "actual_name",
    "canonical_name",
    "mtime_ns",
    "role",
    "sha256",
    "size",
    "target_relative_path",
    "target_sha256",
    "target_size",
    "visible_text_sha256",
    "paragraph_count",
    "table_count",
    "image_count",
    "heading_count",
    "list_level_count",
    "ocr_candidate_count",
    "heading_candidate_count",
    "heading_accepted_count",
    "heading_rejected_count",
    "heading_reason_counts",
    "removed_private_character_count",
    "remaining_private_character_count",
    "removed_external_relationship_count",
    "external_relationship_type_counts",
    "parser_unsupported_node_count",
    "warnings",
}
_CORPUS_AUDIT_FIELDS = {
    "converter_name",
    "converter_version",
    "corpus_revision",
    "documents",
    "generated_at",
    "network_namespace_enabled",
    "preprocessing_revision",
    "schema_version",
    "warnings",
}


class IndustryReleaseError(RuntimeError):
    """表示 Industry release 无法安全组装或验证。"""


@dataclass(frozen=True, slots=True)
class _CorpusIdentity:
    revision: str
    sha256: str
    manifest_sha256: str
    active_documents: tuple[str, ...]
    active_count: int
    reference_count: int
    source_date_epoch: int


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    """已验证 release 目录和外层上传包身份。"""

    release_id: str
    release_dir: Path
    outer_archive: Path | None = None
    outer_sidecar: Path | None = None


ImageIdentity = ImageArtifact | ExistingImageIdentity


def assemble_release(  # noqa: PLR0913
    *,
    repository_root: Path,
    stage: Path,
    corpus_root: Path,
    git_sha: str,
    main_sha: str,
    source_date_epoch: int,
    images: tuple[ImageIdentity, ImageIdentity, ImageIdentity],
) -> ReleaseIdentity:
    """在仅含交付镜像归档的 staging 目录中组装 release。

    Args:
        repository_root: clean Industry 仓库根目录。
        stage: 仅含本次实际交付镜像归档的 staging 目录。
        corpus_root: `prepare_industry_corpus` 的真实输出目录。
        git_sha: 当前 Industry 完整提交 SHA。
        main_sha: 构建时 main 完整提交 SHA。
        source_date_epoch: 当前提交的稳定时间戳。
        images: app、OCR、Qdrant 的归档或服务器既有镜像身份。

    Returns:
        尚未外层打包的 release 身份。

    Raises:
        IndustryReleaseError: corpus、config、文件集合或 selfcheck 失败。

    """
    release_kind = _release_kind(images)
    expected_archives = {
        image.archive_name
        for image in images
        if isinstance(image, ImageArtifact)
    }
    if (
        not stage.is_dir()
        or stage.is_symlink()
        or {path.name for path in stage.iterdir()} != expected_archives
        or any(
            not path.is_file() or path.is_symlink() for path in stage.iterdir()
        )
    ):
        raise IndustryReleaseError(
            "release stage 与交付镜像归档 exact set 不一致。"
        )
    expected_documents = _load_expected_documents(repository_root)
    corpus = _validate_corpus(
        corpus_root,
        expected_git_sha=git_sha,
        expected_documents=expected_documents,
    )
    release_id = f"{git_sha[:12]}-{corpus.sha256[:12]}"
    _copy_release_files(
        repository_root=repository_root,
        stage=stage,
        release_id=release_id,
        git_sha=git_sha,
        images=images,
        release_kind=release_kind,
    )
    config_identity = _build_config(
        repository_root=repository_root,
        stage=stage,
        active_documents=corpus.active_documents,
    )
    _copy_validation(repository_root, stage)
    _build_corpus_archive(
        corpus_root=corpus_root,
        destination=stage / "corpus.tar.gz",
        source_date_epoch=source_date_epoch,
    )
    for image in images:
        if not isinstance(image, ImageArtifact):
            continue
        archive = stage / image.archive_name
        if not archive.is_file() or _sha256(archive) != image.archive_sha256:
            raise IndustryReleaseError("镜像归档与已验证身份不一致。")
    for archive_name in (*sorted(expected_archives), "corpus.tar.gz"):
        write_sha256_sidecar(stage / archive_name)
    _normalize_release_modes(stage)
    generated_at = _generated_at(source_date_epoch)
    manifest = _release_manifest(
        stage=stage,
        release_id=release_id,
        git_sha=git_sha,
        main_sha=main_sha,
        source_date_epoch=source_date_epoch,
        generated_at=generated_at,
        images=images,
        corpus=corpus,
        config_identity=config_identity,
    )
    _write_json(stage / "RELEASE_MANIFEST.json", manifest)
    _write_sha256sums(stage)
    _normalize_release_modes(stage)
    try:
        verify_release(stage)
        _compose_config(stage)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise IndustryReleaseError(
            "release package selfcheck 失败。"
        ) from error
    return ReleaseIdentity(release_id=release_id, release_dir=stage)


def build_outer_upload(
    *,
    release_dir: Path,
    upload_parent: Path,
    source_date_epoch: int,
) -> ReleaseIdentity:
    """生成唯一外层上传目录、sidecar 并 fresh extract 验证。

    Args:
        release_dir: 已通过 package selfcheck 的最终 release 目录。
        upload_parent: `industry-upload` 的父目录。
        source_date_epoch: tar 成员固定时间戳。

    Returns:
        带外层归档与 sidecar 的 release 身份。

    Raises:
        IndustryReleaseError: 输出已存在或 fresh verify 失败。

    """
    verify_release(release_dir)
    release_id = release_dir.name
    final_upload = upload_parent / "industry-upload"
    if final_upload.exists() or final_upload.is_symlink():
        raise IndustryReleaseError("industry-upload 已存在，拒绝覆盖。")
    upload_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=upload_parent,
        prefix=".industry-upload-",
    ) as temporary_name:
        stage = Path(temporary_name)
        manifest = _load_object(release_dir / "RELEASE_MANIFEST.json")
        release_kind = manifest.get("release_kind")
        prefix = (
            "rag-industry-first-deploy-reuse-images"
            if release_kind == _REUSE_RELEASE_KIND
            else "rag-industry-first-deploy"
        )
        archive_name = f"{prefix}-{release_id}.tar.gz"
        archive = stage / archive_name
        _build_outer_archive(
            release_dir=release_dir,
            destination=archive,
            source_date_epoch=source_date_epoch,
        )
        sidecar = write_sha256_sidecar(archive)
        with tempfile.TemporaryDirectory(
            dir=upload_parent,
            prefix=".industry-fresh-",
        ) as fresh_name:
            extracted = verify_outer_archive(
                archive,
                sidecar,
                Path(fresh_name) / "extract",
            )
            _compose_config(extracted)
        try:
            publish_directory(stage, final_upload)
        except (FileExistsError, OSError) as error:
            raise IndustryReleaseError("outer upload 原子发布失败。") from error
    return ReleaseIdentity(
        release_id=release_id,
        release_dir=release_dir,
        outer_archive=final_upload / archive_name,
        outer_sidecar=final_upload / f"{archive_name}.sha256",
    )


def _validate_corpus(
    root: Path,
    *,
    expected_git_sha: str,
    expected_documents: tuple[str, ...],
) -> _CorpusIdentity:
    if not root.is_dir() or root.is_symlink():
        raise IndustryReleaseError("corpus root 必须是真实目录。")
    expected_top = {
        "docs",
        "reference",
        "industry-corpus-manifest.json",
        "industry-corpus-audit.json",
    }
    if {path.name for path in root.iterdir()} != expected_top:
        raise IndustryReleaseError("corpus root exact set 不一致。")
    manifest_path = root / "industry-corpus-manifest.json"
    manifest = _load_object(manifest_path)
    audit = _load_object(root / "industry-corpus-audit.json")
    documents = manifest.get("documents")
    active = manifest.get("active_documents")
    reference = manifest.get("reference_documents")
    audit_documents = audit.get("documents")
    if (
        manifest.get("schema_version") != "1"
        or set(manifest) != _CORPUS_MANIFEST_FIELDS
        or manifest.get("generated_from_git_sha") != expected_git_sha
        or not isinstance(documents, list)
        or not isinstance(active, list)
        or not isinstance(reference, list)
        or manifest.get("active_document_count") != _EXPECTED_ACTIVE_COUNT
        or manifest.get("reference_document_count") != 0
        or reference
        or set(audit) != _CORPUS_AUDIT_FIELDS
        or audit.get("schema_version") != "1"
        or audit.get("corpus_revision") != manifest.get("corpus_revision")
        or not isinstance(audit_documents, list)
    ):
        raise IndustryReleaseError("corpus manifest release 身份无效。")
    active_names = tuple(
        PurePosixPath(value).name
        for value in active
        if isinstance(value, str)
    )
    if active_names != expected_documents:
        raise IndustryReleaseError("corpus active 文件名集合无效。")
    identities: list[dict[str, object]] = []
    manifest_paths: set[str] = set()
    for item in documents:
        if not isinstance(item, dict) or set(item) != _CORPUS_DOCUMENT_FIELDS:
            raise IndustryReleaseError("corpus document schema 无效。")
        relative = item.get("target_relative_path")
        digest = item.get("target_sha256")
        size = item.get("target_size")
        name = PurePosixPath(relative).name if isinstance(relative, str) else ""
        source_name = name.removesuffix("x")
        if (
            not isinstance(relative, str)
            or not relative.startswith("docs/")
            or not isinstance(digest, str)
            or not isinstance(size, int)
            or item.get("canonical_name") != source_name
            or item.get("role") != "active"
        ):
            raise IndustryReleaseError("corpus document 身份字段无效。")
        path = root.joinpath(*PurePosixPath(relative).parts)
        if (
            relative in manifest_paths
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != size
            or _sha256(path) != digest
        ):
            raise IndustryReleaseError("corpus DOCX SHA/size 不一致。")
        manifest_paths.add(relative)
        identities.append({"path": relative, "sha256": digest, "size": size})
    audit_paths = {
        item.get("target_relative_path")
        for item in audit_documents
        if isinstance(item, dict)
    }
    if (
        len(audit_paths) != _EXPECTED_ACTIVE_COUNT
        or audit_paths != manifest_paths
    ):
        raise IndustryReleaseError("corpus audit 与 manifest 不一致。")
    actual_docs = {
        f"docs/{path.name}"
        for path in (root / "docs").iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if (
        manifest_paths != set(active)
        or manifest_paths != actual_docs
        or tuple((root / "reference").iterdir())
        or tuple(root.rglob("*.doc"))
    ):
        raise IndustryReleaseError("corpus active/reference exact set 无效。")
    corpus_sha = _canonical_digest(identities)
    if (
        manifest.get("corpus_sha256") != corpus_sha
        or manifest.get("corpus_revision") != corpus_sha[:16]
    ):
        raise IndustryReleaseError("corpus 联合摘要或 revision 无效。")
    source_epoch = manifest.get("source_date_epoch")
    if not isinstance(source_epoch, int) or source_epoch < 0:
        raise IndustryReleaseError("corpus source_date_epoch 无效。")
    return _CorpusIdentity(
        revision=corpus_sha[:16],
        sha256=corpus_sha,
        manifest_sha256=_sha256(manifest_path),
        active_documents=active_names,
        active_count=_EXPECTED_ACTIVE_COUNT,
        reference_count=0,
        source_date_epoch=source_epoch,
    )


def _load_expected_documents(repository_root: Path) -> tuple[str, ...]:
    expected = _load_object(
        repository_root / "evaluation/industry/expected-corpus.json"
    )
    active = expected.get("active_documents")
    reference = expected.get("reference_documents")
    if (
        expected.get("schema_version") != "1"
        or not isinstance(active, list)
        or len(active) != _EXPECTED_ACTIVE_COUNT
        or not all(
            isinstance(name, str)
            and PurePosixPath(name).name == name
            and name.startswith("GM-")
            and name.endswith(".docx")
            for name in active
        )
        or len(set(active)) != _EXPECTED_ACTIVE_COUNT
        or reference != []
    ):
        raise IndustryReleaseError("expected corpus 清单无效。")
    return tuple(active)


def _build_config(
    *,
    repository_root: Path,
    stage: Path,
    active_documents: tuple[str, ...],
) -> dict[str, str]:
    authoritative = repository_root / "deployment/config"
    destination = stage / "config"
    destination.mkdir()
    for name in _CONFIG_FILES[:-1]:
        shutil.copyfile(authoritative / name, destination / name)
    retrieval = _load_object(destination / "retrieval.json")
    router = _load_object(destination / "intent-router.json")
    calibration = _load_object(destination / "intent-router-calibration.json")
    if retrieval.get("status") != "provisional" or retrieval.get(
        "soft_routes"
    ) != []:
        raise IndustryReleaseError(
            "Industry retrieval 必须 provisional/无 soft route。"
        )
    retrieval["allowed_authority_levels"] = ["official", "verified"]
    _write_json(destination / "retrieval.json", retrieval)
    if router.get("mode") != "shadow":
        raise IndustryReleaseError("Industry intent router 必须保持 shadow。")
    if calibration.get("status") != "unverified":
        raise IndustryReleaseError("Industry calibration 必须保持 unverified。")
    policy = {
        "defaults": {
            "authority_level": "official",
            "document_status": "active",
            "effective_from": None,
            "effective_to": None,
        },
        "overrides": [
            {
                "authority_level": "verified",
                "document_status": "active",
                "effective_from": None,
                "effective_to": None,
                "path": path,
            }
            for path in active_documents
        ],
        "schema_version": "1",
    }
    policy_path = destination / "corpus-policy.json"
    pipeline_path = destination / "pipeline.json"
    _write_json(policy_path, policy)
    loaded_policy = CorpusPolicy.load(policy_path)
    pipeline_payload = _load_object(pipeline_path)
    if not isinstance(pipeline_payload.get("corpus_policy_sha256"), str):
        raise IndustryReleaseError(
            "pipeline 缺少 corpus policy SHA256 绑定。"
        )
    pipeline_payload["corpus_policy_sha256"] = (
        loaded_policy.semantic_sha256()
    )
    _write_json(pipeline_path, pipeline_payload)
    pipeline = load_pipeline(pipeline_path)
    retrieval_settings = RetrievalSettings.load(destination / "retrieval.json")
    identity = {
        name: _sha256(destination / name) for name in _CONFIG_FILES
    }
    identity["pipeline_fingerprint"] = pipeline.fingerprint()
    identity["serving_fingerprint"] = retrieval_settings.serving_fingerprint(
        pipeline
    )
    if len(loaded_policy.overrides) != _EXPECTED_ACTIVE_COUNT or any(
        item.authority_level != "verified" for item in loaded_policy.overrides
    ):
        raise IndustryReleaseError(
            "Industry corpus policy exact override 无效。"
        )
    return identity


def _copy_release_files(  # noqa: PLR0913
    *,
    repository_root: Path,
    stage: Path,
    release_id: str,
    git_sha: str,
    images: tuple[ImageIdentity, ImageIdentity, ImageIdentity],
    release_kind: str,
) -> None:
    source_root = repository_root / "deployment/industry"
    image_by_name = {image.name: image for image in images}
    if set(image_by_name) != {"app", "ocr", "qdrant"}:
        raise IndustryReleaseError("镜像身份集合必须是 app/OCR/Qdrant。")
    for name in _RELEASE_FILES:
        source_name = (
            "package-contract-reuse-images.json"
            if name == "package-contract.json"
            and release_kind == _REUSE_RELEASE_KIND
            else name
        )
        if name == "last_good.py":
            source_name = "serving_last_good.py"
        source = source_root / source_name
        if not source.is_file() or source.is_symlink():
            raise IndustryReleaseError(f"Industry 部署文件缺失：{name}")
        shutil.copyfile(source, stage / name)
    env_path = stage / ".env.example"
    env = env_path.read_text(encoding="utf-8")
    replacements = {
        "REPLACE_APP_IMAGE": image_by_name["app"].ref,
        "REPLACE_OCR_IMAGE": image_by_name["ocr"].ref,
        "REPLACE_QDRANT_IMAGE": image_by_name["qdrant"].ref,
        "REPLACE_FULL_GIT_SHA": git_sha,
        "REPLACE_RELEASE_ID": release_id,
    }
    for marker, value in replacements.items():
        if marker not in env:
            raise IndustryReleaseError(f"env 模板缺少 marker：{marker}")
        env = env.replace(marker, value)
    env_path.write_text(env, encoding="utf-8")
    commands_path = stage / "SERVER_UPLOAD_COMMANDS.txt"
    commands = commands_path.read_text(encoding="utf-8")
    if (
        "REPLACE_RELEASE_ID" not in commands
        or "REPLACE_ARCHIVE_PREFIX" not in commands
    ):
        raise IndustryReleaseError("上传命令缺少 archive marker。")
    archive_prefix = (
        "rag-industry-first-deploy-reuse-images"
        if release_kind == _REUSE_RELEASE_KIND
        else "rag-industry-first-deploy"
    )
    commands_path.write_text(
        commands.replace("REPLACE_RELEASE_ID", release_id).replace(
            "REPLACE_ARCHIVE_PREFIX",
            archive_prefix,
        ),
        encoding="utf-8",
    )


def _copy_validation(repository_root: Path, stage: Path) -> None:
    validation = stage / "validation"
    validation.mkdir()
    source = repository_root / "evaluation/industry"
    shutil.copyfile(source / "smoke.jsonl", validation / "industry-smoke.jsonl")
    shutil.copyfile(
        source / "expected-corpus.json",
        validation / "expected-corpus.json",
    )


def _build_corpus_archive(
    *,
    corpus_root: Path,
    destination: Path,
    source_date_epoch: int,
) -> None:
    members = [corpus_root / "docs", corpus_root / "reference"]
    members.extend(
        (
            corpus_root / "industry-corpus-manifest.json",
            corpus_root / "industry-corpus-audit.json",
        )
    )
    _build_deterministic_tar(
        root=corpus_root,
        members=tuple(members),
        destination=destination,
        source_date_epoch=source_date_epoch,
        top_level=None,
    )


def _build_outer_archive(
    *,
    release_dir: Path,
    destination: Path,
    source_date_epoch: int,
) -> None:
    _build_deterministic_tar(
        root=release_dir,
        members=(release_dir,),
        destination=destination,
        source_date_epoch=source_date_epoch,
        top_level=release_dir.name,
    )


def _build_deterministic_tar(
    *,
    root: Path,
    members: tuple[Path, ...],
    destination: Path,
    source_date_epoch: int,
    top_level: str | None,
) -> None:
    raw = destination.with_suffix("")
    with tarfile.open(raw, mode="x:") as archive:
        paths: list[Path] = []
        for member in members:
            paths.append(member)
            if member.is_dir():
                paths.extend(member.rglob("*"))
        for path in sorted(
            set(paths),
            key=lambda item: (
                "" if item == root else item.relative_to(root).as_posix()
            ),
        ):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise IndustryReleaseError("tar 输入含 symlink 或特殊文件。")
            relative = "" if path == root else path.relative_to(root).as_posix()
            arcname = (
                PurePosixPath(top_level, relative).as_posix()
                if top_level is not None
                else relative
            )
            if not arcname:
                continue
            info = archive.gettarinfo(str(path), arcname=arcname)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = source_date_epoch
            info.mode = (
                0o700 if path.is_dir() else stat.S_IMODE(path.stat().st_mode)
            )
            if path.is_file():
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    with (
        raw.open("rb") as source,
        destination.open("xb") as output,
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed,
    ):
        shutil.copyfileobj(source, compressed, length=_HASH_BLOCK_BYTES)
    raw.unlink()


def _release_manifest(  # noqa: PLR0913
    *,
    stage: Path,
    release_id: str,
    git_sha: str,
    main_sha: str,
    source_date_epoch: int,
    generated_at: str,
    images: tuple[ImageIdentity, ImageIdentity, ImageIdentity],
    corpus: _CorpusIdentity,
    config_identity: dict[str, str],
) -> dict[str, object]:
    release_kind = _release_kind(images)
    reuse_images = release_kind == _REUSE_RELEASE_KIND
    payload_files = _file_identities(stage)
    return {
        "builder_revision": (
            _REUSE_BUILDER_REVISION if reuse_images else _BUILDER_REVISION
        ),
        "calibration_status": "unverified",
        "commit_timestamp": source_date_epoch,
        "compose": {
            "alias": "rag-industry-active",
            "host_port": 8188,
            "ocr_modes": ["dedicated", "external"],
            "project": "rag-industry",
            "services": [
                "rag-industry-app",
                "rag-industry-worker",
                "rag-industry-qdrant",
                "rag-industry-ocr",
            ],
        },
        "config_sha256": {
            name: config_identity[name] for name in _CONFIG_FILES
        },
        "corpus": {
            "active_count": corpus.active_count,
            "manifest_sha256": corpus.manifest_sha256,
            "reference_count": corpus.reference_count,
            "revision": corpus.revision,
            "sha256": corpus.sha256,
        },
        "dirty": False,
        "generated_at": generated_at,
        "git_branch": "Industry",
        "git_sha": git_sha,
        "images": {
            image.name: image.manifest_dict() for image in images
        },
        "intent_router_mode": "shadow",
        "main_sha_at_build": main_sha,
        "package_contract_revision": (
            _REUSE_CONTRACT_REVISION
            if reuse_images
            else _PACKAGE_CONTRACT_REVISION
        ),
        "payload_files": payload_files,
        "pipeline_fingerprint": config_identity["pipeline_fingerprint"],
        "release_id": release_id,
        "release_kind": release_kind,
        "schema_version": "1",
        "serving_fingerprint": config_identity["serving_fingerprint"],
        "source_date_epoch": source_date_epoch,
        "source_revision": git_sha,
    }


def _release_kind(
    images: tuple[ImageIdentity, ImageIdentity, ImageIdentity],
) -> str:
    image_by_name = {image.name: image for image in images}
    if set(image_by_name) != _IMAGE_NAMES:
        raise IndustryReleaseError("镜像身份集合必须是 app/OCR/Qdrant。")
    if all(isinstance(image, ImageArtifact) for image in images):
        return _FULL_RELEASE_KIND
    if (
        isinstance(image_by_name["app"], ImageArtifact)
        and isinstance(image_by_name["ocr"], ExistingImageIdentity)
        and isinstance(image_by_name["qdrant"], ExistingImageIdentity)
    ):
        return _REUSE_RELEASE_KIND
    raise IndustryReleaseError("镜像交付模式必须是完整归档或复用服务器镜像。")


def _file_identities(root: Path) -> list[dict[str, object]]:
    return [
        {
            "mode": stat.S_IMODE(path.stat().st_mode),
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
        and path.name not in {"RELEASE_MANIFEST.json", "SHA256SUMS"}
    ]


def _write_sha256sums(root: Path) -> None:
    files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.name != "SHA256SUMS"
        ),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    (root / "SHA256SUMS").write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n"
            for path in files
        ),
        encoding="ascii",
    )


def _normalize_release_modes(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_dir() or path.name in _EXECUTABLE_FILES:
            path.chmod(0o700)
        else:
            path.chmod(0o600)


def _compose_config(root: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise IndustryReleaseError("缺少 docker，无法验证 Compose 配置。")
    subprocess.run(  # noqa: S603
        (
            docker,
            "compose",
            "--env-file",
            str(root / ".env.example"),
            "-f",
            str(root / "compose.yaml"),
            "--profile",
            "index",
            "--profile",
            "dedicated-ocr",
            "config",
            "-q",
        ),
        check=True,
        capture_output=True,
    )


def _generated_at(source_date_epoch: int) -> str:
    generated = datetime.fromtimestamp(source_date_epoch, tz=UTC).isoformat()
    return generated.replace(
        "+00:00",
        "Z",
    )


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json(value))
    path.chmod(0o600)


def _load_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IndustryReleaseError(f"JSON 无效：{path.name}") from error
    if not isinstance(value, dict):
        raise IndustryReleaseError(f"JSON 顶层必须是对象：{path.name}")
    return value


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_HASH_BLOCK_BYTES):
            digest.update(block)
    return digest.hexdigest()
