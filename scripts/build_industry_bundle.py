"""从 clean Industry commit 构建完整首次部署包与唯一上传归档。"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.build_simple_bundle import (  # noqa: E402
    SimpleBuildError,
    prepare_project_wheel,
)
from scripts.industry_bundle import (  # noqa: E402
    IndustryImageError,
    IndustryReleaseError,
    ReleaseIdentity,
    assemble_release,
    build_app_image_archive,
    build_outer_upload,
    existing_image_identity,
)
from scripts.offline_bundle import publish_directory  # noqa: E402

__all__ = [
    "IndustryBuildError",
    "IndustrySourceIdentity",
    "build_industry_bundle",
    "require_industry_source",
]

_FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")


class IndustryBuildError(RuntimeError):
    """表示 Industry 首次部署包无法从当前源码安全生成。"""


@dataclass(frozen=True, slots=True)
class IndustrySourceIdentity:
    """clean Industry commit 与构建时间身份。"""

    git_sha: str
    main_sha: str
    source_date_epoch: int


def require_industry_source(repository_root: Path) -> IndustrySourceIdentity:
    """要求 clean Industry branch 和完整本地 commit。

    Args:
        repository_root: 当前 Git 根目录。

    Returns:
        HEAD、main 与 commit timestamp。

    Raises:
        IndustryBuildError: branch、工作区或 commit 身份无效。

    """
    root = repository_root.resolve(strict=True)
    branch = _git_output(root, "branch", "--show-current").strip()
    if branch != "Industry":
        raise IndustryBuildError("构建 Industry 包要求当前分支为 Industry。")
    status = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    if status:
        raise IndustryBuildError(
            "构建 Industry 包要求 tracked/untracked 工作区 clean。"
        )
    git_sha = _git_output(root, "rev-parse", "HEAD").strip()
    main_sha = _git_output(root, "rev-parse", "main").strip()
    if (
        _FULL_REVISION.fullmatch(git_sha) is None
        or _FULL_REVISION.fullmatch(main_sha) is None
    ):
        raise IndustryBuildError("HEAD/main 必须是完整小写 Git SHA。")
    _git_output(root, "cat-file", "-e", f"{git_sha}^{{commit}}")
    timestamp = _git_output(root, "show", "-s", "--format=%ct", git_sha).strip()
    if not timestamp.isdigit():
        raise IndustryBuildError("commit timestamp 无效。")
    return IndustrySourceIdentity(
        git_sha=git_sha,
        main_sha=main_sha,
        source_date_epoch=int(timestamp),
    )


def build_industry_bundle(  # noqa: PLR0913
    *,
    repository_root: Path,
    corpus_root: Path,
    reuse_ocr_image: str,
    reuse_ocr_image_id: str,
    reuse_ocr_revision: str,
    reuse_qdrant_image: str,
    reuse_qdrant_image_id: str,
    artifacts_root: Path | None = None,
) -> ReleaseIdentity:
    """构建复用服务器 OCR/Qdrant 镜像的 Industry 首次部署包。

    Args:
        repository_root: clean Industry Git 根目录。
        corpus_root: 与当前 commit 绑定的已审计 corpus 输出。
        reuse_ocr_image: 目标服务器现有 OCR 固定 tag。
        reuse_ocr_image_id: 目标服务器现有 OCR 完整 image ID。
        reuse_ocr_revision: 目标服务器现有 OCR 完整 revision label。
        reuse_qdrant_image: 目标服务器现有 Qdrant 固定 tag。
        reuse_qdrant_image_id: 目标服务器现有 Qdrant 完整 image ID。
        artifacts_root: 可选测试输出根目录；默认仓库 `artifacts`。

    Returns:
        最终 release 目录、外层 archive 与 sidecar。

    Raises:
        IndustryBuildError: Git、corpus、镜像、package 或 fresh verify 失败。

    """
    root = repository_root.resolve(strict=True)
    identity = require_industry_source(root)
    output_root = artifacts_root or root / "artifacts"
    deploy_parent = output_root / "industry-deploy"
    upload_parent = output_root
    deploy_parent.mkdir(parents=True, exist_ok=True)
    if (upload_parent / "industry-upload").exists():
        raise IndustryBuildError("industry-upload 已存在，拒绝覆盖。")
    try:
        prepare_project_wheel(root, identity.git_sha)
        with tempfile.TemporaryDirectory(
            dir=deploy_parent,
            prefix=".industry-release-",
        ) as temporary_name:
            stage = Path(temporary_name)
            app = build_app_image_archive(
                repository_root=root,
                revision=identity.git_sha,
                output_dir=stage,
            )
            ocr = existing_image_identity(
                name="ocr",
                ref=reuse_ocr_image,
                image_id=reuse_ocr_image_id,
                revision=reuse_ocr_revision,
            )
            qdrant = existing_image_identity(
                name="qdrant",
                ref=reuse_qdrant_image,
                image_id=reuse_qdrant_image_id,
                revision=None,
            )
            staged_identity = assemble_release(
                repository_root=root,
                stage=stage,
                corpus_root=corpus_root,
                git_sha=identity.git_sha,
                main_sha=identity.main_sha,
                source_date_epoch=identity.source_date_epoch,
                images=(app, ocr, qdrant),
            )
            final_release = deploy_parent / staged_identity.release_id
            if final_release.exists() or final_release.is_symlink():
                raise IndustryBuildError("Industry release 已存在，拒绝覆盖。")
            publish_directory(stage, final_release)
        final = build_outer_upload(
            release_dir=final_release,
            upload_parent=upload_parent,
            source_date_epoch=identity.source_date_epoch,
        )
    except (
        OSError,
        ValueError,
        IndustryImageError,
        IndustryReleaseError,
        SimpleBuildError,
    ) as error:
        raise IndustryBuildError("INDUSTRY_BUNDLE_BUILD_FAILED") from error
    if _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
    ):
        raise IndustryBuildError("构建结束后 Git 工作区出现漂移。")
    return final


def _git_output(root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise IndustryBuildError("缺少 Git 可执行文件。")
    try:
        completed = subprocess.run(  # noqa: S603
            [git, "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise IndustryBuildError("Git identity 检查失败。") from error
    return completed.stdout


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=_REPOSITORY_ROOT,
    )
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--reuse-ocr-image", required=True)
    parser.add_argument("--reuse-ocr-image-id", required=True)
    parser.add_argument("--reuse-ocr-revision", required=True)
    parser.add_argument("--reuse-qdrant-image", required=True)
    parser.add_argument("--reuse-qdrant-image-id", required=True)
    parser.add_argument("--artifacts-root", type=Path)
    return parser.parse_args()


def main() -> int:
    """执行 Industry 首次部署包构建。

    Args:
        无参数；命令行选项由当前进程解析。

    Returns:
        成功返回 0；任一 fail-closed 门禁失败返回 1。

    """
    arguments = _arguments()
    try:
        result = build_industry_bundle(
            repository_root=arguments.repository_root,
            corpus_root=arguments.corpus_root,
            reuse_ocr_image=arguments.reuse_ocr_image,
            reuse_ocr_image_id=arguments.reuse_ocr_image_id,
            reuse_ocr_revision=arguments.reuse_ocr_revision,
            reuse_qdrant_image=arguments.reuse_qdrant_image,
            reuse_qdrant_image_id=arguments.reuse_qdrant_image_id,
            artifacts_root=arguments.artifacts_root,
        )
    except IndustryBuildError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"release_dir={result.release_dir}")
    print(f"upload_archive={result.outer_archive}")
    print(f"upload_sidecar={result.outer_sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
