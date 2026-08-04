"""生成只包含 app 镜像的简单更新包。"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.build_simple_bundle import (  # noqa: E402
    SimpleBuildError,
    build_app_archive,
    prepare_project_wheel,
    require_clean_revision,
    write_sha256_sidecar,
)

__all__ = ["build_app_update"]


def build_app_update(
    *,
    repository_root: Path,
    output_parent: Path | None = None,
) -> Path:
    """从 clean HEAD 生成三文件 app 更新包。

    Args:
        repository_root: 当前项目 Git 根目录。
        output_parent: 可选测试输出父目录。

    Returns:
        已发布的 `app-update/<SHA前12位>` 目录。

    Raises:
        SimpleBuildError: Git、app 构建、自检或输出不符合契约。

    """
    root = repository_root.resolve(strict=True)
    revision = require_clean_revision(root)
    prepare_project_wheel(root, revision)
    parent = output_parent or root / "artifacts/app-update"
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / revision[:12]
    if final.exists() or final.is_symlink():
        raise SimpleBuildError(f"app update 输出已存在：{final}")
    with tempfile.TemporaryDirectory(
        dir=parent,
        prefix=f".{revision[:12]}.",
    ) as temporary_name:
        stage = Path(temporary_name)
        archive = stage / "app-image.tar.gz"
        build_app_archive(
            repository_root=root,
            revision=revision,
            destination=archive,
        )
        write_sha256_sidecar(archive)
        update_script = root / "deployment/simple/update-app.sh"
        if not update_script.is_file() or update_script.is_symlink():
            raise SimpleBuildError("缺少 simple update-app.sh。")
        copied_script = stage / "update-app.sh"
        shutil.copyfile(update_script, copied_script)
        copied_script.chmod(0o755)
        expected = {
            "app-image.tar.gz",
            "app-image.tar.gz.sha256",
            "update-app.sh",
        }
        if {path.name for path in stage.iterdir()} != expected:
            raise SimpleBuildError("app update 必须恰有三个输出文件。")
        stage.replace(final)
    return final


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output-parent", type=Path)
    return parser.parse_args()


def main() -> int:
    """生成只更新 app 镜像的模块化包。

    Args:
        无参数。

    Returns:
        成功返回 0，输入或本地构建条件不满足时返回 1。

    """
    arguments = _arguments()
    try:
        output = build_app_update(
            repository_root=arguments.repository_root,
            output_parent=arguments.output_parent,
        )
    except SimpleBuildError as error:
        print(f"APP_UPDATE_BUILD_FAILED: {error}", file=sys.stderr)
        return 1
    print(f"app_update_dir={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
