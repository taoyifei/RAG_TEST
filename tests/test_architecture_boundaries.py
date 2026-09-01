from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path, PurePosixPath

_ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_CORE_IMPORTS = (
    "fastapi",
    "qdrant_client",
    "rag_app.api",
    "rag_app.clients",
    "rag_app.ocr",
)
_FORBIDDEN_TRACKED_SUFFIXES = (
    ".docx",
    ".pdf",
    ".pfx",
    ".p12",
    ".pem",
    ".key",
    ".zip",
    ".7z",
    ".tar",
    ".tar.gz",
)


def _core_files() -> tuple[Path, ...]:
    candidates = [_ROOT / "src/rag_app/core.py"]
    core_package = _ROOT / "src/rag_app/core"
    if core_package.is_dir():
        candidates.extend(core_package.rglob("*.py"))
    return tuple(path for path in candidates if path.is_file())


def _import_names(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return tuple(names)


def _tracked_paths() -> tuple[PurePosixPath, ...]:
    git = shutil.which("git")
    assert git is not None
    completed = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        PurePosixPath(item.decode("utf-8"))
        for item in completed.stdout.split(b"\0")
        if item
    )


def test_core_does_not_import_infrastructure() -> None:
    violations = {
        str(path.relative_to(_ROOT)): name
        for path in _core_files()
        for name in _import_names(path)
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in _FORBIDDEN_CORE_IMPORTS
        )
    }

    assert violations == {}


def test_tracked_tree_excludes_sensitive_and_industry_payloads() -> None:
    violations: list[str] = []
    for path in _tracked_paths():
        rendered = path.as_posix()
        lowered = rendered.casefold()
        if path.name == ".env" or path.name.casefold() in {
            "id_rsa",
            "id_ed25519",
            "secrets.json",
        }:
            violations.append(rendered)
        if any(
            lowered.endswith(suffix)
            for suffix in _FORBIDDEN_TRACKED_SUFFIXES
        ):
            violations.append(rendered)
        if rendered.startswith("deployment/industry/"):
            violations.append(rendered)
        if path.parts and path.parts[0].casefold() in {
            "uploads",
            "uploaded",
            "extracted",
        }:
            violations.append(rendered)

    assert sorted(set(violations)) == []
