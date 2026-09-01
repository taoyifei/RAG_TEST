from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

_ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_INNER_IMPORTS = (
    "docx",
    "fastapi",
    "httpx",
    "lxml",
    "paddleocr",
    "qdrant_client",
    "rag_app.adapters",
    "rag_app.api",
    "rag_app.clients",
    "rag_app.composition",
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
    ".sqlite3",
    "-wal",
    "-shm",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
    ".gguf",
)
_INDUSTRY_PRIVATE_PREFIXES = (
    "deployment/industry/",
    "evaluation/industry/",
    "scripts/industry_bundle/",
    "scripts/industry_corpus/",
)
_INDUSTRY_PRIVATE_FILES = {
    "frontend/app-bearer.js",
    "frontend/index-bearer.html",
    "scripts/build_industry_app_update.py",
    "scripts/build_industry_bundle.py",
    "scripts/prepare_industry_corpus.py",
}
_SECRET_PATTERNS = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer[ \t]+[A-Za-z0-9._~-]{24,}\b"),
    ),
)
_MAX_SECRET_SCAN_BYTES = 2 * 1024 * 1024


def _inner_layer_files() -> tuple[Path, ...]:
    candidates = [
        _ROOT / "src/rag_app/application.py",
        _ROOT / "src/rag_app/core.py",
    ]
    for package_name in ("application", "core"):
        package = _ROOT / "src/rag_app" / package_name
        if package.is_dir():
            candidates.extend(package.rglob("*.py"))
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


def test_inner_layers_do_not_import_infrastructure() -> None:
    violations = {
        str(path.relative_to(_ROOT)): name
        for path in _inner_layer_files()
        for name in _import_names(path)
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in _FORBIDDEN_INNER_IMPORTS
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
        if ".data" in path.parts:
            violations.append(rendered)
        if any(
            rendered.startswith(prefix)
            for prefix in _INDUSTRY_PRIVATE_PREFIXES
        ) or rendered in _INDUSTRY_PRIVATE_FILES:
            violations.append(rendered)
        if any(
            part.casefold() in {
                "extracted",
                "uploaded",
                "uploads",
            }
            for part in path.parts
        ):
            violations.append(rendered)

    assert sorted(set(violations)) == []


def test_tracked_text_excludes_obvious_live_secrets() -> None:
    violations: list[str] = []
    for path in _tracked_paths():
        local_path = _ROOT.joinpath(*path.parts)
        if local_path.stat().st_size > _MAX_SECRET_SCAN_BYTES:
            continue
        payload = local_path.read_bytes()
        if b"\0" in payload:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line_number = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path.as_posix()}:{line_number}:{label}")

    assert violations == []
