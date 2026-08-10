"""从 clean Industry commit 构建 app-only 更新包。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from rag_app.generation.semantic_router import (  # noqa: E402
    LLM_CLASSIFIER_CONTRACT_REVISION,
    QUESTION_PROFILE_SCHEMA_REVISION,
    load_intent_router_config,
    load_question_profile_calibration,
)
from rag_app.runtime import load_pipeline  # noqa: E402
from rag_app.settings import RetrievalSettings  # noqa: E402
from scripts.build_industry_bundle import (  # noqa: E402
    IndustryBuildError,
    require_industry_source,
)
from scripts.build_simple_bundle import (  # noqa: E402
    SimpleBuildError,
    prepare_project_wheel,
    write_sha256_sidecar,
)
from scripts.industry_bundle.images import (  # noqa: E402
    ImageArtifact,
    IndustryImageError,
    build_app_image_archive,
)

__all__ = ["IndustryAppUpdateBuildError", "build_industry_app_update"]


class IndustryAppUpdateBuildError(RuntimeError):
    """表示 Industry app-only 更新包无法安全生成。"""


def build_industry_app_update(
    *,
    repository_root: Path,
    output_parent: Path | None = None,
) -> Path:
    """构建并发布四文件 Industry app-only 更新包。

    Args:
        repository_root: clean Industry Git 根目录。
        output_parent: 可选测试输出父目录。

    Returns:
        `industry-app-update/<SHA前12位>` 发布目录。

    Raises:
        IndustryAppUpdateBuildError: Git、镜像或包契约不满足要求。

    """
    root = repository_root.resolve(strict=True)
    try:
        identity = require_industry_source(root)
        prepare_project_wheel(root, identity.git_sha)
        parent = output_parent or root / "artifacts/industry-app-update"
        parent.mkdir(parents=True, exist_ok=True)
        final = parent / identity.git_sha[:12]
        if final.exists() or final.is_symlink():
            raise IndustryAppUpdateBuildError(
                f"Industry app update 输出已存在：{final}"
            )
        with tempfile.TemporaryDirectory(
            dir=parent,
            prefix=f".{identity.git_sha[:12]}.",
        ) as temporary_name:
            stage = Path(temporary_name)
            image = build_app_image_archive(
                repository_root=root,
                revision=identity.git_sha,
                output_dir=stage,
            )
            write_sha256_sidecar(stage / "app-image.tar.gz")
            source_script = root / "deployment/industry/update-app.sh"
            if not source_script.is_file() or source_script.is_symlink():
                raise IndustryAppUpdateBuildError(
                    "缺少 Industry update-app.sh。"
                )
            update_script = stage / "update-app.sh"
            shutil.copyfile(source_script, update_script)
            update_script.chmod(0o755)
            manifest = _update_manifest(root, identity.git_sha, image)
            manifest["files"] = {
                "app-image.tar.gz": image.archive_sha256,
                "app-image.tar.gz.sha256": _sha256(
                    stage / "app-image.tar.gz.sha256"
                ),
                "update-app.sh": _sha256(update_script),
            }
            (stage / "UPDATE_MANIFEST.json").write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            _verify_stage(stage)
            stage.replace(final)
        if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
            raise IndustryAppUpdateBuildError(
                "构建结束后 Git 工作区出现漂移。"
            )
    except (
        IndustryBuildError,
        IndustryImageError,
        OSError,
        SimpleBuildError,
        ValueError,
    ) as error:
        raise IndustryAppUpdateBuildError(
            "INDUSTRY_APP_UPDATE_BUILD_FAILED"
        ) from error
    return final


def _update_manifest(
    root: Path,
    revision: str,
    image: ImageArtifact,
) -> dict[str, object]:
    pipeline = load_pipeline(root / "deployment/config/pipeline.json")
    retrieval = RetrievalSettings.load(
        root / "deployment/config/retrieval.json"
    )
    router = load_intent_router_config(
        root / "deployment/config/intent-router.json"
    )
    calibration = load_question_profile_calibration(
        root / "deployment/config/intent-router-calibration.json"
    )
    serving = retrieval.serving_fingerprint(
        pipeline,
        question_profile_identity={
            "intent_router_sha256": router.canonical_sha256,
            "calibration_sha256": calibration.canonical_sha256,
            "router_revision": router.router_revision,
            "active_mode": router.mode.value,
            "question_profile_schema_revision": (
                QUESTION_PROFILE_SCHEMA_REVISION
            ),
            "llm_classifier_contract_revision": (
                LLM_CLASSIFIER_CONTRACT_REVISION
            ),
        },
    )
    return {
        "branch": "Industry",
        "image": {
            "archive_sha256": image.archive_sha256,
            "config_digest": image.config_digest,
            "id": image.image_id,
            "manifest_digest": image.manifest_digest,
            "platform": image.platform,
            "ref": image.ref,
            "revision": image.revision,
        },
        "index_fingerprint": {
            "reindex_required": False,
            "target": pipeline.fingerprint(),
        },
        "revision": revision,
        "schema_version": "1",
        "serving_fingerprint": serving,
        "target": {
            "alias": "rag-industry-active",
            "project": "rag-industry",
            "service": "rag-industry-app",
        },
    }


def _verify_stage(stage: Path) -> None:
    expected = {
        "UPDATE_MANIFEST.json",
        "app-image.tar.gz",
        "app-image.tar.gz.sha256",
        "update-app.sh",
    }
    if {path.name for path in stage.iterdir()} != expected:
        raise IndustryAppUpdateBuildError(
            "Industry app update exact set 无效。"
        )
    payload = json.loads((stage / "UPDATE_MANIFEST.json").read_bytes())
    if (
        payload.get("branch") != "Industry"
        or payload.get("target")
        != {
            "alias": "rag-industry-active",
            "project": "rag-industry",
            "service": "rag-industry-app",
        }
        or payload.get("index_fingerprint", {}).get("reindex_required")
        is not False
    ):
        raise IndustryAppUpdateBuildError(
            "Industry app update manifest 无效。"
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_output(root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise IndustryAppUpdateBuildError("缺少 Git 可执行文件。")
    try:
        completed = subprocess.run(  # noqa: S603
            [git, "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise IndustryAppUpdateBuildError("Git clean 检查失败。") from error
    return completed.stdout


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=_REPOSITORY_ROOT,
    )
    parser.add_argument("--output-parent", type=Path)
    return parser.parse_args()


def main() -> int:
    """执行 Industry app-only 更新包构建。

    Args:
        无参数；命令行选项由当前进程解析。

    Returns:
        成功返回 0，任一门禁失败返回 1。

    """
    arguments = _arguments()
    try:
        output = build_industry_app_update(
            repository_root=arguments.repository_root,
            output_parent=arguments.output_parent,
        )
    except IndustryAppUpdateBuildError as error:
        print(f"INDUSTRY_APP_UPDATE_BUILD_FAILED: {error}", file=sys.stderr)
        return 1
    print(f"industry_app_update_dir={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
