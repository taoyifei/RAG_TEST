"""提供跨平台、默认离线且返回码透明的统一开发入口。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

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
            "inspect-document",
            "chunk-document",
            "chunk-ablation",
        ),
    )
    parser.add_argument("document_path", nargs="?", type=Path)
    parser.add_argument("--profile", type=Path)
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


def main(arguments: Sequence[str] | None = None) -> int:  # noqa: PLR0911
    """运行统一开发入口。

    Args:
        arguments: 可选命令行参数；默认读取当前进程参数。

    Returns:
        全部检查通过时返回 0，否则返回首个失败命令的原始返回码。

    """
    parsed = _arguments(arguments)
    command = parsed.command
    if command == "doctor":
        return _run_doctor()
    if command == "check":
        return _run_commands(_check_commands())
    if command == "smoke":
        return _run_commands(_smoke_commands())
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
