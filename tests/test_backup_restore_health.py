from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_backup_script import (
    _container_states,
    _prepare_sandbox,
    _run_backup,
)


def test_qdrant_health_precedes_app_and_worker_restore(
    tmp_path: Path,
) -> None:
    """证明恢复顺序严格受 Qdrant 健康状态约束。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path)
    sandbox.state_file.write_text(
        "APP_RUNNING=true\n"
        "WORKER_RUNNING=true\n"
        "QDRANT_RUNNING=true\n",
        encoding="ascii",
    )

    completed = _run_backup(
        sandbox,
        FAKE_QDRANT_HEALTH_MODE="starting_then_healthy",
    )

    assert completed.returncode == 0, completed.stderr
    log = sandbox.command_log.read_text(encoding="utf-8")
    qdrant_up = log.index(
        " up -d --no-deps --no-build --pull never rag-qdrant"
    )
    last_health = log.rindex(".State.Health.Status")
    app_up = log.index(
        " up -d --no-deps --no-build --pull never rag-app"
    )
    app_live = log.index(
        "curl -fsS --max-time 10 http://127.0.0.1:8088/live"
    )
    worker_up = log.index(
        " up -d --no-deps --no-build --pull never rag-worker"
    )
    assert qdrant_up < last_health < app_up < app_live < worker_up
    assert log.count(".State.Health.Status") == 3
    assert log.count("sleep 1") == 2


@pytest.mark.parametrize(
    ("health_mode", "expected_polls"),
    (("unhealthy", 1), ("always_starting", 30)),
)
def test_unhealthy_or_timeout_never_starts_app(
    tmp_path: Path,
    health_mode: str,
    expected_polls: int,
) -> None:
    """证明 Qdrant 不健康时不会继续恢复依赖服务。

    Args:
        tmp_path: pytest 临时目录。
        health_mode: fake Qdrant 健康序列。
        expected_polls: 预期健康轮询次数。

    """
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_backup(
        sandbox,
        FAKE_QDRANT_HEALTH_MODE=health_mode,
    )

    assert completed.returncode == 70
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert log.count(".State.Health.Status") == expected_polls
    assert " up -d --no-deps --no-build --pull never rag-app" not in log
    assert "curl " not in log
    assert (sandbox.backups / "backup-a/MANIFEST.sha256").is_file()


def test_app_live_failure_after_qdrant_health_does_not_start_worker(
    tmp_path: Path,
) -> None:
    """证明 app 存活失败时 worker 不会提前恢复。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path)
    sandbox.state_file.write_text(
        "APP_RUNNING=true\n"
        "WORKER_RUNNING=true\n"
        "QDRANT_RUNNING=true\n",
        encoding="ascii",
    )

    completed = _run_backup(sandbox, FAKE_CURL_FAIL="1")

    assert completed.returncode == 70
    log = sandbox.command_log.read_text(encoding="utf-8")
    assert ".State.Health.Status" in log
    assert " up -d --no-deps --no-build --pull never rag-app" in log
    assert " up -d --no-deps --no-build --pull never rag-worker" not in log


@pytest.mark.parametrize("worker_was_running", (False, True))
def test_worker_restore_matches_original_running_state(
    tmp_path: Path,
    worker_was_running: bool,
) -> None:
    """证明 worker 只在备份前运行时通过 profile 恢复。

    Args:
        tmp_path: pytest 临时目录。
        worker_was_running: 备份前 worker 运行状态。

    """
    sandbox = _prepare_sandbox(tmp_path)
    sandbox.state_file.write_text(
        "APP_RUNNING=true\n"
        f"WORKER_RUNNING={str(worker_was_running).lower()}\n"
        "QDRANT_RUNNING=true\n",
        encoding="ascii",
    )

    completed = _run_backup(sandbox)

    assert completed.returncode == 0, completed.stderr
    log = sandbox.command_log.read_text(encoding="utf-8")
    worker_up = (
        " up -d --no-deps --no-build --pull never rag-worker" in log
    )
    assert worker_up is worker_was_running
    assert (
        f"WORKER_RUNNING={str(worker_was_running).lower()}"
        in _container_states(sandbox)
    )
