"""提供跨平台、默认离线且返回码透明的统一开发入口。"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
_OFFLINE_MARK_EXPRESSION = "not local_integration and not live_provider"
_SMOKE_TESTS = (
    "tests/test_health_api.py",
    "tests/test_docx_parser.py",
    "tests/test_chunker.py",
    "tests/test_rrf.py",
    "tests/test_rerank_stage.py",
    "tests/test_answer_guard.py",
    "tests/test_architecture_boundaries.py",
)


def _doctor_python() -> str:
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("需要 Python 3.11。")
    return sys.version.split()[0]


def _doctor_git() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("找不到 Git。")
    completed = subprocess.run(  # noqa: S603
        [executable, "rev-parse", "--show-toplevel"],
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    observed_root = Path(completed.stdout.strip()).resolve()
    if observed_root != _REPOSITORY_ROOT:
        raise RuntimeError("Git 根目录与 scripts/dev.py 所在项目不一致。")
    return executable


def _doctor_project_import() -> str:
    sys.path.insert(0, str(_SOURCE_ROOT))
    try:
        module = importlib.import_module("rag_app")
    finally:
        sys.path.remove(str(_SOURCE_ROOT))
    module_path = Path(module.__file__ or "").resolve()
    if not module_path.is_relative_to(_SOURCE_ROOT):
        raise RuntimeError("rag_app 未从当前源码树导入。")
    try:
        version = importlib.metadata.version("docx-rag")
    except importlib.metadata.PackageNotFoundError:
        return "source-tree"
    return f"installed={version}; source-tree"


def _doctor_sqlite_fts5() -> str:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        connection.execute("INSERT INTO probe(content) VALUES ('offline')")
        count = connection.execute(
            "SELECT count(*) FROM probe WHERE probe MATCH 'offline'"
        ).fetchone()
    if count != (1,):
        raise RuntimeError("SQLite FTS5 查询结果不符合预期。")
    return sqlite3.sqlite_version


def _doctor_temp_directory() -> str:
    with tempfile.TemporaryDirectory(prefix="rag-doctor-") as temporary:
        probe = Path(temporary) / "write-probe"
        probe.write_text("ok", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "ok":
            raise RuntimeError("临时目录读写校验失败。")
    return tempfile.gettempdir()


def _run_doctor() -> int:
    checks = (
        ("python", _doctor_python),
        ("git", _doctor_git),
        ("project_import", _doctor_project_import),
        ("sqlite_fts5", _doctor_sqlite_fts5),
        ("temp_directory", _doctor_temp_directory),
    )
    for name, check in checks:
        try:
            detail = check()
        except (
            OSError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as error:
            print(f"FAIL {name}: {error}", file=sys.stderr)
            return 1
        print(f"OK {name}: {detail}")
    print("SKIP node: optional in a later phase")
    return 0


def _check_commands() -> tuple[tuple[str, ...], ...]:
    python = sys.executable
    return (
        (
            python,
            "-m",
            "compileall",
            "-q",
            "src",
            "tests",
            "scripts",
            "evaluation",
        ),
        (python, "-m", "ruff", "check", "."),
        (
            python,
            "-m",
            "mypy",
            "--no-incremental",
            "src",
            "evaluation",
            "scripts",
        ),
        (python, "scripts/check_google_docstrings.py"),
        (
            python,
            "-m",
            "pytest",
            "-q",
            "-m",
            _OFFLINE_MARK_EXPRESSION,
        ),
    )


def _smoke_commands() -> tuple[tuple[str, ...], ...]:
    return ((sys.executable, "-m", "pytest", "-q", *_SMOKE_TESTS),)


def _offline_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("RAG_"):
            environment.pop(name)
    environment["RAG_TEST_NETWORK"] = "offline"
    environment["NO_PROXY"] = "*"
    environment["no_proxy"] = "*"
    return environment


def _run_commands(commands: Sequence[Sequence[str]]) -> int:
    environment = _offline_environment()
    for command in commands:
        print(f"RUN {shlex.join(command)}", flush=True)
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=_REPOSITORY_ROOT,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            return completed.returncode
    return 0


def _arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("doctor", "check", "smoke"))
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """运行统一开发入口。

    Args:
        arguments: 可选命令行参数；默认读取当前进程参数。

    Returns:
        全部检查通过时返回 0，否则返回首个失败命令的原始返回码。

    """
    command = _arguments(arguments).command
    if command == "doctor":
        return _run_doctor()
    if command == "check":
        return _run_commands(_check_commands())
    return _run_commands(_smoke_commands())


if __name__ == "__main__":
    raise SystemExit(main())
