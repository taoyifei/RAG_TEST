"""机械检查公开 Python callable 的 Google Args/Returns 小节。"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

_DEFAULT_ROOTS = (Path("src/rag_app"), Path("evaluation"), Path("scripts"))
_CHINESE_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def audit_paths(paths: Iterable[Path]) -> tuple[str, ...]:
    """检查公开 callable 的中文 Google docstring。

    Args:
        paths: 待递归检查的 Python 文件或目录。

    Returns:
        按文件与行号排序的缺失项。

    """
    findings: list[str] = []
    for path in _python_files(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ) or node.name.startswith("_"):
                continue
            docstring = ast.get_docstring(node, clean=True)
            if docstring is None:
                findings.append(f"{path}:{node.lineno}:{node.name}:docstring")
            if not _section_has_chinese(docstring, "Args"):
                findings.append(f"{path}:{node.lineno}:{node.name}:Args")
            if not _section_has_chinese(docstring, "Returns"):
                findings.append(f"{path}:{node.lineno}:{node.name}:Returns")
    return tuple(sorted(findings))


def _python_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files.update(path.rglob("*.py"))
        elif path.suffix == ".py":
            files.add(path)
    return tuple(sorted(files))


def _section_has_chinese(docstring: str | None, section: str) -> bool:
    if docstring is None:
        return False
    lines = docstring.splitlines()
    header = f"{section}:"
    for index, line in enumerate(lines):
        if line.strip() != header:
            continue
        body: list[str] = []
        for candidate in lines[index + 1 :]:
            stripped = candidate.strip()
            if (
                stripped
                and candidate == candidate.lstrip()
                and stripped.endswith(":")
            ):
                break
            body.append(candidate)
        return _CHINESE_PATTERN.search("\n".join(body)) is not None
    return False


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--all",
        action="store_true",
        help="检查全量三根目录；这是默认行为的兼容别名。",
    )
    mode.add_argument(
        "--changed",
        action="store_true",
        help="仅检查 Git 工作区中已修改、已暂存和未跟踪的 Python 文件。",
    )
    return parser.parse_args()


def _changed_python_files(repository_root: Path) -> tuple[Path, ...]:
    commands = (
        ("git", "diff", "--name-only", "--diff-filter=ACMR"),
        ("git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"),
        ("git", "ls-files", "--others", "--exclude-standard"),
    )
    paths: set[Path] = set()
    for command in commands:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        for value in completed.stdout.splitlines():
            path = Path(value)
            if path.suffix == ".py" and not path.is_relative_to(Path("tests")):
                paths.add(path)
    return tuple(sorted(paths))


def main() -> int:
    """运行 Google docstring 机械门禁。

    Args:
        无参数；命令行选项从当前进程读取。

    Returns:
        无缺失项返回 0，否则返回 1。

    """
    arguments = _arguments()
    paths = (
        _changed_python_files(Path.cwd())
        if arguments.changed
        else _DEFAULT_ROOTS
    )
    findings = audit_paths(paths)
    for finding in findings:
        print(finding)
    print(f"missing_google_sections={len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
