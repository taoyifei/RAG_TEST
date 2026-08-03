"""以既有入口完成一次可追溯的本地 smoke release 演练。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

_FULL_REVISION = re.compile(r"[0-9a-f]{40}")
_MIN_FREE_BYTES = 80 * 1024**3
_REPORT_NAME = "release-smoke-report.json"
_ARCHIVE_FIELD_COUNT = 6
_IMAGE_COUNT = 3
_RELEASE_FILE_COUNT = 7


class SmokeError(RuntimeError):
    """表示 smoke 流程以稳定错误码停止。"""

    def __init__(self, code: str) -> None:
        """初始化稳定错误码。"""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Stage:
    """描述一个失败即停止的浅层编排阶段。"""

    name: str
    error_code: str
    action: Callable[[], None]


@dataclass
class SmokeContext:
    """保存阶段间传递的非敏感 release 状态。"""

    root: Path
    report_path: Path
    head: str = ""
    release_id: str = ""
    corpus_id: str = ""
    corpus_manifest: Path | None = None
    release_dir: Path | None = None
    sbom_available: bool = False
    images: list[dict[str, str]] = field(default_factory=list)
    files: list[dict[str, str | int]] = field(default_factory=list)


def execute_stages(
    stages: Sequence[Stage],
    report: dict[str, object],
) -> None:
    """顺序执行阶段，并在首个失败后立即停止。

    Args:
        stages: 已按发布顺序排列的阶段。
        report: 追加脱敏阶段结果的报告。

    Returns:
        无。

    Raises:
        SmokeError: 任一阶段失败，错误码来自该阶段定义。

    """
    results = report.setdefault("stages", [])
    if not isinstance(results, list):
        raise ValueError("report stages 必须是列表。")
    for stage in stages:
        started = time.monotonic()
        try:
            stage.action()
        except Exception as error:
            results.append(
                {
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_code": stage.error_code,
                    "name": stage.name,
                    "status": "failed",
                }
            )
            raise SmokeError(stage.error_code) from error
        results.append(
            {
                "duration_seconds": round(time.monotonic() - started, 3),
                "name": stage.name,
                "status": "passed",
            }
        )


def _run(
    arguments: Sequence[str],
    *,
    root: Path,
    environment: dict[str, str] | None = None,
    capture: bool = False,
) -> str:
    """运行既有入口，失败即抛错，可选返回标准输出。"""
    completed = subprocess.run(  # noqa: S603
        list(arguments),
        cwd=root,
        env=environment,
        check=False,
        capture_output=capture,
        text=capture,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {arguments[0]}")
    return completed.stdout.strip() if capture else ""


def _sha256(path: Path) -> str:
    """计算普通文件 SHA256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _preflight(context: SmokeContext) -> None:
    """检查 clean Git、Python、Docker 工具和磁盘。"""
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("Python 3.11 required")
    context.head = _run(
        ["git", "rev-parse", "HEAD"], root=context.root, capture=True
    )
    if _FULL_REVISION.fullmatch(context.head) is None:
        raise RuntimeError("invalid Git HEAD")
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        root=context.root,
        capture=True,
    )
    if status:
        raise RuntimeError("Git worktree is not clean")
    for arguments in (
        ("docker", "version"),
        ("docker", "compose", "version"),
        ("docker", "buildx", "version"),
    ):
        _run(arguments, root=context.root)
    sbom = subprocess.run(
        ["docker", "sbom", "--help"],  # noqa: S607
        cwd=context.root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    context.sbom_available = sbom.returncode == 0
    if shutil.disk_usage(context.root).free < _MIN_FREE_BYTES:
        raise RuntimeError("insufficient free disk")
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    context.release_id = f"{context.head[:12]}-smoke-{stamp}"
    context.corpus_id = f"smoke-{stamp}"


def _matched_value(path: Path, pattern: str) -> str:
    """从固定配置中读取唯一镜像引用。"""
    matches: list[str] = re.findall(
        pattern,
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise RuntimeError(f"image reference missing: {path.name}")
    return matches[0]


def _assets(context: SmokeContext) -> None:
    """验证固定镜像、前端/tokenizer、OCR 模型和 wheelhouse。"""
    python_image = _matched_value(
        context.root / "Dockerfile",
        r"^ARG PYTHON_IMAGE=(\S+)$",
    )
    ocr_image = _matched_value(
        context.root / "deployment/ocr/Dockerfile",
        r"^FROM (\S+)$",
    )
    qdrant_image = _matched_value(
        context.root / "deployment/qdrant-policy.sh",
        r'^readonly RAG_APPROVED_QDRANT_SOURCE_IMAGE="([^"]+)"$',
    )
    _run(
        ["docker", "image", "inspect", python_image, ocr_image, qdrant_image],
        root=context.root,
    )
    for directory, manifest in (
        (context.root, "deployment/ASSETS.sha256"),
        (context.root / "deployment/ocr/assets", "MANIFEST.sha256"),
        (context.root / "deployment/ocr/assets", "../MODELS.sha256"),
        (
            context.root / "deployment/ocr/assets/wheelhouse",
            "../../WHEELS.sha256",
        ),
    ):
        _run(["sha256sum", "--check", manifest], root=directory)


def _runtime_wheels(context: SmokeContext) -> None:
    """仅从现有 wheelhouse 运行既有 runtime wheel 准备入口。"""
    environment = os.environ.copy()
    environment.update(
        {
            "PIP_FIND_LINKS": str(
                context.root / "deployment/runtime/wheelhouse"
            ),
            "PIP_NO_INDEX": "1",
        }
    )
    _run(
        [sys.executable, "scripts/prepare_runtime_wheels.py"],
        root=context.root,
        environment=environment,
    )


def _build(context: SmokeContext, *, ocr: bool) -> None:
    """使用 `docker buildx build --network none` 构建单个镜像。"""
    tag = (
        f"docx-rag-ocr:{context.release_id}"
        if ocr
        else f"docx-rag:{context.release_id}"
    )
    arguments = [
        "docker",
        "buildx",
        "build",
        "--network",
        "none",
        "--platform",
        "linux/amd64",
        "--load",
        "--build-arg",
        f"VCS_REF={context.head}",
    ]
    if ocr:
        arguments.extend(("--file", "deployment/ocr/Dockerfile"))
    arguments.extend(("--tag", tag, "."))
    _run(arguments, root=context.root)
    identity = _run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Os}}/{{.Architecture}} "
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            tag,
        ],
        root=context.root,
        capture=True,
    )
    if identity != f"linux/amd64 {context.head}":
        raise RuntimeError("built image identity mismatch")


def _selfcheck(context: SmokeContext, *, ocr: bool) -> None:
    """以 `--network none` 调用镜像既有自检入口。"""
    tag = (
        f"docx-rag-ocr:{context.release_id}"
        if ocr
        else f"docx-rag:{context.release_id}"
    )
    arguments = ["docker", "run", "--rm", "--network", "none"]
    if ocr:
        arguments.extend(
            (
                "--entrypoint",
                "python",
                tag,
                "-c",
                "from rag_app.ocr.main import main; assert callable(main)",
            )
        )
    else:
        arguments.extend((tag, "asset-selfcheck"))
    _run(arguments, root=context.root)


def _freeze_corpus(context: SmokeContext) -> None:
    """调用 `freeze_corpus_manifest.py` 冻结当前 DOCX exact set。"""
    output = context.root / "corpus-manifests" / f"{context.corpus_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            sys.executable,
            "scripts/freeze_corpus_manifest.py",
            "freeze",
            "--docs",
            "docs",
            "--corpus-id",
            context.corpus_id,
            "--output",
            str(output),
        ],
        root=context.root,
    )
    context.corpus_manifest = output.resolve(strict=True)


def _package(context: SmokeContext) -> None:
    """调用 `deployment/package.sh` 生成 smoke 双包。"""
    if context.corpus_manifest is None:
        raise RuntimeError("corpus manifest missing")
    environment = os.environ.copy()
    environment.update(
        {
            "CORPUS_MANIFEST": str(context.corpus_manifest),
            "RAG_APP_IMAGE": f"docx-rag:{context.release_id}",
            "RAG_OCR_IMAGE": f"docx-rag-ocr:{context.release_id}",
            "RELEASE_ID": context.release_id,
            "RELEASE_TIER": "smoke",
        }
    )
    _run(
        ["bash", "deployment/package.sh"],
        root=context.root,
        environment=environment,
    )
    context.release_dir = (
        context.root
        / "artifacts/releases"
        / f"{context.release_id}-{context.corpus_id}"
    ).resolve(strict=True)


def _fresh_verify(context: SmokeContext) -> None:
    """在全新临时目录校验 sidecar、双层解包和 runtime verifier。"""
    if context.release_dir is None:
        raise RuntimeError("release directory missing")
    release = context.release_dir
    _run(["sha256sum", "--check", "RELEASE_MANIFEST.sha256"], root=release)
    runtime_archive = release / f"rag-runtime-{context.release_id}.tar.gz"
    corpus_archive = release / f"rag-corpus-{context.corpus_id}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="rag-smoke-verify-") as name:
        temporary = Path(name)
        runtime_output = temporary / "runtime-output"
        corpus_output = temporary / "corpus-output"
        for archive, output, top_level in (
            (runtime_archive, runtime_output, "runtime"),
            (corpus_archive, corpus_output, "corpus"),
        ):
            _run(
                [
                    sys.executable,
                    str(release / "offline_bundle.py"),
                    str(archive),
                    f"{archive}.sha256",
                    str(output),
                    "--top-level",
                    top_level,
                ],
                root=context.root,
            )
        runtime = runtime_output / "runtime"
        _run(["bash", str(runtime / "verify-offline.sh")], root=runtime)
        metadata = json.loads(
            (runtime / "RELEASE_METADATA.json").read_text(encoding="utf-8")
        )
        if metadata.get("release_tier") != "smoke":
            raise RuntimeError("runtime tier mismatch")
        for forbidden in ("acceptance.sh", "model-services", "sbom"):
            if (runtime / forbidden).exists():
                raise RuntimeError("production payload leaked into smoke")
        rows = (runtime / "IMAGE_ARCHIVES.tsv").read_text(
            encoding="ascii"
        ).splitlines()
        context.images = [
            {
                "archive": fields[0],
                "config_digest": fields[4],
                "manifest_digest": fields[2],
                "platform": fields[5],
                "revision": fields[3],
                "tag": fields[1],
            }
            for row in rows
            if len(fields := row.split("\t")) == _ARCHIVE_FIELD_COUNT
        ]
        if len(context.images) != _IMAGE_COUNT:
            raise RuntimeError("image manifest row count mismatch")
    context.files = [
        {
            "name": path.name,
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(release.iterdir())
        if path.is_file()
    ]
    if len(context.files) != _RELEASE_FILE_COUNT:
        raise RuntimeError("release file count mismatch")


def _write_report(context: SmokeContext, report: dict[str, object]) -> None:
    """原子写入脱敏 smoke 报告。"""
    report.update(
        {
            "corpus_id": context.corpus_id or None,
            "files": context.files,
            "head": context.head or None,
            "images": context.images,
            "release_dir": (
                str(context.release_dir) if context.release_dir else None
            ),
            "release_id": context.release_id or None,
            "release_tier": "smoke",
            "sbom_available": context.sbom_available,
            "schema_version": "1",
            "source_revision": context.head or None,
        }
    )
    context.report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = context.report_path.with_suffix(".json.new")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(context.report_path)


def main() -> int:
    """执行完整本地 smoke release 演练。

    Args:
        无。

    Returns:
        全部阶段成功为 0，首个失败为 1。

    """
    root = Path(__file__).resolve().parents[1]
    context = SmokeContext(
        root=root,
        report_path=root / "artifacts" / _REPORT_NAME,
    )
    report: dict[str, object] = {"stages": []}
    stages = tuple(
        Stage(name, error_code, action)
        for name, error_code, action in (
            ("preflight", "PREFLIGHT_FAILED", partial(_preflight, context)),
            ("assets", "ASSET_VALIDATION_FAILED", partial(_assets, context)),
            (
                "runtime_wheels",
                "RUNTIME_WHEELS_FAILED",
                partial(_runtime_wheels, context),
            ),
            (
                "app_build",
                "APP_BUILD_FAILED",
                partial(_build, context, ocr=False),
            ),
            (
                "ocr_build",
                "OCR_BUILD_FAILED",
                partial(_build, context, ocr=True),
            ),
            (
                "app_selfcheck",
                "APP_SELFCHECK_FAILED",
                partial(_selfcheck, context, ocr=False),
            ),
            (
                "ocr_selfcheck",
                "OCR_SELFCHECK_FAILED",
                partial(_selfcheck, context, ocr=True),
            ),
            (
                "corpus_freeze",
                "CORPUS_FREEZE_FAILED",
                partial(_freeze_corpus, context),
            ),
            ("package", "PACKAGE_FAILED", partial(_package, context)),
            (
                "fresh_verify",
                "FRESH_VERIFY_FAILED",
                partial(_fresh_verify, context),
            ),
        )
    )
    try:
        execute_stages(stages, report)
    except SmokeError as error:
        report.update({"error_code": error.code, "status": "failed"})
        _write_report(context, report)
        print(error.code, file=sys.stderr)
        return 1
    report["status"] = "passed"
    _write_report(context, report)
    print(f"report={context.report_path}")
    print(f"release_dir={context.release_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
