"""生成四模块简单部署包。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.prepare_runtime_wheels import (  # noqa: E402
    _copy_tracked_source,
    _transactional_replace,
    _wheel_files,
    _write_wheel_contract,
    verify_project_wheel,
)

__all__ = [
    "SimpleBuildError",
    "build_app_archive",
    "build_simple_bundle",
    "prepare_project_wheel",
    "require_clean_revision",
    "write_sha256_sidecar",
]

_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")
_PROJECT_WHEEL = re.compile(r"^docx_rag-0\.1\.0-.*\.whl$")
_PLATFORM = "linux/amd64"
_PYTHON_IMAGE_ID = (
    "sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1"
)
_PYTHON_LOCAL_IMAGE = "python:3.11.13-slim-bookworm"
_QDRANT_PINNED_IMAGE = (
    "qdrant/qdrant:v1.18.3@sha256:"
    "0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286"
)
_QDRANT_SAVE_IMAGE = "qdrant/qdrant:v1.18.3"
_SIMPLE_FILES = (
    "compose.yaml",
    ".env.example",
    "deploy.sh",
    "update-app.sh",
    "DEPLOYMENT_GUIDE.md",
)
_PACKAGE_ARCHIVES = (
    "app-image.tar.gz",
    "ocr-image.tar.gz",
    "qdrant-image.tar.gz",
    "corpus.tar.gz",
)


class SimpleBuildError(RuntimeError):
    """表示 simple 部署包无法生成。"""


def require_clean_revision(repository_root: Path) -> str:
    """要求 clean Git 并返回完整 HEAD。

    Args:
        repository_root: 当前项目 Git 根目录。

    Returns:
        完整的 40 位小写 Git SHA。

    Raises:
        SimpleBuildError: 工作区不干净或 HEAD 无效。

    """
    status = _git_output(
        repository_root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    if status:
        raise SimpleBuildError("构建 simple 包要求 Git 工作区 clean。")
    revision = _git_output(repository_root, "rev-parse", "HEAD").strip()
    if _FULL_REVISION.fullmatch(revision) is None:
        raise SimpleBuildError("Git HEAD 必须是完整 40 位小写 SHA。")
    return revision


def prepare_project_wheel(repository_root: Path, revision: str) -> None:
    """复用离线依赖 wheel，仅重建当前项目 wheel。

    Args:
        repository_root: 当前项目根目录。
        revision: 要写入 wheel 的完整 Git SHA。

    Returns:
        无返回值；成功后原子替换 ignored wheelhouse 三件套。

    Raises:
        SimpleBuildError: 固定依赖或项目 wheel 不完整。

    """
    wheelhouse = repository_root / "deployment/runtime/wheelhouse"
    manifest = repository_root / "deployment/runtime/WHEELS.sha256"
    metadata = repository_root / "deployment/runtime/PROJECT_WHEEL.json"
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise SimpleBuildError("缺少本地固定 runtime wheelhouse。")
    with tempfile.TemporaryDirectory(
        dir=wheelhouse.parent,
        prefix=".simple-wheel-",
    ) as temporary_name:
        work = Path(temporary_name)
        source = work / "source"
        staged = work / "wheelhouse"
        staged.mkdir()
        _copy_tracked_source(repository_root, source)
        (source / "src/rag_app/_build_revision.py").write_text(
            f'SOURCE_REVISION = "{revision}"\n',
            encoding="ascii",
        )
        _build_local_project_wheel(source, staged)
        project_wheels = tuple(staged.glob("docx_rag-*.whl"))
        if len(project_wheels) != 1:
            raise SimpleBuildError("项目 wheel 构建结果必须恰有一个。")
        verify_project_wheel(
            project_wheels[0],
            expected_revision=revision,
        )
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


def build_app_archive(
    *,
    repository_root: Path,
    revision: str,
    destination: Path,
    config_directory: Path | None = None,
    assets_manifest_path: Path | None = None,
) -> str:
    """构建、自检并保存 linux/amd64 app 镜像。

    Args:
        repository_root: Docker build context。
        revision: 绑定 wheel 与 OCI label 的完整 Git SHA。
        destination: `app-image.tar.gz` 输出路径。
        config_directory: 可选的镜像内 `deployment/config` 精确覆盖目录。
        assets_manifest_path: 与配置覆盖匹配的可选资产清单。

    Returns:
        已保存进归档的 app image tag。

    Raises:
        SimpleBuildError: Docker build、自检或保存失败。

    """
    image = f"docx-rag:{revision[:12]}"
    raw_archive = destination.with_suffix("")
    if _inspect_image_id(_PYTHON_LOCAL_IMAGE, repository_root) != (
        _PYTHON_IMAGE_ID
    ):
        raise SimpleBuildError("本地 Python tag 与固定 image ID 不一致。")
    _require_linux_amd64_image(_PYTHON_LOCAL_IMAGE, repository_root)
    with tempfile.TemporaryDirectory(
        dir=destination.parent,
        prefix=".app-context-",
    ) as temporary_name:
        context = Path(temporary_name)
        _prepare_app_build_context(
            repository_root,
            context,
            config_directory=config_directory,
            assets_manifest_path=assets_manifest_path,
        )
        _run_checked(
            (
                "/usr/bin/env",
                "DOCKER_BUILDKIT=0",
                "docker",
                "build",
                "--network",
                "none",
                "--platform",
                _PLATFORM,
                "--pull=false",
                "--build-arg",
                f"VCS_REF={revision}",
                "--build-arg",
                f"PYTHON_IMAGE={_PYTHON_LOCAL_IMAGE}",
                "--tag",
                image,
                ".",
            ),
            cwd=context,
        )
    _run_checked(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            image,
            "asset-selfcheck",
        ),
        cwd=repository_root,
    )
    _save_image(image, raw_archive, repository_root)
    _gzip_file(raw_archive, destination)
    raw_archive.unlink()
    return image


def build_simple_bundle(
    *,
    repository_root: Path,
    output_parent: Path | None = None,
    ocr_image: str | None = None,
) -> Path:
    """生成 app、OCR、Qdrant、corpus 四个独立包。

    Args:
        repository_root: 当前项目 Git 根目录。
        output_parent: 可选测试输出父目录。
        ocr_image: 可选本地固定 OCR image tag。

    Returns:
        已发布的 `simple-deploy/<SHA前12位>` 目录。

    Raises:
        SimpleBuildError: Git、镜像、DOCX 或输出不符合简单包契约。

    """
    root = repository_root.resolve(strict=True)
    revision = require_clean_revision(root)
    prepare_project_wheel(root, revision)
    parent = output_parent or root / "artifacts/simple-deploy"
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / revision[:12]
    if final.exists() or final.is_symlink():
        raise SimpleBuildError(f"simple 部署输出已存在：{final}")
    selected_ocr = ocr_image or _select_local_ocr_image(root)
    _require_linux_amd64_image(selected_ocr, root)
    _require_qdrant_image(root)
    with tempfile.TemporaryDirectory(
        dir=parent,
        prefix=f".{revision[:12]}.",
    ) as temporary_name:
        stage = Path(temporary_name)
        app_image = build_app_archive(
            repository_root=root,
            revision=revision,
            destination=stage / "app-image.tar.gz",
        )
        _save_compressed_image(
            selected_ocr,
            stage / "ocr-image.tar.gz",
            root,
        )
        _save_compressed_image(
            _QDRANT_SAVE_IMAGE,
            stage / "qdrant-image.tar.gz",
            root,
        )
        _build_corpus(root / "docs", stage / "corpus.tar.gz")
        for archive_name in _PACKAGE_ARCHIVES:
            write_sha256_sidecar(stage / archive_name)
        _copy_simple_files(
            root=root,
            stage=stage,
            revision=revision,
            app_image=app_image,
            ocr_image=selected_ocr,
        )
        _verify_simple_output(stage)
        stage.replace(final)
    return final


def write_sha256_sidecar(path: Path) -> Path:
    """为单个普通文件写标准 basename SHA256 sidecar。

    Args:
        path: 待摘要文件。

    Returns:
        新建的 `.sha256` 路径。

    Raises:
        SimpleBuildError: 输入不是非空普通文件。

    """
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise SimpleBuildError(f"无法为无效文件生成 SHA256：{path.name}")
    sidecar = path.with_name(f"{path.name}.sha256")
    sidecar.write_text(
        f"{_sha256(path)}  {path.name}\n",
        encoding="ascii",
    )
    return sidecar


def _select_local_ocr_image(root: Path) -> str:
    output = _run_output(
        (
            "docker",
            "image",
            "ls",
            "--format",
            "{{.Repository}}:{{.Tag}}",
            "docx-rag-ocr",
        ),
        cwd=root,
    )
    candidates = tuple(
        line.strip()
        for line in output.splitlines()
        if line.strip() and not line.endswith(":<none>")
    )
    if not candidates:
        raise SimpleBuildError("本地没有可复用的固定 docx-rag-ocr 镜像。")
    return candidates[0]


def _prepare_app_build_context(
    root: Path,
    destination: Path,
    *,
    config_directory: Path | None = None,
    assets_manifest_path: Path | None = None,
) -> None:
    files = (
        "Dockerfile",
        "requirements.runtime.lock",
        "deployment/ASSETS.sha256",
        "deployment/runtime/WHEELS.sha256",
    )
    directories = (
        "frontend",
        "deployment/assets",
        "deployment/config",
        "deployment/runtime/wheelhouse",
    )
    for relative in files:
        source = root / relative
        target = destination / relative
        if not source.is_file() or source.is_symlink():
            raise SimpleBuildError(f"app build 输入缺失：{relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for relative in directories:
        source = root / relative
        target = destination / relative
        if not source.is_dir() or source.is_symlink():
            raise SimpleBuildError(f"app build 目录缺失：{relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    if (config_directory is None) != (assets_manifest_path is None):
        raise SimpleBuildError(
            "app config 覆盖与资产清单必须同时提供。"
        )
    if config_directory is None or assets_manifest_path is None:
        return
    if (
        not config_directory.is_dir()
        or config_directory.is_symlink()
        or not assets_manifest_path.is_file()
        or assets_manifest_path.is_symlink()
    ):
        raise SimpleBuildError("app config 覆盖输入无效。")
    config_target = destination / "deployment/config"
    source_entries = list(config_directory.iterdir())
    target_entries = list(config_target.iterdir())
    if (
        {path.name for path in source_entries}
        != {path.name for path in target_entries}
        or any(
            not path.is_file() or path.is_symlink()
            for path in source_entries
        )
    ):
        raise SimpleBuildError("app config 覆盖 exact set 无效。")
    for source in source_entries:
        shutil.copyfile(source, config_target / source.name)
    shutil.copyfile(
        assets_manifest_path,
        destination / "deployment/ASSETS.sha256",
    )


def _build_local_project_wheel(source: Path, destination: Path) -> None:
    _run_checked(
        (
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--wheel-dir",
            str(destination),
            str(source),
        ),
        cwd=source,
    )


def _require_qdrant_image(root: Path) -> None:
    pinned_id = _inspect_image_id(_QDRANT_PINNED_IMAGE, root)
    tagged_id = _inspect_image_id(_QDRANT_SAVE_IMAGE, root)
    if pinned_id != tagged_id:
        raise SimpleBuildError("Qdrant v1.18.3 tag 与固定 digest 不一致。")
    _require_linux_amd64_image(_QDRANT_SAVE_IMAGE, root)


def _inspect_image_id(image: str, root: Path) -> str:
    value = _run_output(
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image,
        ),
        cwd=root,
    ).strip()
    if not value.startswith("sha256:"):
        raise SimpleBuildError(f"本地镜像 ID 无效：{image}")
    return value


def _require_linux_amd64_image(image: str, root: Path) -> None:
    platform = _run_output(
        (
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Os}}/{{.Architecture}}",
            image,
        ),
        cwd=root,
    ).strip()
    if platform != _PLATFORM:
        raise SimpleBuildError(f"镜像不是 linux/amd64：{image}")


def _save_compressed_image(image: str, destination: Path, root: Path) -> None:
    raw_archive = destination.with_suffix("")
    _save_image(image, raw_archive, root)
    _gzip_file(raw_archive, destination)
    raw_archive.unlink()


def _save_image(image: str, destination: Path, root: Path) -> None:
    _run_checked(
        (
            "docker",
            "image",
            "save",
            "--platform",
            _PLATFORM,
            "--output",
            str(destination),
            image,
        ),
        cwd=root,
    )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise SimpleBuildError(f"docker save 未生成归档：{image}")


def _gzip_file(source: Path, destination: Path) -> None:
    with (
        source.open("rb") as input_stream,
        destination.open("xb") as raw_output,
        gzip.GzipFile(fileobj=raw_output, mode="wb", mtime=0) as output,
    ):
        shutil.copyfileobj(input_stream, output, length=1024 * 1024)


def _build_corpus(docs_root: Path, destination: Path) -> None:
    if not docs_root.is_dir() or docs_root.is_symlink():
        raise SimpleBuildError("docs 必须是本地真实目录。")
    files = tuple(
        sorted(
            (path for path in docs_root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(docs_root).as_posix(),
        )
    )
    if not files or any(path.is_symlink() for path in files):
        raise SimpleBuildError("docs 必须包含非 symlink 普通文件。")
    if not any(path.suffix.lower() == ".docx" for path in files):
        raise SimpleBuildError("docs 中没有可打包的 DOCX。")
    with tarfile.open(destination, mode="w:gz") as archive:
        for path in files:
            archive.add(
                path,
                arcname=path.relative_to(docs_root).as_posix(),
                recursive=False,
            )


def _copy_simple_files(
    *,
    root: Path,
    stage: Path,
    revision: str,
    app_image: str,
    ocr_image: str,
) -> None:
    simple_root = root / "deployment/simple"
    for name in _SIMPLE_FILES:
        source = simple_root / name
        if not source.is_file() or source.is_symlink():
            raise SimpleBuildError(f"simple 部署文件缺失：{name}")
        destination = stage / name
        shutil.copyfile(source, destination)
        if name.endswith(".sh"):
            destination.chmod(0o755)
    env_path = stage / ".env.example"
    rendered = env_path.read_text(encoding="utf-8")
    replacements = {
        "REPLACE_APP_IMAGE": app_image,
        "REPLACE_OCR_IMAGE": ocr_image,
        "REPLACE_QDRANT_IMAGE": _QDRANT_SAVE_IMAGE,
        "REPLACE_FULL_GIT_SHA": revision,
        "REPLACE_SHORT_GIT_SHA": revision[:12],
    }
    for marker, value in replacements.items():
        if rendered.count(marker) != 1:
            raise SimpleBuildError(f"env 模板标记数量无效：{marker}")
        rendered = rendered.replace(marker, value)
    env_path.write_text(rendered, encoding="utf-8")
    guide_path = stage / "DEPLOYMENT_GUIDE.md"
    guide = guide_path.read_text(encoding="utf-8")
    if "REPLACE_SHORT_GIT_SHA" not in guide:
        raise SimpleBuildError("部署指南缺少 Git SHA 渲染标记。")
    guide = guide.replace("REPLACE_SHORT_GIT_SHA", revision[:12])
    guide = guide.replace("REPLACE_FULL_GIT_SHA", revision)
    guide_path.write_text(guide, encoding="utf-8")


def _verify_simple_output(stage: Path) -> None:
    expected = set(_SIMPLE_FILES)
    for archive_name in _PACKAGE_ARCHIVES:
        expected.add(archive_name)
        expected.add(f"{archive_name}.sha256")
    actual = {path.name for path in stage.iterdir()}
    if actual != expected:
        raise SimpleBuildError("simple 部署输出文件集合不完整。")
    if any(
        not path.is_file() or path.is_symlink() or path.stat().st_size == 0
        for path in stage.iterdir()
    ):
        raise SimpleBuildError("simple 部署输出必须是非空普通文件。")
    for archive_name in _PACKAGE_ARCHIVES:
        expected_line = (
            f"{_sha256(stage / archive_name)}  {archive_name}\n"
        )
        if (stage / f"{archive_name}.sha256").read_text(
            encoding="ascii"
        ) != expected_line:
            raise SimpleBuildError(f"SHA256 sidecar 无效：{archive_name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _git_output(root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise SimpleBuildError("缺少 Git 可执行文件。")
    try:
        completed = subprocess.run(  # noqa: S603
            [git, "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SimpleBuildError("Git preflight 失败。") from error
    return completed.stdout


def _run_checked(arguments: Sequence[str], *, cwd: Path) -> None:
    try:
        subprocess.run(list(arguments), cwd=cwd, check=True)  # noqa: S603
    except (OSError, subprocess.CalledProcessError) as error:
        raise SimpleBuildError(f"命令执行失败：{arguments[0]}") from error


def _run_output(arguments: Sequence[str], *, cwd: Path) -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            list(arguments),
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SimpleBuildError(f"命令执行失败：{arguments[0]}") from error
    return completed.stdout


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output-parent", type=Path)
    parser.add_argument("--ocr-image")
    return parser.parse_args()


def main() -> int:
    """生成首次简单模块化部署包。

    Args:
        无参数。

    Returns:
        成功返回 0，输入或本地构建条件不满足时返回 1。

    """
    arguments = _arguments()
    try:
        output = build_simple_bundle(
            repository_root=arguments.repository_root,
            output_parent=arguments.output_parent,
            ocr_image=arguments.ocr_image,
        )
    except SimpleBuildError as error:
        print(f"SIMPLE_BUNDLE_BUILD_FAILED: {error}", file=sys.stderr)
        return 1
    print(f"simple_deploy_dir={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
