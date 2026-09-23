"""用现有湾事通部署环境单次刷新 F05 问题运营榜。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from time import monotonic, sleep

from rag_app.adapters.stores.sqlite_connection import SqliteConnectionFactory
from rag_app.composition.product_runtime import ProductRuntimeSettings
from rag_app.product.crypto import SecretCipher, load_master_key
from rag_app.product.query_history import ProductQueryHistory
from rag_app.wanshitong.models import ScopeBinding
from rag_app.wanshitong.question_analytics import QuestionAnalyticsService
from rag_app.wanshitong.scope_store import ScopeBindingStore
from rag_app.wanshitong.settings import WanshitongSettings

EXIT_COMPLETE = 0
EXIT_LIMITED = 10
EXIT_FAILED = 11
EXIT_TIMEOUT = 12
EXIT_ERROR = 13
_TERMINAL_STATES = frozenset({"COMPLETE", "FAILED", "LIMITED"})
_POLL_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 3600


def _positive_timeout(value: str) -> int:
    """只接受有界的任务等待秒数。"""
    try:
        seconds = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("等待秒数必须是整数。") from error
    if not 1 <= seconds <= _MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError("等待秒数必须在 1 到 3600 之间。")
    return seconds


def _check_database(database: Path) -> Path:
    """确认复用现有 WAL 数据库，不创建目录或修改 journal mode。"""
    if database.is_symlink() or not database.is_file():
        raise ValueError("现有湾事通 SQLite 数据库不可用。")
    resolved = database.resolve(strict=True)
    with closing(
        sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    ) as connection:
        journal_mode = str(
            connection.execute("PRAGMA journal_mode").fetchone()[0]
        )
    if journal_mode.upper() != "WAL":
        raise ValueError("问题榜单次刷新要求现有 SQLite 已使用 WAL。")
    return resolved


def _active_binding(
    connections: SqliteConnectionFactory,
) -> ScopeBinding:
    """只读取已绑定且仍活动的固定 Project/KB。"""
    binding = ScopeBindingStore(connections).read()
    if binding is None:
        raise ValueError("湾事通固定 Scope 尚未绑定。")
    with connections.transaction() as connection:
        active = connection.execute(
            "SELECT 1 FROM projects p JOIN knowledge_bases kb "
            "ON kb.project_id=p.project_id WHERE p.project_id=? "
            "AND kb.knowledge_base_id=? AND p.deleted_at IS NULL "
            "AND kb.deleted_at IS NULL AND p.lifecycle_status='active' "
            "AND kb.lifecycle_status='active'",
            (binding.project_id, binding.knowledge_base_id),
        ).fetchone()
    if active is None:
        raise ValueError("湾事通固定 Scope 已失效或正在删除。")
    return binding


def _build_service() -> tuple[QuestionAnalyticsService, ScopeBinding]:
    """从与 Web 进程相同的配置读取 SQLite、主密钥和部署标识。"""
    product = ProductRuntimeSettings.from_environment()
    wanshitong = WanshitongSettings.from_environment()
    if not wanshitong.enabled:
        raise ValueError("必须在湾事通产品模式运行问题榜单次刷新。")
    if product.master_key_file is None:
        raise ValueError("问题榜单次刷新必须配置 RAG_MASTER_KEY_FILE。")
    if product.data_dir.is_symlink():
        raise ValueError("RAG_DATA_DIR 禁止 symlink。")
    database = _check_database(product.data_dir / "universal-rag.sqlite3")
    connections = SqliteConnectionFactory(database)
    binding = _active_binding(connections)
    cipher = SecretCipher(load_master_key(product.master_key_file))
    history = ProductQueryHistory(
        connections,
        cipher,
        save_body=product.history_save_body,
        retention_days=product.history_retention_days,
    )
    service = QuestionAnalyticsService(
        connections,
        history,
        deployment_id=wanshitong.sso.deployment_id or "LEGACY_UNKNOWN",
        retention_days=product.history_retention_days,
    )
    return service, binding


def _wait_for_run(
    service: QuestionAnalyticsService,
    binding: ScopeBinding,
    run_id: str,
    timeout_seconds: int,
) -> dict[str, object]:
    """等待当前 run 完成；超时则返回最后一次 BUILDING 状态。"""
    deadline = monotonic() + timeout_seconds
    while True:
        run = service.run(
            run_id,
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        )
        if run.get("state") in _TERMINAL_STATES:
            return run
        remaining = deadline - monotonic()
        if remaining <= 0:
            return run
        sleep(min(_POLL_SECONDS, remaining))


def _emit(run: dict[str, object], *, state: str | None = None) -> None:
    """只输出不含正文、密钥和数据路径的监控摘要。"""
    result = {
        "run_id": run.get("run_id"),
        "state": state or run.get("state"),
        "failure_code": run.get("failure_code"),
        "deployment_id": run.get("deployment_id"),
        "source_total": run.get("source_total"),
        "eligible_count": run.get("eligible_count"),
        "finished_at": run.get("finished_at"),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    """执行一次刷新并以退出码区分完成、限额、失败与超时。

    Args:
        argv: 可选命令行参数；默认读取当前进程参数。

    Returns:
        0 为 COMPLETE，10 为 LIMITED，11 为 FAILED，12 为超时，
        13 为配置或调用错误。参数错误沿用 argparse 的退出码 2。

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_timeout,
        default=300,
        help="本次统计终态的最长等待时间，默认 300 秒。",
    )
    args = parser.parse_args(argv)
    try:
        service, binding = _build_service()
        started = service.refresh(
            project_id=binding.project_id,
            knowledge_base_id=binding.knowledge_base_id,
        )
        run = _wait_for_run(
            service,
            binding,
            str(started["run_id"]),
            args.timeout_seconds,
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "state": "ERROR",
                    "failure_code": "CLI_" + type(error).__name__.upper(),
                },
                sort_keys=True,
            )
        )
        return EXIT_ERROR
    state = str(run.get("state"))
    if state == "COMPLETE":
        _emit(run)
        return EXIT_COMPLETE
    if state == "LIMITED":
        _emit(run)
        return EXIT_LIMITED
    if state == "FAILED":
        _emit(run)
        return EXIT_FAILED
    if state == "BUILDING":
        _emit(run, state="TIMEOUT")
        return EXIT_TIMEOUT
    _emit(run, state="ERROR")
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
