"""DOCX RAG 服务与断网资源自检命令。"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import uvicorn

from rag_app._build_revision import SOURCE_REVISION
from rag_app.assets import AssetPaths, verify_offline_assets
from rag_app.runtime import build_runtime, load_pipeline
from rag_app.settings import RuntimeSettings
from rag_app.state import JobKind, JobState
from rag_app.worker_runtime import build_worker_runtime

__all__ = ["BuildInfo", "build_info", "main"]


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """安装包和 OCI 期望源码身份的最小报告。"""

    installed_revision: str
    expected_revision: str
    matches: bool


def build_info(
    *,
    expected_revision: str,
    installed_revision: str = SOURCE_REVISION,
) -> BuildInfo:
    """构造不含路径或配置内容的 revision 报告。

    Args:
        expected_revision: OCI 或部署环境期望的完整 Git SHA。
        installed_revision: wheel 内安装的完整 Git SHA。

    Returns:
        两个 revision 及其精确匹配结果。

    """
    return BuildInfo(
        installed_revision=installed_revision,
        expected_revision=expected_revision,
        matches=installed_revision == expected_revision,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """执行唯一服务入口或无网络资源自检。

    Args:
        argv: 可选命令行参数；缺失时由 argparse 读取进程参数。

    Returns:
        成功为零；校验异常由调用方获得非零退出。

    """
    parser = _parser()
    arguments = parser.parse_args(argv)
    read_only_result = _run_read_only_command(arguments)
    if read_only_result is not None:
        return read_only_result
    if arguments.command == "serve":
        verify_offline_assets(_default_asset_paths())
        settings = RuntimeSettings()  # type: ignore[call-arg]
        service_bundle = build_runtime(settings)
        try:
            uvicorn.run(
                service_bundle.app,
                host=settings.host,
                port=settings.port,
                access_log=True,
                log_level="info",
            )
        finally:
            service_bundle.close()
        return 0
    if arguments.command == "worker":
        settings = RuntimeSettings()  # type: ignore[call-arg]
        worker_bundle = build_worker_runtime(settings)
        try:
            while True:
                result = worker_bundle.runner.run_next(
                    worker_id=arguments.worker_id
                )
                if result is not None:
                    print(json.dumps(asdict(result), sort_keys=True))
                if arguments.once:
                    return 0
                if result is None:
                    time.sleep(arguments.poll_seconds)
        finally:
            worker_bundle.close()
    if arguments.command == "index":
        settings = RuntimeSettings()  # type: ignore[call-arg]
        index_bundle = build_worker_runtime(settings)
        try:
            job = index_bundle.control.create_job(
                idempotency_key=arguments.idempotency_key,
                kind=JobKind(arguments.kind),
                pipeline_fingerprint=build_runtime_fingerprint(settings),
            )
            while job.state in {JobState.PENDING, JobState.RUNNING}:
                index_bundle.runner.run_next(worker_id=arguments.worker_id)
                job = index_bundle.control.get_job(job.job_id)
            print(
                json.dumps(
                    {
                        "job_id": job.job_id,
                        "state": job.state.value,
                        "error_code": job.error_code,
                    },
                    sort_keys=True,
                )
            )
            return 0 if job.state == JobState.SUCCEEDED else 1
        finally:
            index_bundle.close()
    raise AssertionError("argparse 未约束到已知命令。")


def _run_read_only_command(arguments: argparse.Namespace) -> int | None:
    """执行不创建 runtime 的只读诊断命令。

    Args:
        arguments: 已解析的 CLI 参数。

    Returns:
        已处理命令的退出码；非只读命令返回 None。

    """
    if arguments.command == "build-info":
        build_report = build_info(
            expected_revision=arguments.expected_revision
        )
        print(
            json.dumps(
                asdict(build_report),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0 if build_report.matches else 1
    if arguments.command == "asset-selfcheck":
        asset_report = verify_offline_assets(
            AssetPaths(
                root=arguments.root,
                manifest_path=arguments.manifest,
                pipeline_path=arguments.pipeline,
                retrieval_path=arguments.retrieval,
                tokenizer_path=arguments.tokenizer,
                frontend_dir=arguments.frontend,
            )
        )
        print(
            json.dumps(
                asdict(asset_report),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    return None


def _parser() -> argparse.ArgumentParser:
    """构建所有 CLI 子命令共享的参数解析器。

    Args:
        无参数。

    Returns:
        已注册服务、索引和离线自检子命令的解析器。

    """
    parser = argparse.ArgumentParser(prog="rag-app")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="启动 API 与本地静态页。")
    build = subparsers.add_parser(
        "build-info",
        help="报告安装包与 OCI 期望 revision 是否一致。",
    )
    build.add_argument(
        "--expected-revision",
        default=os.environ.get("RAG_RELEASE_REVISION", ""),
    )
    worker = subparsers.add_parser(
        "worker",
        help="串行执行管理 API 创建的索引任务。",
    )
    worker.add_argument(
        "--worker-id",
        default="single-index-worker",
    )
    worker.add_argument("--once", action="store_true")
    worker.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
    )
    index = subparsers.add_parser(
        "index",
        help="幂等创建并同步执行一次全量或增量任务。",
    )
    index.add_argument("kind", choices=[kind.value for kind in JobKind])
    index.add_argument("--idempotency-key", required=True)
    index.add_argument(
        "--worker-id",
        default="index-cli",
    )
    selfcheck = subparsers.add_parser(
        "asset-selfcheck",
        help="在无网络环境检查镜像内资源。",
    )
    selfcheck.add_argument("--root", type=Path, default=Path("/app"))
    selfcheck.add_argument(
        "--manifest",
        type=Path,
        default=Path("/app/deployment/ASSETS.sha256"),
    )
    selfcheck.add_argument(
        "--pipeline",
        type=Path,
        default=Path("/app/deployment/config/pipeline.json"),
    )
    selfcheck.add_argument(
        "--retrieval",
        type=Path,
        default=Path("/app/deployment/config/retrieval.json"),
    )
    selfcheck.add_argument(
        "--tokenizer",
        type=Path,
        default=Path(
            "/app/deployment/assets/tokenizers/llm/tokenizer.json"
        ),
    )
    selfcheck.add_argument(
        "--frontend",
        type=Path,
        default=Path("/app/frontend"),
    )
    return parser


def build_runtime_fingerprint(settings: RuntimeSettings) -> str:
    """读取 CLI 创建任务使用的 pipeline 指纹。

    Args:
        settings: 已完成环境校验的运行设置。

    Returns:
        当前 pipeline 的稳定指纹。

    """
    return load_pipeline(settings.pipeline_path).fingerprint()


def _default_asset_paths() -> AssetPaths:
    return AssetPaths(
        root=Path("/app"),
        manifest_path=Path("/app/deployment/ASSETS.sha256"),
        pipeline_path=Path("/app/deployment/config/pipeline.json"),
        retrieval_path=Path("/app/deployment/config/retrieval.json"),
        tokenizer_path=Path(
            "/app/deployment/assets/tokenizers/llm/tokenizer.json"
        ),
        frontend_dir=Path("/app/frontend"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
