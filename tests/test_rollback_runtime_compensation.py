from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_rollback_persistence import (
    _APP_IMAGE,
    _assert_original_metadata,
    _prepare_sandbox,
    _run_rollback,
    _Sandbox,
)

_NEW_APP_IMAGE = "sha256:" + "d" * 64
_NEW_OCR_IMAGE = "sha256:" + "e" * 64
_NEW_QDRANT_IMAGE = "sha256:" + "f" * 64


def _container_state(sandbox: _Sandbox) -> dict[str, str]:
    """读取 fake docker 的容器状态。

    Args:
        sandbox: 回滚测试沙箱。

    Returns:
        状态键值映射。

    """
    return dict(
        line.split("=", maxsplit=1)
        for line in sandbox.state_file.read_text(
            encoding="ascii",
        ).splitlines()
    )


def _set_rollback_worker_target(
    sandbox: _Sandbox,
    *,
    running: bool,
) -> None:
    """设置部署时持久化的旧 worker 运行状态。

    Args:
        sandbox: 回滚测试沙箱。
        running: 旧 worker 是否应在成功回滚后运行。

    """
    value = str(running).lower()
    sandbox.rollback_file.write_text(
        sandbox.original_rollback.replace(
            "ROLLBACK_WORKER_WAS_RUNNING=false",
            f"ROLLBACK_WORKER_WAS_RUNNING={value}",
        ),
        encoding="utf-8",
    )


def _set_core_state(
    sandbox: _Sandbox,
    *,
    service: str,
    exists: bool,
    running: bool,
) -> None:
    """设置回滚调用前的核心容器 degraded 状态。

    Args:
        sandbox: 回滚测试沙箱。
        service: APP、OCR 或 QDRANT。
        exists: 容器是否存在。
        running: 容器是否运行。

    """
    state = _container_state(sandbox)
    state[f"{service}_EXISTS"] = str(exists).lower()
    state[f"{service}_RUNNING"] = str(running).lower()
    sandbox.state_file.write_text(
        "".join(f"{key}={value}\n" for key, value in state.items()),
        encoding="ascii",
    )


def _assert_original_runtime(
    sandbox: _Sandbox,
    *,
    worker_exists: bool,
    worker_running: bool,
) -> None:
    """断言容器已恢复到回滚调用前的目标。

    Args:
        sandbox: 回滚测试沙箱。
        worker_exists: 调用前 worker 是否存在。
        worker_running: 调用前 worker 是否运行。

    """
    state = _container_state(sandbox)
    assert state["APP_IMAGE"] == _NEW_APP_IMAGE
    assert state["OCR_IMAGE"] == _NEW_OCR_IMAGE
    assert state["QDRANT_IMAGE"] == _NEW_QDRANT_IMAGE
    assert state["APP_RUNNING"] == "true"
    assert state["OCR_RUNNING"] == "true"
    assert state["QDRANT_RUNNING"] == "true"
    assert state["WORKER_EXISTS"] == str(worker_exists).lower()
    assert state["WORKER_RUNNING"] == str(worker_running).lower()
    if worker_exists:
        assert state["WORKER_IMAGE"] == _NEW_APP_IMAGE


@pytest.mark.parametrize(
    "failure_overrides",
    (
        {"FAKE_COMPOSE_UP_FAIL": "1"},
        {"FAKE_BAD_CONTAINER": "rag-app"},
        {"FAKE_TARGET_QDRANT_HEALTH": "unhealthy"},
        {"FAKE_TARGET_OCR_HEALTH": "unhealthy"},
        {"FAKE_TARGET_APP_HEALTH": "unhealthy"},
        {"FAKE_TARGET_CURL_FAIL": "1"},
        {"FAKE_FAIL_ENV_REPLACE": "1"},
        {"FAKE_FAIL_CURRENT_RENAME": "1"},
        {"FAKE_CORRUPT_ENV_AFTER_CURRENT": "1"},
    ),
)
def test_failure_restores_running_worker_core_and_metadata(
    tmp_path: Path,
    failure_overrides: dict[str, str],
) -> None:
    """证明所有事务阶段失败都会恢复调用前完整状态。

    Args:
        tmp_path: pytest 临时目录。
        failure_overrides: fake 命令故障注入。

    """
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(
        sandbox,
        FAKE_WORKER_RUNNING="1",
        **failure_overrides,
    )

    assert completed.returncode == 1
    assert "ROLLBACK_FAILED_RECOVERED" in completed.stderr
    _assert_original_runtime(
        sandbox,
        worker_exists=True,
        worker_running=True,
    )
    _assert_original_metadata(sandbox)


def test_failure_removes_worker_that_compensation_target_did_not_have(
    tmp_path: Path,
) -> None:
    """证明失败补偿不会遗留回滚过程临时创建的 worker。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path)
    _set_rollback_worker_target(sandbox, running=True)

    completed = _run_rollback(
        sandbox,
        FAKE_WORKER_EXISTS="0",
        FAKE_FAIL_CURRENT_RENAME="1",
    )

    assert completed.returncode == 1
    assert "ROLLBACK_FAILED_RECOVERED" in completed.stderr
    _assert_original_runtime(
        sandbox,
        worker_exists=False,
        worker_running=False,
    )
    assert sandbox.env_file.read_text(encoding="utf-8") == sandbox.original_env
    assert sandbox.current_link.resolve() == sandbox.new_release
    assert "ROLLBACK_WORKER_WAS_RUNNING=true\n" in (
        sandbox.rollback_file.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "value",
    ("missing", "true\nROLLBACK_WORKER_WAS_RUNNING=false", "yes"),
)
def test_invalid_recorded_worker_state_fails_before_container_change(
    tmp_path: Path,
    value: str,
) -> None:
    """证明 worker 历史状态必须恰好一个布尔值。

    Args:
        tmp_path: pytest 临时目录。
        value: 无效记录场景。

    """
    sandbox = _prepare_sandbox(tmp_path)
    if value == "missing":
        content = sandbox.original_rollback.replace(
            "ROLLBACK_WORKER_WAS_RUNNING=false\n",
            "",
        )
    else:
        content = sandbox.original_rollback.replace(
            "ROLLBACK_WORKER_WAS_RUNNING=false",
            f"ROLLBACK_WORKER_WAS_RUNNING={value}",
        )
    sandbox.rollback_file.write_text(content, encoding="utf-8")

    completed = _run_rollback(sandbox)

    assert completed.returncode != 0
    _assert_original_runtime(
        sandbox,
        worker_exists=True,
        worker_running=False,
    )
    log = (
        sandbox.docker_log.read_text(encoding="utf-8")
        if sandbox.docker_log.exists()
        else ""
    )
    assert " up -d " not in log
    assert _APP_IMAGE not in _container_state(sandbox).values()


def test_compensation_health_failure_returns_stable_recovery_code(
    tmp_path: Path,
) -> None:
    """证明补偿自身失败会返回独立稳定类别。

    Args:
        tmp_path: pytest 临时目录。

    """
    sandbox = _prepare_sandbox(tmp_path)

    completed = _run_rollback(sandbox, FAKE_CURL_FAIL="1")

    assert completed.returncode == 70
    assert "ROLLBACK_FAILED_RECOVERY_FAILED" in completed.stderr
    assert "ROLLBACK_FAILED_RECOVERED" not in completed.stderr


@pytest.mark.parametrize(
    ("service", "exists", "running"),
    (
        ("APP", True, False),
        ("OCR", False, False),
    ),
)
def test_degraded_current_runtime_can_still_rollback(
    tmp_path: Path,
    service: str,
    exists: bool,
    running: bool,
) -> None:
    """证明 stopped 或缺失的当前核心不会阻止健康回滚。

    Args:
        tmp_path: pytest 临时目录。
        service: degraded 核心服务前缀。
        exists: 调用前是否存在。
        running: 调用前是否运行。

    """
    sandbox = _prepare_sandbox(tmp_path)
    _set_core_state(
        sandbox,
        service=service,
        exists=exists,
        running=running,
    )

    completed = _run_rollback(sandbox)

    assert completed.returncode == 0, completed.stderr
    state = _container_state(sandbox)
    for prefix in ("APP", "OCR", "QDRANT"):
        assert state[f"{prefix}_EXISTS"] == "true"
        assert state[f"{prefix}_RUNNING"] == "true"


@pytest.mark.parametrize(
    ("service", "exists", "running"),
    (
        ("APP", True, False),
        ("OCR", False, False),
    ),
)
def test_target_health_failure_restores_original_degraded_state(
    tmp_path: Path,
    service: str,
    exists: bool,
    running: bool,
) -> None:
    """证明目标不健康时补偿精确恢复调用前 degraded 状态。

    Args:
        tmp_path: pytest 临时目录。
        service: degraded 核心服务前缀。
        exists: 调用前是否存在。
        running: 调用前是否运行。

    """
    sandbox = _prepare_sandbox(tmp_path)
    _set_core_state(
        sandbox,
        service=service,
        exists=exists,
        running=running,
    )

    completed = _run_rollback(
        sandbox,
        FAKE_TARGET_QDRANT_HEALTH="unhealthy",
    )

    assert completed.returncode == 1
    assert "ROLLBACK_FAILED_RECOVERED" in completed.stderr
    state = _container_state(sandbox)
    assert state[f"{service}_EXISTS"] == str(exists).lower()
    assert state[f"{service}_RUNNING"] == str(running).lower()
    assert sandbox.env_file.read_text(encoding="utf-8") == sandbox.original_env
    assert sandbox.current_link.resolve() == sandbox.new_release
