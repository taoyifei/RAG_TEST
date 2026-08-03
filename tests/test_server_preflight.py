"""验证服务器 preflight 只读、脱敏且可机器解析。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    """写入测试命令。

    Args:
        path: 命令路径。
        content: Shell 内容。

    """
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _snapshot(root: Path) -> tuple[tuple[str, int], ...]:
    """获取目录内路径和大小快照。

    Args:
        root: 快照根目录。

    Returns:
        稳定排序的相对路径与大小。

    """
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                path.stat().st_size if path.is_file() else -1,
            )
            for path in root.rglob("*")
        )
    )


def test_server_preflight_emits_redacted_json_without_side_effects(
    tmp_path: Path,
) -> None:
    """证明只读检查不写文件、不启动容器且不输出 token。

    Args:
        tmp_path: pytest 临时目录。

    """
    root = Path(__file__).parents[1]
    source = root / "deployment/server-preflight.sh"
    assert source.is_file()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    _write_executable(
        binaries / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "version --format {{.Server.Version}}") echo 27.5.1 ;;
  "compose version --short") echo 2.33.1 ;;
  "info --format {{json .DriverStatus}}")
    echo '[["Backing Filesystem","extfs"]]' ;;
  "info --format {{json .Runtimes}}")
    echo '{"io.containerd.runc.v2":{},"nvidia":{}}' ;;
  "ps -a --format {{.Names}}") echo rag-app ;;
  "network ls --format {{.Name}}") echo rag-internal ;;
  *) exit 91 ;;
esac
""",
    )
    _write_executable(
        binaries / "nvidia-smi",
        """#!/usr/bin/env bash
set -euo pipefail
echo '0, 24564, 24000'
""",
    )
    _write_executable(
        binaries / "df",
        """#!/usr/bin/env bash
set -euo pipefail
echo 'Filesystem 1B-blocks Used Available Use% Mounted on'
echo '/dev/fake 1000000000000 1 900000000000 1% /'
""",
    )
    _write_executable(
        binaries / "ss",
        """#!/usr/bin/env bash
set -euo pipefail
exit 0
""",
    )
    before = _snapshot(tmp_path)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{binaries}:/usr/bin:/bin",
            "RAG_QUERY_TOKEN": "never-print-this-token",
            "RAG_PREFLIGHT_PROJECT_ROOT": str(tmp_path / "missing-project"),
        }
    )

    completed = subprocess.run(  # noqa: S603
        ["/bin/bash", str(source)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    after = _snapshot(tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert before == after
    assert completed.stderr == ""
    assert "never-print-this-token" not in completed.stdout
    report = json.loads(completed.stdout)
    assert report["overall"] == "WARN"
    statuses = {item["name"]: item["status"] for item in report["checks"]}
    assert statuses == {
        "docker": "PASS",
        "fixed_directory": "WARN",
        "gpu": "PASS",
        "model_endpoints": "WARN",
        "ports": "PASS",
        "runtime_state": "WARN",
        "storage": "PASS",
    }


def test_server_preflight_source_has_no_mutating_commands() -> None:
    """锁定 preflight 不执行文件、镜像或容器写操作。"""
    source = (
        Path(__file__).parents[1] / "deployment/server-preflight.sh"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "docker load",
        "docker pull",
        "docker compose up",
        "docker run",
        "mkdir ",
        "install ",
        "rm ",
        "touch ",
    ):
        assert forbidden not in source
