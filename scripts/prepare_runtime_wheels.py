"""按 Python 3.11 runtime lock 准备 linux/amd64 wheelhouse。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

_LOCK_LINE = re.compile(r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+$")
_PROJECT_WHEEL = re.compile(r"^docx_rag-0\.1\.0-.*\.whl$")
_REQUIRED_PROJECT_MEMBERS = frozenset(
    {
        "rag_app/worker_runtime.py",
        "rag_app/_build_revision.py",
        "rag_app/ocr/__init__.py",
        "rag_app/ocr/main.py",
    }
)
_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_REVISION_ASSIGNMENT = re.compile(
    rb'^SOURCE_REVISION = "([0-9a-f]{40})"\n$'
)


def verify_project_wheel(
    path: Path,
    *,
    expected_revision: str | None = None,
) -> str:
    """验证项目 wheel 包含入口和正式源码 revision。

    Args:
        path: 当前源码构建的 `docx-rag` wheel。
        expected_revision: 可选的完整 Git HEAD。

    Returns:
        wheel 内固定的完整 Git revision。

    Raises:
        ValueError: wheel 损坏、缺少模块或 revision 无效、不匹配。

    """
    try:
        with zipfile.ZipFile(path) as archive:
            members = frozenset(archive.namelist())
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("项目 wheel 不是有效 ZIP。") from error
    missing = _REQUIRED_PROJECT_MEMBERS - members
    if missing:
        raise ValueError(
            "项目 wheel 缺少 rag_app/ocr 或 worker 模块："
            f"{sorted(missing)}"
        )
    with zipfile.ZipFile(path) as archive:
        revision_source = archive.read("rag_app/_build_revision.py")
    matched = _REVISION_ASSIGNMENT.fullmatch(revision_source)
    if matched is None:
        raise ValueError("项目 wheel build revision 缺失或格式无效。")
    revision = matched.group(1).decode("ascii")
    if expected_revision is not None and revision != expected_revision:
        raise ValueError("项目 wheel revision 与 Git HEAD 不一致。")
    return revision


def prepare_runtime_wheels(
    *,
    repository_root: Path,
    lock_path: Path,
    output_dir: Path,
    manifest_path: Path,
    metadata_path: Path | None = None,
) -> int:
    """下载固定 runtime wheels 并重建当前项目 wheel。

    Args:
        repository_root: 当前 Git 源码根目录。
        lock_path: 完整固定的 Python 3.11 runtime lock。
        output_dir: ignored linux/amd64 wheelhouse。
        manifest_path: 输出的逐 wheel SHA256 清单。
        metadata_path: 可选项目 wheel revision/SHA 元数据输出；
            默认与清单同目录。

    Returns:
        最终 wheel 数量。

    Raises:
        ValueError: lock、wheel 集合或项目 wheel 不符合契约。
        subprocess.CalledProcessError: pip 下载或构建失败。

    """
    if sys.version_info[:2] != (3, 11):
        raise ValueError("runtime wheel 必须由 Python 3.11 准备。")
    root = repository_root.resolve(strict=True)
    lock = lock_path.resolve(strict=True)
    _require_clean_git(root)
    revision = _git_output(root, "rev-parse", "HEAD").strip()
    if _FULL_GIT_SHA.fullmatch(revision) is None:
        raise ValueError("Git HEAD 必须是完整 40 位小写 SHA。")
    _validate_lock(lock)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=output_dir.parent,
        prefix=".runtime-wheels-",
    ) as temporary_name:
        work = Path(temporary_name)
        stage = work / "wheelhouse"
        source_copy = work / "source"
        stage.mkdir()
        _copy_tracked_source(root, source_copy)
        revision_path = source_copy / "src/rag_app/_build_revision.py"
        revision_path.write_text(
            f'SOURCE_REVISION = "{revision}"\n',
            encoding="ascii",
        )
        _download_locked_wheels(lock, stage)
        _build_project_wheel(source_copy, stage)
        wheels = tuple(sorted(stage.glob("*.whl"), key=lambda item: item.name))
        project_wheels = tuple(
            wheel for wheel in wheels if _PROJECT_WHEEL.fullmatch(wheel.name)
        )
        if len(project_wheels) != 1:
            raise ValueError("wheelhouse 必须恰有一个当前 docx-rag wheel。")
        verify_project_wheel(
            project_wheels[0],
            expected_revision=revision,
        )
        if not wheels:
            raise ValueError("runtime wheelhouse 不能为空。")
        output_dir.mkdir(parents=True, exist_ok=True)
        for existing in output_dir.iterdir():
            if not existing.is_file() or existing.suffix != ".whl":
                raise ValueError(
                    "runtime wheelhouse 含非 wheel 文件，拒绝覆盖。"
                )
        for existing in output_dir.glob("*.whl"):
            existing.unlink()
        for wheel in wheels:
            wheel.replace(output_dir / wheel.name)
    final_wheels = tuple(
        sorted(output_dir.glob("*.whl"), key=lambda item: item.name)
    )
    lines = [
        f"{_sha256_file(wheel)}  {wheel.name}" for wheel in final_wheels
    ]
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    final_project_wheels = tuple(
        wheel
        for wheel in final_wheels
        if _PROJECT_WHEEL.fullmatch(wheel.name)
    )
    if len(final_project_wheels) != 1:
        raise ValueError("输出 wheelhouse 缺少唯一项目 wheel。")
    project_wheel = final_project_wheels[0]
    resolved_metadata = metadata_path or manifest_path.with_name(
        "PROJECT_WHEEL.json"
    )
    resolved_metadata.parent.mkdir(parents=True, exist_ok=True)
    resolved_metadata.write_text(
        json.dumps(
            {
                "project_wheel": project_wheel.name,
                "schema_version": "1",
                "sha256": _sha256_file(project_wheel),
                "source_revision": revision,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(final_wheels)


def _require_clean_git(root: Path) -> None:
    status = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    if status:
        raise ValueError("准备 runtime wheel 要求 clean Git 工作区。")


def _copy_tracked_source(root: Path, destination: Path) -> None:
    tracked = tuple(
        item
        for item in _git_output(root, "ls-files", "-z").split("\0")
        if item
    )
    if not tracked:
        raise ValueError("Git 没有可复制的 tracked 源文件。")
    destination.mkdir(parents=True)
    for relative in tracked:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("Git tracked 路径越界。")
        source = root / relative_path
        target = destination / relative_path
        if not source.is_file() or source.is_symlink():
            raise ValueError("Git tracked 源必须是普通文件。")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _git_output(root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise ValueError("缺少 Git 可执行文件。")
    completed = subprocess.run(  # noqa: S603
        [git, "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _validate_lock(path: Path) -> None:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or any(_LOCK_LINE.fullmatch(line) is None for line in lines):
        raise ValueError("runtime lock 必须全部使用 name==version。")
    if len(set(lines)) != len(lines):
        raise ValueError("runtime lock 含重复依赖。")


def _download_locked_wheels(lock: Path, destination: Path) -> None:
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--dest",
            str(destination),
            "--only-binary=:all:",
            "--platform",
            "manylinux_2_28_x86_64",
            "--implementation",
            "cp",
            "--python-version",
            "3.11",
            "--abi",
            "cp311",
            "--requirement",
            str(lock),
        ],
        check=True,
    )


def _build_project_wheel(root: Path, destination: Path) -> None:
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(destination),
            str(root),
        ],
        check=True,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=Path("requirements.runtime.lock"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("deployment/runtime/wheelhouse"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("deployment/runtime/WHEELS.sha256"),
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        default=Path("deployment/runtime/PROJECT_WHEEL.json"),
    )
    return parser.parse_args()


def main() -> int:
    """准备应用离线 wheelhouse。

    Args:
        无参数。

    Returns:
        成功时返回 0。

    """
    arguments = _arguments()
    count = prepare_runtime_wheels(
        repository_root=arguments.repository_root,
        lock_path=arguments.lock,
        output_dir=arguments.output,
        manifest_path=arguments.manifest,
        metadata_path=arguments.metadata,
    )
    print(f"verified_wheels={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
