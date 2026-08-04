"""构建只包含已验证 app 镜像的模块化更新包。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from rag_app.contracts import PipelineSpec
from rag_app.settings import RetrievalSettings
from scripts.docker_archive_identity import (
    DockerArchiveIdentityError,
    inspect_docker_archive,
)
from scripts.offline_bundle import publish_directory
from scripts.prepare_runtime_wheels import (
    _build_project_wheel,
    _copy_tracked_source,
    _transactional_replace,
    _wheel_files,
    _write_wheel_contract,
    verify_project_wheel,
)

_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
_PROJECT_WHEEL = re.compile(r"^docx_rag-0\.1\.0-.*\.whl$")
_SHA_LINE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
_PLATFORM = "linux/amd64"


class AppUpdateError(RuntimeError):
    """表示 app 更新包无法安全生成。"""


@dataclass(frozen=True)
class ChangeClassification:
    """保存 base 到 target 的更新路径分类结果。"""

    allowed: bool
    categories: tuple[str, ...]
    rejected_paths: tuple[str, ...]


@dataclass(frozen=True)
class _Fingerprints:
    """保存单个 revision 的 index/serving 兼容指纹。"""

    index: str
    serving: str


@dataclass(frozen=True)
class _BuildContext:
    """保存 app 镜像构建阶段的非敏感输入。"""

    root: Path
    stage: Path
    base: str
    target: str
    path_count: int
    classification: ChangeClassification
    base_fingerprints: _Fingerprints
    target_fingerprints: _Fingerprints


def classify_changed_paths(paths: Sequence[str]) -> ChangeClassification:
    """判断 Git 目标路径能否由 app update 承载。

    Args:
        paths: base 到 target 的变更目标路径。

    Returns:
        稳定排序的允许类别和拒绝路径。

    Raises:
        AppUpdateError: 路径为空、绝对、越界或格式不规范。

    """
    if not paths:
        raise AppUpdateError("base 到 target 没有可发布变更。")
    categories: set[str] = set()
    rejected: set[str] = set()
    for value in paths:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or "." in path.parts
            or ".." in path.parts
            or "\\" in value
        ):
            raise AppUpdateError(f"Git 变更路径不规范：{value}")
        category = _allowed_category(path.as_posix())
        if category is None:
            rejected.add(value)
        else:
            categories.add(category)
    return ChangeClassification(
        allowed=not rejected,
        categories=tuple(sorted(categories)),
        rejected_paths=tuple(sorted(rejected)),
    )


def build_update(
    *,
    repository_root: Path,
    base_revision: str,
    output_parent: Path | None = None,
) -> Path:
    """从 clean HEAD 构建四文件 app 镜像更新包。

    Args:
        repository_root: 当前项目 Git 根目录。
        base_revision: 服务器基础 release 的完整 SOURCE_REVISION。
        output_parent: 可选输出父目录；默认位于 ignored artifacts。

    Returns:
        已原子发布且恰含四个文件的更新目录。

    Raises:
        AppUpdateError: Git、变更、构建或包身份不符合契约。

    """
    root = repository_root.resolve(strict=True)
    target = _git_preflight(root, base_revision)
    changed_paths = _changed_paths(root, base_revision, target)
    classification = classify_changed_paths(changed_paths)
    if not classification.allowed:
        raise AppUpdateError(
            "变更需要生成完整 release，拒绝 app update："
            + ", ".join(classification.rejected_paths)
        )
    base_fingerprints = _fingerprints(root, base_revision)
    target_fingerprints = _fingerprints(root, target)
    _prepare_project_wheel(root, target)
    parent = output_parent or root / "artifacts/app-updates"
    parent.mkdir(parents=True, exist_ok=True)
    parent = parent.resolve(strict=True)
    if parent.is_symlink():
        raise AppUpdateError("app update 输出父目录不能是符号链接。")
    final = parent / target[:12]
    if final.exists() or final.is_symlink():
        raise AppUpdateError(f"app update 输出已存在：{final}")
    with tempfile.TemporaryDirectory(
        dir=parent,
        prefix=f".{target[:12]}.",
    ) as temporary_name:
        stage = Path(temporary_name)
        _build_package(
            _BuildContext(
                root=root,
                stage=stage,
                base=base_revision,
                target=target,
                path_count=len(changed_paths),
                classification=classification,
                base_fingerprints=base_fingerprints,
                target_fingerprints=target_fingerprints,
            )
        )
        _verify_package(stage, target)
        publish_directory(stage, final)
    return final


def _allowed_category(path: str) -> str | None:
    """返回路径的 app 类别，完整发布路径返回 None。"""
    if path.startswith("src/rag_app/"):
        category = "app_python"
    elif path.startswith("frontend/"):
        category = "frontend"
    elif path == "Dockerfile":
        category = "app_build"
    elif path in {
        "pyproject.toml",
        "requirements.lock",
        "requirements.runtime.lock",
    }:
        category = "app_dependencies"
    elif path == "deployment/ASSETS.sha256" \
            or path.startswith("deployment/assets/"):
        category = "app_assets"
    elif path in {
        "deployment/config/pipeline.json",
        "deployment/config/retrieval.json",
    }:
        category = "app_serving_config"
    elif path.startswith("tests/") or path in {
        "PROGRESS.md",
        "BLOCKED.md",
    }:
        category = "verification_only"
    else:
        category = None
    return category


def _git_preflight(root: Path, base: str) -> str:
    """校验 clean HEAD 和 base 祖先关系。"""
    if _FULL_REVISION.fullmatch(base) is None:
        raise AppUpdateError("base revision 必须是完整 40 位小写 SHA。")
    if _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    ):
        raise AppUpdateError("构建 app update 要求 Git 工作区 clean。")
    target = _git_output(root, "rev-parse", "HEAD").strip()
    try:
        resolved = _git_output(root, "rev-parse", f"{base}^{{commit}}").strip()
        _git_output(root, "merge-base", "--is-ancestor", base, target)
    except subprocess.CalledProcessError as error:
        raise AppUpdateError(
            "base revision 必须存在且是 target revision 的祖先。"
        ) from error
    if resolved != base or _FULL_REVISION.fullmatch(target) is None:
        raise AppUpdateError("base/target revision 无效。")
    return target


def _changed_paths(root: Path, base: str, target: str) -> tuple[str, ...]:
    """读取新增、修改、删除和重命名目标路径。"""
    fields = _git_output(
        root,
        "diff",
        "--name-status",
        "--find-renames",
        "-z",
        base,
        target,
    ).split("\0")
    if fields and not fields[-1]:
        fields.pop()
    paths: list[str] = []
    position = 0
    while position < len(fields):
        status = fields[position]
        position += 1
        rename = status.startswith(("R", "C"))
        required = 2 if rename else 1
        if not status or position + required > len(fields):
            raise AppUpdateError("Git name-status 输出不完整。")
        position += int(rename)
        paths.append(fields[position])
        position += 1
    return tuple(sorted(set(paths)))


def _fingerprints(root: Path, revision: str) -> _Fingerprints:
    """计算指定 revision 的 index/serving 指纹。"""
    try:
        pipeline = PipelineSpec.model_validate_json(
            _git_output(
                root,
                "show",
                f"{revision}:deployment/config/pipeline.json",
            )
        )
        retrieval = RetrievalSettings.model_validate_json(
            _git_output(
                root,
                "show",
                f"{revision}:deployment/config/retrieval.json",
            )
        )
    except (subprocess.CalledProcessError, ValidationError) as error:
        raise AppUpdateError(
            "base/target app 配置缺失或不兼容，必须完整发布。"
        ) from error
    return _Fingerprints(
        pipeline.index_fingerprint(),
        retrieval.serving_fingerprint(pipeline),
    )


def _prepare_project_wheel(root: Path, revision: str) -> None:
    """复用固定依赖 wheel，仅重建当前项目 wheel。"""
    wheelhouse = root / "deployment/runtime/wheelhouse"
    manifest = root / "deployment/runtime/WHEELS.sha256"
    metadata = root / "deployment/runtime/PROJECT_WHEEL.json"
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise AppUpdateError("缺少已固定的 runtime wheelhouse。")
    with tempfile.TemporaryDirectory(
        dir=wheelhouse.parent,
        prefix=".app-wheel-",
    ) as temporary_name:
        work = Path(temporary_name)
        source = work / "source"
        staged = work / "wheelhouse"
        staged.mkdir()
        _copy_tracked_source(root, source)
        (source / "src/rag_app/_build_revision.py").write_text(
            f'SOURCE_REVISION = "{revision}"\n',
            encoding="ascii",
        )
        _build_project_wheel(source, staged)
        projects = tuple(staged.glob("docx_rag-*.whl"))
        if len(projects) != 1:
            raise AppUpdateError("项目 wheel 构建结果必须恰有一个。")
        verify_project_wheel(projects[0], expected_revision=revision)
        for wheel in _wheel_files(wheelhouse):
            if _PROJECT_WHEEL.fullmatch(wheel.name) is None:
                shutil.copyfile(wheel, staged / wheel.name)
        staged_manifest = work / manifest.name
        staged_metadata = work / metadata.name
        _write_wheel_contract(
            wheelhouse=staged,
            manifest_path=staged_manifest,
            metadata_path=staged_metadata,
            revision=revision,
        )
        _transactional_replace(
            (
                (staged, wheelhouse),
                (staged_manifest, manifest),
                (staged_metadata, metadata),
            ),
            backup_dir=work / "backup",
        )


def _build_package(context: _BuildContext) -> None:
    """构建、自检、保存单个 app 镜像并写元数据。"""
    short = context.target[:12]
    tag = f"docx-rag:{short}"
    archive_name = f"docx-rag-app-{short}.tar.gz"
    raw_archive = context.stage / ".app-image.tar"
    archive = context.stage / archive_name
    _run_checked(
        (
            "docker", "buildx", "build", "--network", "none",
            "--platform", _PLATFORM, "--load", "--build-arg",
            f"VCS_REF={context.target}", "--tag", tag, ".",
        ),
        root=context.root,
    )
    _run_checked(
        ("docker", "run", "--rm", "--network", "none", tag,
         "asset-selfcheck"),
        root=context.root,
    )
    _run_checked(
        ("docker", "image", "save", "--platform", _PLATFORM,
         "--output", str(raw_archive), tag),
        root=context.root,
    )
    try:
        identity = inspect_docker_archive(
            raw_archive,
            expected_tag=tag,
            expected_platform=_PLATFORM,
            expected_revision=context.target,
        )
    except DockerArchiveIdentityError as error:
        raise AppUpdateError("app Docker 归档身份无效。") from error
    with (
        raw_archive.open("rb") as source,
        archive.open("xb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as output,
    ):
        shutil.copyfileobj(source, output, length=1024 * 1024)
    raw_archive.unlink()
    metadata = {
        "archive": archive_name,
        "base_revision": context.base,
        "change_categories": list(context.classification.categories),
        "changed_path_count": context.path_count,
        "config_digest": identity.config_digest,
        "image_tag": tag,
        "index_fingerprint": {
            "base": context.base_fingerprints.index,
            "target": context.target_fingerprints.index,
        },
        "manifest_digest": identity.manifest_digest,
        "platform": identity.platform,
        "reindex_required": (
            context.base_fingerprints.index
            != context.target_fingerprints.index
        ),
        "schema_version": "1",
        "serving_fingerprint": {
            "base": context.base_fingerprints.serving,
            "target": context.target_fingerprints.serving,
        },
        "target_revision": context.target,
    }
    (context.stage / "APP_UPDATE.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (context.stage / f"{archive_name}.sha256").write_text(
        f"{_sha256(archive)}  {archive_name}\n",
        encoding="ascii",
    )
    _write_manifest(context.stage)


def _write_manifest(stage: Path) -> None:
    """写入三项 payload 的 exact-set manifest。"""
    files = sorted(
        (
            path for path in stage.iterdir()
            if path.name != "APP_UPDATE_MANIFEST.sha256"
        ),
        key=lambda item: item.name,
    )
    (stage / "APP_UPDATE_MANIFEST.sha256").write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in files),
        encoding="ascii",
    )


def _verify_package(stage: Path, target: str) -> None:
    """复核四文件 exact set 与全部 SHA256。"""
    archive = f"docx-rag-app-{target[:12]}.tar.gz"
    expected = {
        "APP_UPDATE.json",
        "APP_UPDATE_MANIFEST.sha256",
        archive,
        f"{archive}.sha256",
    }
    if {path.name for path in stage.iterdir()} != expected or any(
        not path.is_file() or path.is_symlink() for path in stage.iterdir()
    ):
        raise AppUpdateError("app update 输出必须恰有四个普通文件。")
    entries: dict[str, str] = {}
    for line in (stage / "APP_UPDATE_MANIFEST.sha256").read_text(
        encoding="ascii"
    ).splitlines():
        match = _SHA_LINE.fullmatch(line)
        if match is None or match.group(2) in entries:
            raise AppUpdateError("APP_UPDATE_MANIFEST.sha256 格式无效。")
        entries[match.group(2)] = match.group(1)
    if set(entries) != expected - {"APP_UPDATE_MANIFEST.sha256"}:
        raise AppUpdateError("APP_UPDATE_MANIFEST.sha256 文件集合无效。")
    if any(_sha256(stage / name) != digest for name, digest in entries.items()):
        raise AppUpdateError("app update 文件 SHA256 不一致。")


def _sha256(path: Path) -> str:
    """流式计算普通文件 SHA256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _git_output(root: Path, *arguments: str) -> str:
    """运行 Git 并返回标准输出。"""
    git = shutil.which("git")
    if git is None:
        raise AppUpdateError("缺少 Git 可执行文件。")
    completed = subprocess.run(  # noqa: S603
        [git, "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _run_checked(arguments: tuple[str, ...], *, root: Path) -> str:
    """运行构建命令，失败时收敛为 app update 异常。"""
    try:
        subprocess.run(list(arguments), cwd=root, check=True)  # noqa: S603
    except (OSError, subprocess.CalledProcessError) as error:
        raise AppUpdateError(f"命令执行失败：{arguments[0]}") from error
    return ""


def _arguments() -> argparse.Namespace:
    """解析 app update 构建参数。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-revision", required=True)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output-parent", type=Path)
    return parser.parse_args()


def main() -> int:
    """构建 app 镜像增量更新包。

    Args:
        无参数。

    Returns:
        成功返回 0，契约失败返回 1。

    """
    arguments = _arguments()
    try:
        output = build_update(
            repository_root=arguments.repository_root,
            base_revision=arguments.base_revision,
            output_parent=arguments.output_parent,
        )
    except AppUpdateError as error:
        print(f"APP_UPDATE_BUILD_FAILED: {error}", file=sys.stderr)
        return 1
    print(f"app_update_dir={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
