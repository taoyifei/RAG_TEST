"""从 clean Industry commit 构建 serving app update bundle。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from rag_app.corpus_policy import CorpusPolicy  # noqa: E402
from rag_app.generation.semantic_router import (  # noqa: E402
    LLM_CLASSIFIER_CONTRACT_REVISION,
    QUESTION_PROFILE_SCHEMA_REVISION,
    load_intent_router_config,
    load_question_profile_calibration,
)
from rag_app.model_contracts import actual_prompt_revision  # noqa: E402
from rag_app.runtime import load_pipeline  # noqa: E402
from rag_app.settings import RetrievalSettings  # noqa: E402
from scripts.build_industry_bundle import (  # noqa: E402
    IndustryBuildError,
    IndustrySourceIdentity,
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

_OLD_REVISION = "2c4cf220c7cf7dd2e8744253453e994ee7af3ee1"
_INDEX_FINGERPRINT = (
    "sha256:dd16e57d6b39e95af18ea5317d66682c71f4044e927a09bc6cc0599a8f7f192a"
)
_SOURCE_SERVING_FINGERPRINT = (
    "sha256:41dc694db23d1895b08a703e058fc5ea6d7511da9484e42268c6bb3258c81c9b"
)
_PACKAGE_FILES = {
    "SERVER_UPDATE_COMMANDS.txt",
    "UPDATE_MANIFEST.json",
    "app-image.tar.gz",
    "app-image.tar.gz.sha256",
    "package_selfcheck.py",
    "serving-runtime.tar.gz",
    "serving-runtime.tar.gz.sha256",
    "update-app.sh",
}
_CONFIG_SOURCES = {
    "corpus-policy.json": "deployment/config/corpus-policy.json",
    "intent-router-calibration.json": (
        "deployment/config/intent-router-calibration.json"
    ),
    "intent-router.json": "deployment/config/intent-router.json",
    "pipeline.json": "deployment/config/pipeline.json",
    "retrieval.json": "deployment/config/retrieval.json",
}
_RUNTIME_SOURCES = {
    "compose.yaml": "deployment/industry/compose.yaml",
    "last_good.py": "deployment/industry/serving_last_good.py",
    "lib.sh": "deployment/industry/lib.sh",
    "rollback-app-update.sh": ("deployment/industry/rollback-app-update.sh"),
    "runtime_check.py": "deployment/industry/serving_runtime_check.py",
    "ui_contract_check.py": "deployment/industry/ui_contract_check.py",
    "validation_check.py": "deployment/industry/runtime_check.py",
    "verify-app-update.sh": "deployment/industry/verify-app-update.sh",
    "validation/expected-corpus.json": (
        "evaluation/industry/expected-corpus.json"
    ),
    "validation/industry-smoke.jsonl": "evaluation/industry/smoke.jsonl",
}


class IndustryAppUpdateBuildError(RuntimeError):
    """表示 Industry serving app update 无法安全生成。"""


def build_industry_app_update(
    *,
    repository_root: Path,
    output_parent: Path | None = None,
) -> Path:
    """构建并发布八文件 Industry serving app update。

    Args:
        repository_root: clean Industry Git 根目录。
        output_parent: 可选测试输出父目录。

    Returns:
        `industry-serving-update/<SHA前12位>` 发布目录。

    Raises:
        IndustryAppUpdateBuildError: Git、配置、镜像或包合同不满足要求。

    """
    root = repository_root.resolve(strict=True)
    try:
        identity = require_industry_source(root)
        _validate_serving_config(root)
        prepare_project_wheel(root, identity.git_sha)
        parent = output_parent or root / "artifacts/industry-serving-update"
        parent.mkdir(parents=True, exist_ok=True)
        final = parent / identity.git_sha[:12]
        if final.exists() or final.is_symlink():
            raise IndustryAppUpdateBuildError(
                f"Industry serving update 输出已存在：{final}"
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
            runtime = _build_runtime_archive(root, stage, identity)
            write_sha256_sidecar(stage / "serving-runtime.tar.gz")
            _copy_package_programs(root, stage)
            manifest = _update_manifest(
                root,
                identity.git_sha,
                image,
                runtime,
                stage,
            )
            _write_json(stage / "UPDATE_MANIFEST.json", manifest)
            _verify_stage(stage)
            _run_package_selfcheck(stage)
            stage.replace(final)
        if _git_output(root, "status", "--porcelain", "--untracked-files=all"):
            raise IndustryAppUpdateBuildError("构建结束后 Git 工作区出现漂移。")
    except (
        IndustryBuildError,
        IndustryImageError,
        OSError,
        SimpleBuildError,
        ValueError,
    ) as error:
        if isinstance(error, IndustryAppUpdateBuildError):
            raise
        raise IndustryAppUpdateBuildError(
            "INDUSTRY_SERVING_UPDATE_BUILD_FAILED"
        ) from error
    return final


def _validate_serving_config(root: Path) -> None:
    pipeline = load_pipeline(root / "deployment/config/pipeline.json")
    if pipeline.prompt_revision != actual_prompt_revision():
        raise IndustryAppUpdateBuildError("PROMPT_REVISION_MISMATCH")
    policy = CorpusPolicy.load(root / "deployment/config/corpus-policy.json")
    if pipeline.corpus_policy_sha256 != policy.semantic_sha256():
        raise IndustryAppUpdateBuildError("CORPUS_POLICY_SHA256_MISMATCH")
    if pipeline.fingerprint() != _INDEX_FINGERPRINT:
        raise IndustryAppUpdateBuildError("INDEX_FINGERPRINT_CHANGED")


def _build_runtime_archive(
    root: Path,
    stage: Path,
    identity: IndustrySourceIdentity,
) -> dict[str, object]:
    runtime_root = f"serving-runtime/{identity.git_sha[:12]}"
    payloads: dict[str, bytes] = {}
    for target, source in _RUNTIME_SOURCES.items():
        payloads[target] = _required_regular_file(root / source).read_bytes()
    for target, source in _CONFIG_SOURCES.items():
        payloads[f"config/{target}"] = _required_regular_file(
            root / source
        ).read_bytes()
    runtime_manifest = {
        "files": {
            name: {
                "mode": f"{_runtime_mode(name):04o}",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(payloads.items())
        },
        "revision": identity.git_sha,
        "root": runtime_root,
        "schema_version": "1",
    }
    payloads["RUNTIME_MANIFEST.json"] = _canonical_json(runtime_manifest)
    archive_path = stage / "serving-runtime.tar.gz"
    _write_deterministic_tar(
        archive_path,
        runtime_root,
        payloads,
        identity.source_date_epoch,
    )
    files = {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in sorted(payloads.items())
    }
    canonical = json.dumps(files, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return {
        "archive_sha256": _sha256(archive_path),
        "canonical_digest": hashlib.sha256(canonical).hexdigest(),
        "files": files,
        "root": runtime_root,
        "source_date_epoch": identity.source_date_epoch,
    }


def _write_deterministic_tar(
    output_path: Path,
    runtime_root: str,
    payloads: dict[str, bytes],
    mtime: int,
) -> None:
    directories = {"serving-runtime", runtime_root}
    for relative in payloads:
        parent = PurePosixPath(f"{runtime_root}/{relative}").parent
        while str(parent) not in {".", ""}:
            directories.add(str(parent))
            parent = parent.parent
    with (
        output_path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
        tarfile.open(
            fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT
        ) as archive,
    ):
        for name in sorted(directories):
            info = _tar_info(f"{name}/", mtime, 0o755)
            info.type = tarfile.DIRTYPE
            info.size = 0
            archive.addfile(info)
        for relative, payload in sorted(payloads.items()):
            info = _tar_info(
                f"{runtime_root}/{relative}",
                mtime,
                _runtime_mode(relative),
            )
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _tar_info(name: str, mtime: int, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = mtime
    info.mode = mode
    return info


def _runtime_mode(relative: str) -> int:
    if relative.endswith(".sh") or relative in {
        "last_good.py",
        "runtime_check.py",
        "ui_contract_check.py",
        "validation_check.py",
    }:
        return 0o755
    return 0o644


def _copy_package_programs(root: Path, stage: Path) -> None:
    sources = {
        "package_selfcheck.py": (
            "deployment/industry/serving_update_selfcheck.py"
        ),
        "update-app.sh": "deployment/industry/update-app.sh",
    }
    for target, source in sources.items():
        destination = stage / target
        shutil.copyfile(_required_regular_file(root / source), destination)
        destination.chmod(0o755)
    (stage / "SERVER_UPDATE_COMMANDS.txt").write_text(
        (
            "# Replace placeholders; start from a fresh shell.\n"
            "# Do not source the private env.\n"
            "PACKAGE_DIR=/absolute/path/to/industry-serving-update\n"
            "ENV_FILE=/absolute/path/to/rag-industry.env\n"
            'cd "${PACKAGE_DIR}"\n'
            "sha256sum -c app-image.tar.gz.sha256\n"
            "sha256sum -c serving-runtime.tar.gz.sha256\n"
            'python3 package_selfcheck.py verify "${PACKAGE_DIR}"\n'
            'bash update-app.sh "${ENV_FILE}"\n'
            "# Do not run run-index.sh; reindex_required=false.\n"
        ),
        encoding="utf-8",
    )


def _update_manifest(
    root: Path,
    revision: str,
    image: ImageArtifact,
    runtime: dict[str, object],
    stage: Path,
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
    config_files = {
        name: _sha256(root / source)
        for name, source in sorted(_CONFIG_SOURCES.items())
    }
    manifest: dict[str, object] = {
        "branch": "Industry",
        "config_files": config_files,
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
            "source": _INDEX_FINGERPRINT,
            "target": pipeline.fingerprint(),
        },
        "package_contract_revision": "industry-serving-update-v2",
        "revision": revision,
        "runtime": runtime,
        "schema_version": "2",
        "serving_fingerprint": {
            "source": _SOURCE_SERVING_FINGERPRINT,
            "target": serving,
        },
        "source_compatibility": {
            "compatible_revisions": [_OLD_REVISION],
            "old_app_runtime_state_required": False,
            "required_index_fingerprint": _INDEX_FINGERPRINT,
            "trace_v2_read_compatible": True,
        },
        "target": {
            "alias": "rag-industry-active",
            "project": "rag-industry",
            "service": "rag-industry-app",
        },
        "trace": {
            "question_capture": "plaintext",
            "question_retention_seconds": 604800,
            "schema_version": 2,
        },
        "ui": {
            "allow_insecure_http": True,
            "cookie_secure": False,
            "query_auth_mode": "same_origin_session",
            "session_ttl_seconds": 1800,
        },
    }
    manifest["files"] = {
        name: _sha256(stage / name)
        for name in sorted(_PACKAGE_FILES - {"UPDATE_MANIFEST.json"})
    }
    return manifest


def _verify_stage(stage: Path) -> None:
    if {path.name for path in stage.iterdir()} != _PACKAGE_FILES:
        raise IndustryAppUpdateBuildError(
            "Industry serving update exact set 无效。"
        )
    if any(not path.is_file() or path.is_symlink() for path in stage.iterdir()):
        raise IndustryAppUpdateBuildError(
            "Industry serving update 文件类型无效。"
        )


def _run_package_selfcheck(stage: Path) -> None:
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(stage / "package_selfcheck.py"),
            "verify",
            str(stage),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
    )
    if completed.returncode != 0:
        raise IndustryAppUpdateBuildError(
            "PACKAGE_SELFCHECK_FAILED: " + completed.stderr.strip()
        )


def _required_regular_file(path: Path) -> Path:
    if not path.is_file() or path.is_symlink():
        raise IndustryAppUpdateBuildError(f"更新包源文件无效：{path.name}")
    return path


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(_canonical_json(value))


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
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
    """执行 Industry serving app update 构建。

    Args:
        无参数；命令行参数由 argparse 解析。

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
        print(
            f"INDUSTRY_SERVING_UPDATE_BUILD_FAILED: {error}",
            file=sys.stderr,
        )
        return 1
    print(f"industry_serving_update_dir={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
