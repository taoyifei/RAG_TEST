from __future__ import annotations

import ast
import json
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
    ".doc",
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
_NEW_PACKAGE_NAMES = ("adapters", "application", "composition", "core")
_SYNTHETIC_DOCX_ROOT = PurePosixPath("tests/fixtures/docx_v4")


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


def _declared_synthetic_docx_paths() -> frozenset[PurePosixPath]:
    manifest_path = _ROOT.joinpath(
        *_SYNTHETIC_DOCX_ROOT.parts,
        "manifest.json",
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    declared: set[PurePosixPath] = set()
    for item in payload:
        assert isinstance(item, dict)
        path = _SYNTHETIC_DOCX_ROOT / str(item["name"])
        assert path.parent == _SYNTHETIC_DOCX_ROOT
        assert path.suffix == ".docx"
        declared.add(path)
    return frozenset(declared)


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
    tracked = _tracked_paths()
    allowed_docx = _declared_synthetic_docx_paths()
    for path in tracked:
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
        ) and path not in allowed_docx:
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
    assert allowed_docx.issubset(set(tracked))


def test_runtime_python_sources_are_tracked() -> None:
    runtime_root = _ROOT / "src/rag_app"
    local_sources = {
        PurePosixPath(path.relative_to(_ROOT).as_posix())
        for path in runtime_root.rglob("*.py")
    }

    assert sorted(local_sources.difference(_tracked_paths())) == []


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


def _new_architecture_files() -> tuple[Path, ...]:
    files: list[Path] = []
    for package_name in _NEW_PACKAGE_NAMES:
        package = _ROOT / "src/rag_app" / package_name
        if package.is_dir():
            files.extend(package.rglob("*.py"))
    return tuple(sorted(files))


def test_adapters_do_not_import_api_routes() -> None:
    adapters = _ROOT / "src/rag_app/adapters"
    violations = {
        str(path.relative_to(_ROOT)): name
        for path in adapters.rglob("*.py")
        for name in _import_names(path)
        if name == "rag_app.api" or name.startswith("rag_app.api.")
    }
    assert violations == {}


def test_user_configuration_cannot_trigger_dynamic_import_or_eval() -> None:
    forbidden_calls: list[str] = []
    for path in _new_architecture_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {
                "__import__",
                "eval",
            }:
                forbidden_calls.append(str(path.relative_to(_ROOT)))
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
            ):
                forbidden_calls.append(str(path.relative_to(_ROOT)))
    assert forbidden_calls == []


def test_core_model_annotations_do_not_leak_any() -> None:
    violations: list[str] = []
    core_models = _ROOT / "src/rag_app/core/models"
    for path in core_models.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Name) and node.id == "Any"
            for node in ast.walk(tree)
        ):
            violations.append(str(path.relative_to(_ROOT)))
    assert violations == []


def test_document_version_ids_use_the_single_core_helper() -> None:
    """阻止业务代码重新只按内容摘要生成 dver。"""
    violations: list[str] = []
    identifiers = _ROOT / "src/rag_app/core/identifiers.py"
    for path in (_ROOT / "src/rag_app").rglob("*.py"):
        if path == identifiers:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id == "deterministic_id"
            ):
                continue
            namespace = node.args[0]
            if (
                isinstance(namespace, ast.Constant)
                and namespace.value == "dver"
            ):
                violations.append(
                    f"{path.relative_to(_ROOT).as_posix()}:{node.lineno}"
                )

    assert violations == []


def test_parser_adapters_have_no_blob_store_write_side_effects() -> None:
    parser_root = _ROOT / "src/rag_app/adapters/parsers"
    violations: list[str] = []
    for path in parser_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(_ROOT)
        violations.extend(
            f"{relative}:import:{name}"
            for name in _import_names(path)
            if name.endswith("blob_store")
        )
        violations.extend(
            f"{relative}:{node.lineno}:{node.func.attr}"
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"put", "put_if_absent", "delete"}
            )
        )

    assert violations == []


def test_new_architecture_packages_have_no_absolute_import_cycle() -> None:
    files = _new_architecture_files()
    module_by_path = {
        path: ".".join(path.relative_to(_ROOT / "src").with_suffix("").parts)
        for path in files
    }
    known_modules = set(module_by_path.values())
    graph = {
        module: {
            imported
            for imported in _import_names(path)
            if imported in known_modules
        }
        for path, module in module_by_path.items()
    }

    def visit(module: str, active: tuple[str, ...]) -> None:
        assert module not in active, " -> ".join((*active, module))
        for dependency in graph[module]:
            visit(dependency, (*active, module))

    for module_name in graph:
        visit(module_name, ())
