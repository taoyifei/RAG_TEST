import os
import shutil
import subprocess
from pathlib import Path


def _python310() -> Path:
    return Path(".venv-ocr310/bin/python").resolve(strict=True)


def _run_import(
    python_path: Path,
    source_root: Path,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    # 参数只来自严格解析的本地解释器和测试临时目录，不经过 shell。
    return subprocess.run(  # noqa: S603
        [
            str(python_path),
            "-c",
            "import rag_app.ocr.main",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )


def test_python310_imports_ocr_main_from_source() -> None:
    result = _run_import(_python310(), Path("src").resolve(strict=True))

    assert result.returncode == 0, result.stderr


def test_python310_imports_docker_copy_minimum_tree(tmp_path: Path) -> None:
    dockerfile = Path("deployment/ocr/Dockerfile").read_text(encoding="utf-8")
    source_copy_lines = tuple(
        line
        for line in dockerfile.splitlines()
        if line.startswith("COPY src/rag_app/")
    )
    assert source_copy_lines == (
        "COPY src/rag_app/__init__.py /opt/rag-ocr/src/rag_app/__init__.py",
        "COPY src/rag_app/ocr/ /opt/rag-ocr/src/rag_app/ocr/",
    )
    source_root = tmp_path / "src"
    package_root = source_root / "rag_app"
    package_root.mkdir(parents=True)
    shutil.copy2("src/rag_app/__init__.py", package_root / "__init__.py")
    shutil.copytree(
        "src/rag_app/ocr",
        package_root / "ocr",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    result = _run_import(_python310(), source_root)

    assert not (package_root / "contracts.py").exists()
    assert result.returncode == 0, result.stderr


def test_build_contexts_reexclude_python_caches() -> None:
    """验证重新纳入构建输入后仍排除 Python 缓存。

    Args:
        无参数。

    Returns:
        无返回值。

    """
    repository = Path(__file__).parents[1]
    for relative_path in (
        Path("Dockerfile.dockerignore"),
        Path("deployment/ocr/Dockerfile.dockerignore"),
    ):
        lines = (repository / relative_path).read_text(
            encoding="utf-8",
        ).splitlines()
        last_include = max(
            index for index, line in enumerate(lines) if line.startswith("!")
        )
        for exclusion in ("**/__pycache__/", "**/*.pyc", "**/*.pyo"):
            assert lines.index(exclusion) > last_include
