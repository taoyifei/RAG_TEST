"""提供跨平台、默认离线且返回码透明的统一开发入口。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import cast

from rag_app._build_revision import SOURCE_REVISION
from rag_app.adapters.providers import (
    AliyunQwen37EmbeddingAdapter,
    AliyunQwen37EmbeddingConfig,
    JinaEmbeddingConfig,
    JinaV5TextEmbeddingAdapter,
)
from rag_app.application.embedding_router import (
    ActiveRevisionEmbeddingState,
    EmbeddingFailoverRouter,
    QueryEmbeddingRequest,
)
from rag_app.composition import (
    ComponentRegistry,
    build_components,
    default_hot_standby_profile,
    default_offline_profile,
    load_profile,
    register_builtin_components,
)
from rag_app.composition.chunking_cli import (
    chunk_ablation_command,
    chunk_document_command,
)
from rag_app.composition.p06_cli import P06_COMMANDS, p06_command
from rag_app.composition.p09_cli import P09_COMMANDS, p09_command
from rag_app.composition.provider_profiles import load_provider_catalog
from rag_app.core.capabilities import (
    ComponentCapabilities,
    ComponentDescriptor,
    ComponentKind,
    ProviderMode,
)
from rag_app.core.errors import (
    DenseUnavailable,
    ProviderInvalidResponse,
    ProviderRateLimited,
    ProviderUnavailable,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    DocumentRef,
    EmbeddingCoverage,
    EmbeddingRequest,
    EmbeddingRequestRole,
    EmbeddingResult,
    EmbeddingSlotIdentity,
    ParseContext,
    ParseSource,
    ProviderHealth,
    ProviderHealthStatus,
    canonical_document_ir_json,
)
from rag_app.core.policies import EgressPolicy

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))
_P08_CLI = importlib.import_module("evaluation.v2.cli")
P08_COMMANDS = cast(frozenset[str], _P08_CLI.P08_COMMANDS)
p08_command = cast(
    Callable[[Sequence[str]], int],
    _P08_CLI.p08_command,
)
_OFFLINE_MARK_EXPRESSION = "not local_integration and not live_provider"
_SMOKE_TESTS = (
    (
        "tests/adapters/parsers/docx/test_snapshots.py::"
        "test_restart_fixture_parses_with_v4"
    ),
    (
        "tests/adapters/chunkers/test_docx_structural.py::"
        "test_table_merge_and_nested_table_keep_real_source_relationships"
    ),
    "tests/test_health_api.py",
    "tests/test_docx_parser.py",
    "tests/test_chunker.py",
    "tests/test_rrf.py",
    "tests/test_rerank_stage.py",
    "tests/test_answer_guard.py",
    "tests/test_architecture_boundaries.py",
    (
        "tests/e2e/test_p06_revision_lifecycle.py::"
        "test_p06_revision_lifecycle_survives_reopen"
    ),
    "tests/e2e/test_p07_retrieval.py",
    (
        "tests/evaluation/test_dataset.py::"
        "test_synthetic_dataset_is_versioned_and_group_isolated"
    ),
    (
        "tests/evaluation/test_artifacts_and_guards.py::"
        "test_live_lane_requires_explicit_authorization"
    ),
)
_PROVIDER_ENV_NAMES = frozenset(
    {
        "JINA_API_KEY",
        "DASHSCOPE_API_KEY",
        "ALIYUN_MODEL_STUDIO_WORKSPACE_ID",
        "ALIYUN_MODEL_STUDIO_REGION",
    }
)
_FAILOVER_SCENARIOS = (
    "jina-timeout",
    "jina-429",
    "jina-bad-dimension",
    "both-unavailable",
)
_WEB_COMMANDS = frozenset(
    {
        "web-install-check",
        "web-lint",
        "web-typecheck",
        "web-test",
        "web-build",
        "web-e2e",
    }
)
_PRODUCT_TESTS = (
    "tests/security/test_secret_store.py",
    "tests/api/test_model_services.py",
    "tests/api/test_retrieval_profiles.py",
    "tests/api/test_console_session.py",
    "tests/api/test_access_tokens.py",
    "tests/composition/test_product_runtime.py",
    "tests/composition/test_product_deployment.py",
    "tests/cli/test_product_serve.py",
    "tests/application/test_profile_impact.py",
    "tests/application/test_provider_runtime_registry.py",
)
_IDENTITY_GATE = "DEV_RUNTIME_IDENTITY_GATE"
_IDENTITY_JSON_MAX_BYTES = 1024 * 1024
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,159}$")
_API_SCHEMA_PATH = Path("docs/public/openapi-v1.json")
_FRONTEND_PACKAGE_PATH = Path("frontend/package.json")
_COMPATIBILITY_MANIFEST_PATH = Path("compatibility-manifest.json")


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
        if name.startswith("RAG_") or name in _PROVIDER_ENV_NAMES:
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


def _frontend_npm() -> str | None:
    """返回可执行的本机 npm，不接受 Windows cmd shim。"""
    candidates = (str(Path.home() / ".local/bin/npm"), shutil.which("npm"))
    for candidate in candidates:
        if (
            candidate
            and not candidate.startswith("/mnt/c/")
            and Path(candidate).is_file()
            and os.access(candidate, os.X_OK)
        ):
            return candidate
    return None


def _run_web_script(script: str) -> int:
    """执行一个锁定在 frontend package 的 npm script。"""
    npm = _frontend_npm()
    if npm is None:
        print("BLOCKED web: 未找到可执行的 Linux npm。", file=sys.stderr)
        return 2
    environment = _offline_environment()
    environment["PATH"] = (
        f"{Path(sys.executable).parent}{os.pathsep}"
        f"{Path(npm).parent}{os.pathsep}{environment.get('PATH', '')}"
    )
    command = (npm, "--prefix", "frontend", "run", script)
    print(f"RUN {shlex.join(command)}", flush=True)
    completed = subprocess.run(  # noqa: S603
        command,
        cwd=_REPOSITORY_ROOT,
        env=environment,
        check=False,
    )
    return completed.returncode


def _web_install_check() -> int:
    """验证 Node、锁文件和已安装依赖，不隐式下载。"""
    npm = _frontend_npm()
    package_lock = _REPOSITORY_ROOT / "frontend" / "package-lock.json"
    if npm is None or not package_lock.is_file():
        print(
            "BLOCKED web-install-check: 需要 Linux Node/npm "
            "和 package-lock.json。",
            file=sys.stderr,
        )
        return 2
    environment = _offline_environment()
    environment["PATH"] = (
        f"{Path(npm).parent}{os.pathsep}{environment.get('PATH', '')}"
    )
    completed = subprocess.run(  # noqa: S603
        [npm, "--prefix", "frontend", "ls", "--depth=0"],
        cwd=_REPOSITORY_ROOT,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        print(
            "BLOCKED web-install-check: 请在 frontend 执行 npm ci。",
            file=sys.stderr,
        )
    return completed.returncode


def _windows_path(path: Path) -> str:
    """将 WSL 路径转换为 Windows Node 可读取的绝对路径。"""
    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/wslpath", "-w", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _wait_for_p10(port: int) -> bool:
    """在有限时间内等待 loopback P10 服务就绪。"""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", "/ready")
            if connection.getresponse().status == HTTPStatus.OK:
                return True
        except OSError:
            time.sleep(0.1)
        finally:
            connection.close()
    return False


def _web_e2e(profile: Path | None) -> int:
    """运行真实离线 Playwright；WSL 可复用已安装的 Windows Chrome。"""
    build_result = _run_web_script("build")
    if build_result != 0:
        return build_result
    node = shutil.which("node.exe")
    chrome = Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")
    if node is None or not chrome.is_file():
        return _run_web_script("e2e")
    server_command = [
        sys.executable,
        "scripts/serve_p10.py",
        "--port",
        "8091",
        "--frontend-dir",
        "frontend/dist",
    ]
    if profile is not None:
        server_command.extend(("--profile", str(profile)))
    external = os.environ.get("P10_EXTERNAL_SERVER") == "1"
    server = (
        None
        if external
        else subprocess.Popen(  # noqa: S603
            server_command,
            cwd=_REPOSITORY_ROOT,
            env=_offline_environment(),
        )
    )
    try:
        if not external and not _wait_for_p10(8091):
            print("BLOCKED web-e2e: P10 loopback 服务未就绪。", file=sys.stderr)
            return 2
        environment = _offline_environment()
        environment.update(
            {
                "P10_EXTERNAL_SERVER": "1",
                "P10_BROWSER_CHANNEL": "chrome",
                "P10_BASE_URL": os.environ.get(
                    "P10_BASE_URL", "http://127.0.0.1:8091"
                )
                if external
                else "http://127.0.0.1:8091",
            }
        )
        wsl_environment = environment.get("WSLENV", "")
        p10_environment = (
            "P10_EXTERNAL_SERVER/w:P10_BROWSER_CHANNEL/w:P10_BASE_URL/w"
        )
        environment["WSLENV"] = (
            f"{p10_environment}:{wsl_environment}"
            if wsl_environment
            else p10_environment
        )
        cli = _windows_path(
            _REPOSITORY_ROOT / "frontend/node_modules/@playwright/test/cli.js"
        )
        config = _windows_path(
            _REPOSITORY_ROOT / "frontend/playwright.config.ts"
        )
        completed = subprocess.run(  # noqa: S603
            [node, cli, "test", f"--config={config}"],
            cwd=_REPOSITORY_ROOT,
            env=environment,
            check=False,
        )
        return completed.returncode
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def _arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "doctor",
            "check",
            "smoke",
            "provider-list",
            "provider-check",
            "provider-smoke",
            "failover-smoke",
            "product-check",
            "product-smoke",
            "runtime-identity",
            "inspect-document",
            "chunk-document",
            "chunk-ablation",
            *sorted(_WEB_COMMANDS),
        ),
    )
    parser.add_argument("document_path", nargs="?", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--build-context-manifest", type=Path)
    parser.add_argument("--image-inspect-json", type=Path)
    parser.add_argument("--runtime-status-json", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--include-content", action="store_true")
    parser.add_argument("--provider", choices=("jina", "aliyun-qwen37"))
    parser.add_argument("--scenario", choices=_FAILOVER_SCENARIOS)
    parsed = parser.parse_args(arguments)
    if parsed.command == "provider-check" and parsed.profile is None:
        parser.error("provider-check 必须提供 --profile。")
    if parsed.command == "provider-smoke" and parsed.provider is None:
        parser.error("provider-smoke 必须提供 --provider。")
    if parsed.command == "failover-smoke" and parsed.scenario is None:
        parser.error("failover-smoke 必须提供 --scenario。")
    if parsed.command == "inspect-document" and parsed.document_path is None:
        parser.error("inspect-document 必须提供文档路径。")
    if parsed.command == "chunk-document" and parsed.document_path is None:
        parser.error("chunk-document 必须提供文档路径。")
    if parsed.command == "chunk-ablation":
        if parsed.document_path is None:
            parser.error("chunk-ablation 必须提供文档或目录路径。")
        if parsed.output is None:
            parser.error("chunk-ablation 必须提供 --output。")
    return parsed


def _identity_git(*arguments: str) -> bytes:
    """执行不经过 shell 的只读 Git 身份查询。"""
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("GIT_UNAVAILABLE")
    completed = subprocess.run(  # noqa: S603
        (executable, *arguments),
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("GIT_IDENTITY_QUERY_FAILED")
    return completed.stdout


def _source_tree_identity() -> dict[str, object]:
    """读取当前源码树对应的完整 Git 提交身份。"""
    revision = _identity_git("rev-parse", "--verify", "HEAD^{commit}")
    source_tree_revision = revision.decode("ascii").strip()
    if _REVISION_PATTERN.fullmatch(source_tree_revision) is None:
        raise RuntimeError("GIT_HEAD_INVALID")
    tracked_changes = subprocess.run(  # noqa: S603
        (
            shutil.which("git") or "git",
            "diff-index",
            "--quiet",
            "HEAD",
            "--",
        ),
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
    )
    if tracked_changes.returncode not in {0, 1}:
        raise RuntimeError("GIT_WORKTREE_QUERY_FAILED")
    untracked = _identity_git("ls-files", "--others", "--exclude-standard")
    dirty = tracked_changes.returncode == 1 or bool(untracked)
    return {
        "status": "BLOCKED" if dirty else "PASS",
        "git_head": source_tree_revision,
        "source_tree_revision": source_tree_revision,
        "worktree_dirty": dirty,
        "reason": ("SOURCE_TREE_DIRTY" if dirty else "SOURCE_TREE_CLEAN"),
    }


def _build_revision_identity(source_tree_revision: str) -> dict[str, object]:
    """核对 wheel 内嵌 revision，源码树占位值不伪装成提交。"""
    if SOURCE_REVISION == "development-unset":
        return {
            "status": "NOT_APPLICABLE",
            "build_revision": SOURCE_REVISION,
            "matches_source_tree": None,
            "reason": "SOURCE_TREE_BUILD_REVISION_UNSET",
        }
    valid = _REVISION_PATTERN.fullmatch(SOURCE_REVISION) is not None
    matches = valid and source_tree_revision == SOURCE_REVISION
    return {
        "status": "PASS" if matches else "BLOCKED",
        "build_revision": SOURCE_REVISION if valid else None,
        "matches_source_tree": matches,
        "reason": (
            "BUILD_REVISION_MATCH"
            if matches
            else "BUILD_REVISION_INVALID_OR_MISMATCH"
        ),
    }


def _without_duplicate_keys(
    pairs: Sequence[tuple[str, object]],
) -> dict[str, object]:
    """拒绝会让身份字段含义不唯一的重复 JSON key。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("IDENTITY_JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _load_identity_json(path: Path) -> Mapping[str, object] | Sequence[object]:
    """读取有大小上限且无重复 key 的本地身份 JSON。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("IDENTITY_JSON_NOT_REGULAR_FILE")
    content = path.read_bytes()
    if len(content) > _IDENTITY_JSON_MAX_BYTES:
        raise ValueError("IDENTITY_JSON_TOO_LARGE")
    payload = json.loads(
        content.decode("utf-8"), object_pairs_hook=_without_duplicate_keys
    )
    if not isinstance(payload, (Mapping, Sequence)) or isinstance(
        payload, (str, bytes, bytearray)
    ):
        raise ValueError("IDENTITY_JSON_INVALID_ROOT")
    return payload


def _mapping(value: object, reason: str) -> Mapping[str, object]:
    """要求身份片段为 JSON object。"""
    if not isinstance(value, Mapping):
        raise ValueError(reason)
    return value


def _tracked_file_bytes(relative_path: Path) -> bytes:
    """读取当前工作树中已被 Git 跟踪的普通文件。"""
    _identity_git(
        "ls-files",
        "--error-unmatch",
        "--",
        relative_path.as_posix(),
    )
    path = _REPOSITORY_ROOT / relative_path
    if path.is_symlink() or not path.is_file():
        raise ValueError("TRACKED_IDENTITY_FILE_INVALID")
    content = path.read_bytes()
    if len(content) > _IDENTITY_JSON_MAX_BYTES:
        raise ValueError("TRACKED_IDENTITY_FILE_TOO_LARGE")
    return content


def _tracked_json(relative_path: Path) -> tuple[Mapping[str, object], bytes]:
    """读取已跟踪的无重复 key JSON object。"""
    content = _tracked_file_bytes(relative_path)
    payload = json.loads(
        content.decode("utf-8"), object_pairs_hook=_without_duplicate_keys
    )
    return _mapping(payload, "TRACKED_IDENTITY_JSON_INVALID"), content


def _api_schema_identity() -> dict[str, object]:
    """计算当前已跟踪 OpenAPI schema 的实际 SHA-256。"""
    payload, content = _tracked_json(_API_SCHEMA_PATH)
    if not isinstance(payload.get("openapi"), str):
        raise ValueError("API_SCHEMA_VERSION_MISSING")
    return {
        "status": "PASS",
        "path": _API_SCHEMA_PATH.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _safe_identity(value: object, reason: str) -> str:
    """只允许输出短且不含空白的非敏感身份值。"""
    if (
        not isinstance(value, str)
        or _SAFE_IDENTITY_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(reason)
    return value


def _frontend_identity() -> dict[str, object]:
    """从已跟踪 package 与兼容清单计算前端构建身份。"""
    package, package_content = _tracked_json(_FRONTEND_PACKAGE_PATH)
    compatibility, compatibility_content = _tracked_json(
        _COMPATIBILITY_MANIFEST_PATH
    )
    package_name = _safe_identity(package.get("name"), "PACKAGE_NAME_INVALID")
    package_version = _safe_identity(
        package.get("version"), "PACKAGE_VERSION_INVALID"
    )
    build_identity = f"{package_name}@{package_version}"
    manifest_identity = _safe_identity(
        compatibility.get("frontend_build_id"),
        "FRONTEND_MANIFEST_IDENTITY_INVALID",
    )
    matches = build_identity == manifest_identity
    return {
        "status": "PASS" if matches else "BLOCKED",
        "build_identity": build_identity,
        "compatibility_manifest_identity": manifest_identity,
        "matches_compatibility_manifest": matches,
        "package_manifest_sha256": hashlib.sha256(package_content).hexdigest(),
        "compatibility_manifest_sha256": hashlib.sha256(
            compatibility_content
        ).hexdigest(),
        "build_artifact_status": "NOT_RUN",
    }


def _build_context_identity(
    path: Path | None, source_tree_revision: str
) -> dict[str, object]:
    """核对显式提供的受控 build context manifest。"""
    if path is None:
        return {
            "status": "NOT_RUN",
            "source_revision": None,
            "matches_source_tree": None,
            "reason": "BUILD_CONTEXT_MANIFEST_NOT_PROVIDED",
        }
    payload = _mapping(
        _load_identity_json(path), "BUILD_CONTEXT_MANIFEST_INVALID"
    )
    revision = payload.get("source_revision")
    if (
        not isinstance(revision, str)
        or _REVISION_PATTERN.fullmatch(revision) is None
    ):
        raise ValueError("BUILD_CONTEXT_REVISION_INVALID")
    matches = revision == source_tree_revision
    return {
        "status": "PASS" if matches else "BLOCKED",
        "source_revision": revision,
        "matches_source_tree": matches,
        "reason": (
            "BUILD_CONTEXT_REVISION_MATCH"
            if matches
            else "BUILD_CONTEXT_REVISION_MISMATCH"
        ),
    }


def _profile_identity(path: Path | None) -> dict[str, object]:
    """离线装配显式 Profile，仅计算组合身份。"""
    if path is None:
        return {
            "status": "NOT_RUN",
            "profile_id": None,
            "index_fingerprint": None,
            "serving_fingerprint": None,
            "reason": "PROFILE_NOT_PROVIDED",
        }
    profile = load_profile(path)
    registry = ComponentRegistry()
    register_builtin_components(registry)
    with build_components(profile, registry) as components:
        return {
            "status": "PASS",
            "profile_id": profile.profile_id,
            "index_fingerprint": components.index_fingerprint,
            "serving_fingerprint": components.serving_fingerprint,
            "network_calls": 0,
        }


def _runtime_field(payload: Mapping[str, object], key: str) -> object:
    """从状态根或其 `identity` 对象读取一个已知字段。"""
    if key in payload:
        return payload[key]
    identity = payload.get("identity")
    if isinstance(identity, Mapping):
        return identity.get(key)
    return None


def _runtime_identity(
    path: Path | None, source_tree_revision: str
) -> tuple[dict[str, object], dict[str, object]]:
    """核对显式导出的 Runtime 状态，不连接正在运行的服务。"""
    if path is None:
        return (
            {
                "status": "NOT_RUN",
                "build_revision": None,
                "image_id": None,
                "profile_id": None,
                "index_fingerprint": None,
                "serving_fingerprint": None,
                "runtime_identity": None,
                "reason": "RUNTIME_STATUS_NOT_PROVIDED",
            },
            {
                "status": "NOT_RUN",
                "active_revision_id": None,
                "reason": "RUNTIME_STATUS_NOT_PROVIDED",
            },
        )
    payload = _mapping(_load_identity_json(path), "RUNTIME_STATUS_INVALID")
    build_revision_value = _runtime_field(payload, "build_revision")
    if build_revision_value is None:
        build_revision_value = _runtime_field(payload, "source_revision")
    build_revision: str | None = None
    build_matches: bool | None = None
    build_status = "NOT_APPLICABLE"
    if build_revision_value is not None:
        if not isinstance(build_revision_value, str):
            raise ValueError("RUNTIME_BUILD_REVISION_INVALID")
        if build_revision_value == "development-unset":
            build_revision = build_revision_value
        elif _REVISION_PATTERN.fullmatch(build_revision_value) is not None:
            build_revision = build_revision_value
            build_matches = build_revision == source_tree_revision
            build_status = "PASS" if build_matches else "BLOCKED"
        else:
            raise ValueError("RUNTIME_BUILD_REVISION_INVALID")
    profile_value = _runtime_field(payload, "profile_id")
    index_value = _runtime_field(payload, "index_fingerprint")
    serving_value = _runtime_field(payload, "serving_fingerprint")
    profile_fields = (profile_value, index_value, serving_value)
    if any(value is not None for value in profile_fields) and not all(
        value is not None for value in profile_fields
    ):
        raise ValueError("RUNTIME_PROFILE_IDENTITY_INCOMPLETE")
    profile_id = (
        None
        if profile_value is None
        else _safe_identity(profile_value, "RUNTIME_PROFILE_ID_INVALID")
    )
    index_fingerprint = None if index_value is None else str(index_value)
    serving_fingerprint = None if serving_value is None else str(serving_value)
    if index_fingerprint is not None and (
        _SHA256_ID_PATTERN.fullmatch(index_fingerprint) is None
        or _SHA256_ID_PATTERN.fullmatch(serving_fingerprint or "") is None
    ):
        raise ValueError("RUNTIME_FINGERPRINT_INVALID")
    runtime_value = _runtime_field(payload, "runtime_identity")
    runtime_name = (
        None
        if runtime_value is None
        else _safe_identity(runtime_value, "RUNTIME_IDENTITY_INVALID")
    )
    image_value = _runtime_field(payload, "image_id")
    image_id = None if image_value is None else str(image_value)
    if image_id is not None and _SHA256_ID_PATTERN.fullmatch(image_id) is None:
        raise ValueError("RUNTIME_IMAGE_ID_INVALID")
    active_value = _runtime_field(payload, "active_revision_id")
    if active_value is None:
        active_value = _runtime_field(payload, "index_revision_id")
    active_revision_id = (
        None
        if active_value is None
        else _safe_identity(active_value, "ACTIVE_REVISION_ID_INVALID")
    )
    recognized = any(
        value is not None
        for value in (
            build_revision_value,
            profile_value,
            runtime_value,
            image_value,
            active_value,
        )
    )
    if not recognized:
        raise ValueError("RUNTIME_IDENTITY_FIELDS_MISSING")
    runtime_status = "BLOCKED" if build_status == "BLOCKED" else "PASS"
    runtime: dict[str, object] = {
        "status": runtime_status,
        "build_revision": build_revision,
        "build_revision_status": build_status,
        "build_revision_matches_source_tree": build_matches,
        "image_id": image_id,
        "profile_id": profile_id,
        "index_fingerprint": index_fingerprint,
        "serving_fingerprint": serving_fingerprint,
        "runtime_identity": runtime_name,
        "reason": (
            "RUNTIME_IDENTITY_VERIFIED"
            if runtime_status == "PASS"
            else "RUNTIME_BUILD_REVISION_MISMATCH"
        ),
    }
    active_revision: dict[str, object] = {
        "status": "PASS" if active_revision_id is not None else "NOT_RUN",
        "active_revision_id": active_revision_id,
        "reason": (
            "ACTIVE_REVISION_REPORTED"
            if active_revision_id is not None
            else "ACTIVE_REVISION_UNAVAILABLE"
        ),
    }
    return runtime, active_revision


def _image_identity(
    path: Path | None,
    source_tree_revision: str,
    runtime_image_id: object,
) -> dict[str, object]:
    """核对显式 Docker inspect 中的 image ID 与 OCI revision。"""
    if path is None:
        return {
            "status": "NOT_RUN",
            "image_id": None,
            "oci_revision": None,
            "reason": "IMAGE_INSPECT_NOT_PROVIDED",
        }
    raw = _load_identity_json(path)
    if isinstance(raw, Mapping):
        inspect = raw
    elif len(raw) == 1:
        inspect = _mapping(raw[0], "IMAGE_INSPECT_INVALID")
    else:
        raise ValueError("IMAGE_INSPECT_INVALID")
    image_id = inspect.get("Id")
    if (
        not isinstance(image_id, str)
        or _SHA256_ID_PATTERN.fullmatch(image_id) is None
    ):
        raise ValueError("IMAGE_ID_INVALID")
    config = _mapping(inspect.get("Config"), "IMAGE_CONFIG_INVALID")
    labels = _mapping(config.get("Labels"), "IMAGE_LABELS_INVALID")
    revision = labels.get("org.opencontainers.image.revision")
    if (
        not isinstance(revision, str)
        or _REVISION_PATTERN.fullmatch(revision) is None
    ):
        raise ValueError("IMAGE_OCI_REVISION_INVALID")
    revision_matches = revision == source_tree_revision
    expected_image_id = (
        runtime_image_id if isinstance(runtime_image_id, str) else None
    )
    image_matches = (
        None if expected_image_id is None else image_id == expected_image_id
    )
    passed = revision_matches and image_matches is not False
    return {
        "status": "PASS" if passed else "BLOCKED",
        "image_id": image_id,
        "oci_revision": revision,
        "revision_matches_source_tree": revision_matches,
        "runtime_image_id_status": (
            "NOT_APPLICABLE"
            if image_matches is None
            else "PASS"
            if image_matches
            else "BLOCKED"
        ),
        "matches_runtime_image_id": image_matches,
        "reason": (
            "IMAGE_IDENTITY_MATCH" if passed else "IMAGE_IDENTITY_MISMATCH"
        ),
    }


def _blocked_identity(reason: str) -> dict[str, object]:
    """构造不包含异常文本或路径的稳定阻塞结果。"""
    return {"status": "BLOCKED", "reason": reason}


def _run_runtime_identity(arguments: argparse.Namespace) -> int:
    """输出源码树、构建、前端与可选 Runtime 的统一身份 JSON。"""
    checks: dict[str, dict[str, object]] = {}
    source_tree_revision: str | None = None
    try:
        checks["source_tree"] = _source_tree_identity()
        source_tree_revision = str(
            checks["source_tree"]["source_tree_revision"]
        )
    except (OSError, UnicodeError, RuntimeError, ValueError):
        checks["source_tree"] = _blocked_identity("SOURCE_TREE_IDENTITY_FAILED")
    if source_tree_revision is None:
        checks["build_revision"] = _blocked_identity("SOURCE_TREE_UNAVAILABLE")
        checks["build_context"] = _blocked_identity("SOURCE_TREE_UNAVAILABLE")
    else:
        checks["build_revision"] = _build_revision_identity(
            source_tree_revision
        )
        try:
            checks["build_context"] = _build_context_identity(
                arguments.build_context_manifest,
                source_tree_revision,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            checks["build_context"] = _blocked_identity(
                "BUILD_CONTEXT_IDENTITY_FAILED"
            )
    try:
        checks["api_schema"] = _api_schema_identity()
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RuntimeError,
        ValueError,
    ):
        checks["api_schema"] = _blocked_identity("API_SCHEMA_IDENTITY_FAILED")
    try:
        checks["frontend"] = _frontend_identity()
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RuntimeError,
        ValueError,
    ):
        checks["frontend"] = _blocked_identity("FRONTEND_IDENTITY_FAILED")
    try:
        checks["profile"] = _profile_identity(arguments.profile)
    except Exception:  # CLI 边界只输出稳定错误码，避免泄漏配置内容。
        checks["profile"] = _blocked_identity("PROFILE_IDENTITY_FAILED")
    if source_tree_revision is None:
        checks["runtime"] = _blocked_identity("SOURCE_TREE_UNAVAILABLE")
        checks["active_revision"] = _blocked_identity("SOURCE_TREE_UNAVAILABLE")
        checks["image"] = _blocked_identity("SOURCE_TREE_UNAVAILABLE")
    else:
        try:
            runtime, active_revision = _runtime_identity(
                arguments.runtime_status_json,
                source_tree_revision,
            )
            checks["runtime"] = runtime
            checks["active_revision"] = active_revision
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            checks["runtime"] = _blocked_identity("RUNTIME_IDENTITY_FAILED")
            checks["active_revision"] = _blocked_identity(
                "RUNTIME_IDENTITY_FAILED"
            )
        try:
            checks["image"] = _image_identity(
                arguments.image_inspect_json,
                source_tree_revision,
                checks["runtime"].get("image_id"),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            checks["image"] = _blocked_identity("IMAGE_IDENTITY_FAILED")
    blocked = any(check.get("status") == "BLOCKED" for check in checks.values())
    report = {
        "gate": _IDENTITY_GATE,
        "schema_version": 1,
        "mode": "source-tree",
        "overall_status": "BLOCKED" if blocked else "PASS",
        "overall_reason": (
            "REQUIRED_IDENTITY_CHECK_BLOCKED"
            if blocked
            else "AVAILABLE_IDENTITY_CHECKS_PASSED"
        ),
        "status_semantics": {
            "PASS": "check completed and matched",
            "BLOCKED": "check failed or identity mismatched",
            "NOT_RUN": "optional evidence was not provided",
            "NOT_APPLICABLE": "identity does not exist in source-tree mode",
        },
        "checks": checks,
    }
    print(
        json.dumps(
            report,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 1 if blocked else 0


def _product_check_commands() -> tuple[tuple[str, ...], ...]:
    """返回产品静态审计与离线测试命令。"""
    return (
        (sys.executable, "scripts/product_hardcode_audit.py"),
        (sys.executable, "-m", "pytest", "-q", *_PRODUCT_TESTS),
    )


def _product_smoke_commands() -> tuple[tuple[str, ...], ...]:
    """返回 CLI、会话、Provider 和 Profile 的最小产品冒烟。"""
    return (
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/cli/test_product_serve.py",
            "tests/api/test_console_session.py",
            "tests/api/test_retrieval_profiles.py",
        ),
    )


def _inspect_document(
    path: Path,
    *,
    profile_path: Path | None,
    output_json: Path | None,
    include_content: bool,
) -> int:
    """离线解析一个受控本地文档并输出非敏感摘要。

    Args:
        path: 用户显式指定的本地 DOCX。
        profile_path: 可选严格 Profile；缺失时使用离线 Profile。
        output_json: 可选且必须显式指定的 IR JSON 输出路径。
        include_content: 是否在显式 JSON 输出或标准输出中包含正文。

    Returns:
        解析和可选写出成功时返回 0。

    Raises:
        FileNotFoundError: 输入不是现有普通文件。

    """
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(
            "inspect-document 输入必须是现有非 symlink 文件。"
        )
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    profile = (
        load_profile(profile_path)
        if profile_path is not None
        else default_offline_profile()
    )
    registry = ComponentRegistry()
    register_builtin_components(registry)
    context = ParseContext(
        document=DocumentRef(
            project_id=deterministic_id("prj", "inspect-document"),
            knowledge_base_id=deterministic_id("kb", "inspect-document"),
            document_id=deterministic_id("doc", digest),
            display_name=path.name,
        )
    )
    with build_components(profile, registry) as components:
        result = components.parser.parse(
            ParseSource(
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                display_name=path.name,
                extension=path.suffix or ".docx",
                content=content,
            ),
            components.parsing_policy,
            context,
        )
        report = result.report
        print(f"document_hash_prefix={digest[:12]}")
        print(
            f"parser={report.parser_id}@{report.parser_version} "
            f"nodes={report.node_count} issues={len(report.issues)}"
        )
        print(
            f"stories={dict(report.story_counts)} "
            f"coverage={report.coverage:.6f} "
            f"elapsed_seconds={report.elapsed_seconds:.6f}"
        )
        rendered = canonical_document_ir_json(
            result.document_ir,
            include_content=include_content,
        )
        if output_json is not None:
            output_json.parent.mkdir(parents=True, exist_ok=True)
            output_json.write_text(f"{rendered}\n", encoding="utf-8")
            print(f"output_json={output_json}")
        elif include_content:
            print(rendered)
    return 0


def _provider_list() -> int:
    print(json.dumps(load_provider_catalog(), ensure_ascii=False, indent=2))
    return 0


def _provider_check(profile_path: Path) -> int:
    profile = load_profile(profile_path)
    registry = ComponentRegistry()
    register_builtin_components(registry)
    with build_components(profile, registry) as components:
        print(f"OK profile_id={profile.profile_id}")
        print(f"OK index_fingerprint={components.index_fingerprint}")
        print(f"OK serving_fingerprint={components.serving_fingerprint}")
        print("OK network_calls=0")
    return 0


def _provider_smoke(provider_id: str) -> int:
    if os.environ.get("RAG_ALLOW_EXTERNAL_API") != "true":
        print(
            "FAIL external API smoke requires RAG_ALLOW_EXTERNAL_API=true",
            file=sys.stderr,
        )
        return 2
    adapter: JinaV5TextEmbeddingAdapter | AliyunQwen37EmbeddingAdapter
    if provider_id == "jina":
        adapter = JinaV5TextEmbeddingAdapter(
            JinaEmbeddingConfig(
                slot_id="primary",
                request_policy_identity="live-smoke-v1",
                query_egress_allowed=True,
            )
        )
        slot_id = "primary"
    else:
        adapter = AliyunQwen37EmbeddingAdapter(
            AliyunQwen37EmbeddingConfig(
                slot_id="standby",
                request_policy_identity="live-smoke-v1",
                query_egress_allowed=True,
            )
        )
        slot_id = "standby"
    try:
        result = adapter.embed(
            EmbeddingRequest(
                slot_id=slot_id,
                role=EmbeddingRequestRole.QUERY,
                texts=(
                    "Public synthetic health check for enterprise retrieval.",
                ),
            )
        )
        print(
            "OK "
            f"provider={provider_id} dimension={result.observed_dimension} "
            f"calls={len(result.calls)}"
        )
        return 0
    finally:
        adapter.close()


class _FailoverSmokeProvider:
    """只供 CLI 注入 smoke 使用的无网络 Provider。"""

    def __init__(
        self,
        slot: EmbeddingSlotIdentity,
        failure: Exception | None = None,
    ) -> None:
        """保存 slot 和可选脚本化失败。

        Args:
            slot: Provider 对应向量空间。
            failure: embed 时抛出的预期错误。

        Returns:
            无返回值。

        """
        self._slot = slot
        self._failure = failure
        self.descriptor = ComponentDescriptor(
            kind=ComponentKind.EMBEDDING,
            name=slot.provider_id,
            version=slot.model,
            mode=ProviderMode.DETERMINISTIC,
            capabilities=ComponentCapabilities(
                supports_batch=True,
                dimensions=(slot.dimension,),
                roles=("query",),
            ),
        )

    @property
    def capabilities(self) -> ComponentCapabilities:
        """返回脚本化 Provider 能力。

        Args:
            无参数；读取当前实例。

        Returns:
            固定维度能力。

        """
        return self.descriptor.capabilities

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """返回固定非零向量或抛出脚本化失败。

        Args:
            request: Core Embedding 请求。

        Returns:
            绑定当前 slot 的固定向量。

        """
        if self._failure is not None:
            raise self._failure
        vector = (1.0,) + (0.0,) * (self._slot.dimension - 1)
        return EmbeddingResult(
            slot_id=request.slot_id,
            role=request.role,
            vectors=(vector,),
            observed_dimension=self._slot.dimension,
            request_policy_identity=canonical_sha256(
                self._slot.query_request_policy
            ),
        )

    def health(self, *, network: bool = False) -> ProviderHealth:
        """返回无需网络的健康状态。

        Args:
            network: 被忽略；脚本化实现不联网。

        Returns:
            HEALTHY 状态。

        """
        del network
        return ProviderHealth(
            status=ProviderHealthStatus.HEALTHY,
            reason_code="SMOKE_READY",
        )


def _failover_smoke(scenario: str) -> int:
    profile = default_hot_standby_profile()
    configured = profile.components.embedding_topology
    if isinstance(configured, str):
        raise RuntimeError("hot-standby smoke profile topology 无效。")
    topology = configured.to_core()
    primary_slot = topology.slot(topology.primary_slot_id)
    standby_slot_id = topology.standby_slot_id
    if standby_slot_id is None:
        raise RuntimeError("hot-standby smoke profile 缺少 standby。")
    standby_slot = topology.slot(standby_slot_id)
    primary_failure: Exception | None
    if scenario == "jina-timeout":
        primary_failure = ProviderUnavailable(
            "injected timeout", stage="smoke.primary"
        )
    elif scenario == "jina-429":
        primary_failure = ProviderRateLimited(
            "injected rate limit", stage="smoke.primary"
        )
    elif scenario == "jina-bad-dimension":
        primary_failure = ProviderInvalidResponse(
            "injected bad dimension", stage="smoke.primary"
        )
    else:
        primary_failure = ProviderUnavailable(
            "injected primary unavailable", stage="smoke.primary"
        )
    standby_failure = (
        ProviderUnavailable(
            "injected standby unavailable",
            stage="smoke.standby",
        )
        if scenario == "both-unavailable"
        else None
    )
    router = EmbeddingFailoverRouter(
        _FailoverSmokeProvider(primary_slot, primary_failure),
        _FailoverSmokeProvider(standby_slot, standby_failure),
    )
    revision = ActiveRevisionEmbeddingState(
        topology=topology,
        coverages=tuple(
            EmbeddingCoverage(
                slot_id=slot.slot_id,
                vector_name=slot.vector_name,
                vector_count=1,
                chunk_count=1,
                observed_dimension=slot.dimension,
            )
            for slot in topology.slots
        ),
    )
    egress = EgressPolicy(
        remote_query_embedding=True,
        remote_query_embedding_jina=True,
        remote_query_embedding_aliyun=True,
        allow_aliyun_embedding_failover=True,
        aliyun_daily_request_budget=10,
        aliyun_daily_token_budget=10000,
    )
    try:
        result = router.embed_query_with_failover(
            QueryEmbeddingRequest("public synthetic query"),
            revision,
            egress,
        )
    except DenseUnavailable:
        if scenario != "both-unavailable":
            raise
        print("OK scenario=both-unavailable result=DENSE_UNAVAILABLE")
        return 0
    print(
        f"OK scenario={scenario} selected_slot={result.selected_slot_id} "
        f"vector_name={result.vector_name}"
    )
    return 0


def main(  # noqa: PLR0911, PLR0912
    arguments: Sequence[str] | None = None,
) -> int:
    """运行统一开发入口。

    Args:
        arguments: 可选命令行参数；默认读取当前进程参数。

    Returns:
        全部检查通过时返回 0，否则返回首个失败命令的原始返回码。

    """
    raw_arguments = (
        tuple(arguments) if arguments is not None else tuple(sys.argv[1:])
    )
    if raw_arguments and raw_arguments[0] in P06_COMMANDS:
        return p06_command(raw_arguments)
    if raw_arguments and raw_arguments[0] in P08_COMMANDS:
        return p08_command(raw_arguments)
    if raw_arguments and raw_arguments[0] in P09_COMMANDS:
        return p09_command(raw_arguments)
    parsed = _arguments(arguments)
    command = parsed.command
    if command == "doctor":
        return _run_doctor()
    if command == "check":
        return _run_commands(_check_commands())
    if command == "smoke":
        return _run_commands(_smoke_commands())
    if command == "product-check":
        return _run_commands(_product_check_commands())
    if command == "product-smoke":
        return _run_commands(_product_smoke_commands())
    if command == "runtime-identity":
        return _run_runtime_identity(parsed)
    if command == "web-install-check":
        return _web_install_check()
    if command.startswith("web-"):
        script = command.removeprefix("web-")
        if script == "e2e":
            return _web_e2e(parsed.profile)
        return _run_web_script(script)
    if command == "provider-list":
        return _provider_list()
    if command == "inspect-document":
        return _inspect_document(
            parsed.document_path,
            profile_path=parsed.profile,
            output_json=parsed.output_json,
            include_content=parsed.include_content,
        )
    if command == "chunk-document":
        return chunk_document_command(
            parsed.document_path,
            profile_path=parsed.profile,
            include_content=parsed.include_content,
        )
    if command == "chunk-ablation":
        return chunk_ablation_command(
            parsed.document_path,
            output_directory=parsed.output,
            profile_path=parsed.profile,
        )
    if command == "provider-check":
        return _provider_check(parsed.profile)
    if command == "provider-smoke":
        return _provider_smoke(parsed.provider)
    return _failover_smoke(parsed.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
