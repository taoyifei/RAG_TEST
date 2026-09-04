"""DOCX RAG 服务与断网资源自检命令。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import uvicorn
from qdrant_client import QdrantClient

from rag_app._build_revision import SOURCE_REVISION
from rag_app.api.product import create_product_lifespan_app
from rag_app.assets import AssetPaths, verify_offline_assets
from rag_app.composition.product_runtime import ProductRuntimeSettings
from rag_app.index.gc import GarbageCollectorConfig, IndexGarbageCollector
from rag_app.manifest import ReadOnlyManifestRepository
from rag_app.product.crypto import (
    initialize_master_key,
    initialize_product_secret_bundle,
)
from rag_app.runtime import (
    build_runtime,
    load_pipeline,
    require_release_revision,
)
from rag_app.settings import RuntimeSettings
from rag_app.state import JobKind, JobState
from rag_app.state.jobs import ReadOnlyJobStore
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
    if arguments.command == "index-gc":
        return _run_index_gc(
            apply=arguments.apply,
            collection_prefix=arguments.collection_prefix,
        )
    return _run_runtime_command(arguments)


def _run_runtime_command(arguments: argparse.Namespace) -> int:
    """执行需要完整应用运行时的命令。

    Args:
        arguments: 已解析且未被只读或 GC 入口处理的参数。

    Returns:
        服务、worker 或索引任务的退出码。

    """
    product_result = _run_product_command(arguments)
    if product_result is not None:
        return product_result
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


def _run_product_command(arguments: argparse.Namespace) -> int | None:
    """执行 Product、Legacy Serve 或 Secret 初始化。

    Args:
        arguments: 已解析 CLI 参数。

    Returns:
        已处理命令的退出码；其他命令返回 None。

    """
    if arguments.command == "serve":
        product_settings = ProductRuntimeSettings.from_environment()
        print("PRODUCT_RUNTIME product-runtime-p10.5", flush=True)
        app = create_product_lifespan_app(
            product_settings,
            query_token=os.environ.get("RAG_QUERY_TOKEN"),
            admin_token=os.environ.get("RAG_ADMIN_TOKEN"),
        )
        uvicorn.run(
            app,
            host=product_settings.host,
            port=product_settings.port,
            access_log=True,
            log_level="info",
        )
        return 0
    if arguments.command == "legacy-serve":
        if os.environ.get("RAG_DATA_DIR"):
            raise ValueError("LEGACY_RUNTIME 禁止复用 RAG_DATA_DIR。")
        print("LEGACY_RUNTIME deprecated", flush=True)
        verify_offline_assets(_default_asset_paths())
        legacy_settings = RuntimeSettings()  # type: ignore[call-arg]
        service_bundle = build_runtime(legacy_settings)
        try:
            uvicorn.run(
                service_bundle.app,
                host=legacy_settings.host,
                port=legacy_settings.port,
                access_log=True,
                log_level="info",
            )
        finally:
            service_bundle.close()
        return 0
    if arguments.command != "init-secrets":
        return None
    if arguments.directory is not None:
        bundle = initialize_product_secret_bundle(arguments.directory)
        _print_json(
            {
                "bootstrap_token_id": bundle.bootstrap_token_id,
                "directory": str(bundle.directory),
                "master_key_id": bundle.master_key_id,
                "qdrant_api_key_id": bundle.qdrant_api_key_id,
            }
        )
        return 0
    if arguments.output is None:
        raise AssertionError("argparse 未提供 Secret 输出目标。")
    key = initialize_master_key(arguments.output)
    _print_json(
        {
            "fingerprint": key.key_id,
            "key_id": key.key_id,
            "path": str(arguments.output.resolve()),
        }
    )
    return 0


def _run_read_only_command(arguments: argparse.Namespace) -> int | None:
    """执行不创建 runtime 的只读诊断命令。

    Args:
        arguments: 已解析的 CLI 参数。

    Returns:
        已处理命令的退出码；非只读命令返回 None。

    """
    if arguments.command == "build-info":
        build_report = build_info(expected_revision=arguments.expected_revision)
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


def _run_index_gc(*, apply: bool, collection_prefix: str) -> int:
    """规划或显式执行本地索引垃圾回收。

    Args:
        apply: 为 True 时执行计划；否则只输出 dry-run。
        collection_prefix: 单索引 worker 使用的物理 collection 前缀。

    Returns:
        dry-run 或全部执行成功为 0；任一删除失败为 1。

    """
    settings = RuntimeSettings()  # type: ignore[call-arg]
    require_release_revision(settings)
    _require_existing_gc_database(
        settings.state_database,
        label="control SQLite",
    )
    _require_existing_gc_database(
        settings.manifest_database,
        label="manifest SQLite",
    )
    files_before = _gc_file_snapshot(settings)
    pipeline = load_pipeline(settings.pipeline_path)
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout=10,
        check_compatibility=False,
    )
    try:
        collector = IndexGarbageCollector(
            client=client,
            manifests=ReadOnlyManifestRepository(settings.manifest_database),
            control=ReadOnlyJobStore(settings.state_database),
            config=GarbageCollectorConfig(
                alias_name=settings.qdrant_alias,
                index_state_dir=settings.index_state_dir,
                collection_prefix=collection_prefix,
                dense_dimension=pipeline.embedding_dimension,
                pipeline_fingerprint=pipeline.fingerprint(),
                index_revision=pipeline.index_revision,
            ),
        )
        plan = collector.plan()
        if _gc_file_snapshot(settings) != files_before:
            raise RuntimeError("GC_DRY_RUN_FILE_DRIFT")
        items = [
            {
                "id": item.stable_id,
                "kind": item.kind.value,
                "reason": item.reason,
            }
            for item in plan.items
        ]
        if not apply:
            _print_json({"items": items, "mode": "dry-run"})
            return 0
        report = collector.apply(plan)
        results = [asdict(result) for result in report.results]
        _print_json({"items": items, "mode": "apply", "results": results})
        successful = {"deleted", "already_absent"}
        return (
            0
            if all(result.status in successful for result in report.results)
            else 1
        )
    finally:
        client.close()


def _require_existing_gc_database(path: Path, *, label: str) -> None:
    """要求 GC 数据库已经存在且是非 symlink 普通文件。

    Args:
        path: SQLite 主库路径。
        label: 不含路径的错误标签。

    Raises:
        FileNotFoundError: 路径缺失或不是安全普通文件。

    """
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"{label} 必须是已存在的安全普通文件。")


def _gc_file_snapshot(
    settings: RuntimeSettings,
) -> tuple[tuple[str, str], ...]:
    """冻结 GC 可见 SQLite 文件集合与内容摘要。

    Args:
        settings: 含 control、manifest 与 collection state 路径的设置。

    Returns:
        路径与 SHA256 组成的稳定元组。

    Raises:
        ValueError: 任一候选路径是 symlink 或非普通文件。

    """
    paths: set[Path] = set()
    for main_path in (
        settings.state_database,
        settings.manifest_database,
    ):
        for path in (
            main_path,
            Path(f"{main_path}-wal"),
            Path(f"{main_path}-shm"),
        ):
            if path.is_symlink():
                raise ValueError("GC SQLite 文件不能是 symlink。")
            if path.exists():
                if not path.is_file():
                    raise ValueError("GC SQLite 路径必须是普通文件。")
                paths.add(path)
    state_dir = settings.index_state_dir
    if state_dir.is_symlink() or (
        state_dir.exists() and not state_dir.is_dir()
    ):
        raise ValueError("GC state 目录必须是真实目录。")
    if state_dir.is_dir():
        for path in state_dir.iterdir():
            if not _is_collection_state_file(path.name):
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("GC collection state 必须是普通文件。")
            paths.add(path)
    return tuple(
        (
            path.as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(paths)
    )


def _is_collection_state_file(name: str) -> bool:
    """判断文件名是否属于 collection state 逻辑三件套。"""
    return name.startswith("index-") and name.endswith(
        (".sqlite3", ".sqlite3-wal", ".sqlite3-shm")
    )


def _print_json(payload: object) -> None:
    """输出稳定且紧凑的 JSON。

    Args:
        payload: 只含非敏感管理摘要的 JSON 可序列化对象。

    Returns:
        无返回值。

    """
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _parser() -> argparse.ArgumentParser:
    """构建所有 CLI 子命令共享的参数解析器。

    Args:
        无参数。

    Returns:
        已注册服务、索引和离线自检子命令的解析器。

    """
    parser = argparse.ArgumentParser(prog="rag-app")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "serve", help="启动 Product Runtime 与 React 控制台。"
    )
    subparsers.add_parser(
        "legacy-serve",
        help="已弃用：启动旧 Runtime，仅用于历史兼容。",
    )
    secrets_parser = subparsers.add_parser(
        "init-secrets",
        help="排他创建 0600 产品 Secret 文件。",
    )
    secret_target = secrets_parser.add_mutually_exclusive_group(required=True)
    secret_target.add_argument("--output", type=Path)
    secret_target.add_argument("--directory", type=Path)
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
    index_gc = subparsers.add_parser(
        "index-gc",
        help="默认 dry-run 规划安全索引垃圾回收。",
    )
    index_gc.add_argument("--apply", action="store_true")
    index_gc.add_argument(
        "--collection-prefix",
        default="rag-docx",
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
        default=Path("/app/deployment/assets/tokenizers/llm/tokenizer.json"),
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
