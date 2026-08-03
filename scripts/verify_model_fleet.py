"""一次性验证并绑定 RAG 使用的六个模型端点。"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))

from rag_app.chunking import HuggingFaceTokenCounter  # noqa: E402
from rag_app.contracts import PipelineSpec  # noqa: E402
from rag_app.runtime import load_pipeline  # noqa: E402
from rag_app.settings import RetrievalSettings  # noqa: E402
from scripts.verify_model_contracts import (  # noqa: E402
    ContractError,
    LlmBudgetOptions,
    ModelContractOptions,
    ServiceName,
    _canonical_manifest_sha256,
    _load_deployment_manifest,
    _normalize_endpoint,
    _validate_deployment_manifest_v2_schema,
    verify_model_contract,
)

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ATTEMPT_ID = re.compile(r"^[0-9a-f]{32}$")
_FLEET_REPORT_NAME = "FLEET_REPORT.json"
_REPORT_SPECS = (
    ("model-contract-embedding.json", "embedding", 0),
    ("model-contract-reranker.json", "reranker", 0),
    ("model-contract-llm-1.json", "llm", 0),
    ("model-contract-llm-2.json", "llm", 1),
    ("model-contract-llm-3.json", "llm", 2),
    ("model-contract-llm-4.json", "llm", 3),
)
_MANIFEST_NAMES = (
    "embedding.json",
    "reranker.json",
    "llm-1.json",
    "llm-2.json",
    "llm-3.json",
    "llm-4.json",
)
_ENDPOINT_ENVIRONMENTS = {
    "embedding": ("RAG_EMBEDDING_ENDPOINTS", 1),
    "reranker": ("RAG_RERANKER_ENDPOINTS", 1),
    "llm": ("RAG_LLM_ENDPOINTS", 4),
}
_MODEL_ENVIRONMENTS = {
    "embedding": "RAG_EMBEDDING_MODEL",
    "reranker": "RAG_RERANKER_MODEL",
    "llm": "RAG_LLM_MODEL",
}
_TOKEN_ENVIRONMENTS = {
    "embedding": "RAG_EMBEDDING_API_TOKEN",
    "reranker": "RAG_RERANKER_API_TOKEN",
    "llm": "RAG_LLM_API_TOKEN",
}


@dataclass(frozen=True, slots=True)
class FleetVerificationOptions:
    """模型 fleet 核验使用的文件、身份和超时参数。

    Attributes:
        pipeline_path: 本轮 provisional 或 frozen pipeline 配置。
        retrieval_path: 与 pipeline 配套的检索配置。
        llm_tokenizer_path: 生产 LLM tokenizer JSON 路径。
        deployment_manifest_directory: 恰含六份 schema v2 清单的目录。
        output_directory: 必须尚不存在的证据输出目录。
        source_revision: 当前 runtime 的 40 位源码 Git revision。
        timeout_seconds: 每个只读模型请求的超时秒数。
        attempt_id: 测试或续跑时显式提供的 32 位尝试标识。

    """

    pipeline_path: Path
    retrieval_path: Path
    llm_tokenizer_path: Path
    deployment_manifest_directory: Path
    output_directory: Path
    source_revision: str
    timeout_seconds: float
    attempt_id: str | None = None


def verify_model_fleet(
    options: FleetVerificationOptions,
    *,
    environment: Mapping[str, str],
    client: httpx.Client,
) -> dict[str, object]:
    """验证固定 1+1+4 端点并发布不可混用的同轮证据。

    Args:
        options: 文件、源码身份、输出位置和超时参数。
        environment: 提供端点、模型 ID 与可选令牌的环境变量。
        client: 调用方管理生命周期的 HTTP 客户端。

    Returns:
        不含 endpoint 和令牌的 fleet 汇总。

    Raises:
        FileExistsError: 输出已存在或发布时发生并发抢占。
        OSError: 输出目录不能安全创建或原子发布。
        ValueError: 配置、清单、端点数量或模型契约不满足要求。

    """
    if _SOURCE_REVISION.fullmatch(options.source_revision) is None:
        raise ValueError("source revision 必须是 40 位小写 Git SHA。")
    selected_attempt = options.attempt_id or secrets.token_hex(16)
    if _ATTEMPT_ID.fullmatch(selected_attempt) is None:
        raise ValueError("attempt ID 必须是 32 位小写十六进制字符串。")
    if options.timeout_seconds <= 0:
        raise ValueError("模型验证超时必须为正数。")
    parent = _safe_output_parent(options.output_directory)
    pipeline = load_pipeline(options.pipeline_path)
    retrieval = RetrievalSettings.load(options.retrieval_path)
    endpoints = _load_endpoint_fleet(environment)
    models = _load_models(environment, pipeline)
    manifests = _load_manifests(options.deployment_manifest_directory)
    _validate_manifest_bindings(
        manifests,
        endpoints=endpoints,
        pipeline=pipeline,
    )
    llm_budget = _llm_budget(
        manifests,
        retrieval=retrieval,
        tokenizer_path=options.llm_tokenizer_path,
    )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{options.output_directory.name}.",
            dir=parent,
        )
    )
    staging.chmod(stat.S_IRWXU)
    try:
        summary_items = []
        for report_spec, manifest_name in zip(
            _REPORT_SPECS,
            _MANIFEST_NAMES,
            strict=True,
        ):
            report_name, service, endpoint_index = report_spec
            manifest_path, manifest = manifests[manifest_name]
            contract_options = ModelContractOptions(
                service=cast(ServiceName, service),
                endpoint=endpoints[service][endpoint_index],
                model=models[service],
                expected_revision=cast(str, manifest["model_revision"]),
                token=environment.get(_TOKEN_ENVIRONMENTS[service]) or None,
                dimension=(
                    pipeline.embedding_dimension
                    if service == "embedding"
                    else None
                ),
                timeout_seconds=options.timeout_seconds,
                deployment_manifest=manifest_path,
                llm_budget=llm_budget if service == "llm" else None,
            )
            try:
                report = verify_model_contract(
                    contract_options,
                    client=client,
                )
            except ContractError as error:
                raise ValueError(
                    f"{service} 模型契约校验失败：{error.code}。"
                ) from error
            _validate_report_identity(
                report,
                manifest=manifest,
            )
            report_sha256 = _write_read_only_json(staging / report_name, report)
            summary_items.append(
                {
                    "name": report_name,
                    "service": service,
                    "sha256": report_sha256,
                }
            )
        summary_items.sort(key=lambda item: item["name"])
        summary: dict[str, object] = {
            "schema_version": "1",
            "attempt_id": selected_attempt,
            "source_revision": options.source_revision,
            "status": "passed",
            "reports": summary_items,
        }
        _write_read_only_json(staging / _FLEET_REPORT_NAME, summary)
        _fsync_directory(staging)
        _publish_directory_no_replace(staging, options.output_directory)
        staging = Path()
        return summary
    finally:
        if staging != Path() and staging.exists():
            shutil.rmtree(staging)


def _load_endpoint_fleet(
    environment: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    endpoints = {}
    for service, (name, expected_count) in _ENDPOINT_ENVIRONMENTS.items():
        raw = environment.get(name)
        if raw is None:
            raise ValueError(f"缺少环境变量 {name}。")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"{name} 不是有效 JSON 数组。") from error
        if (
            not isinstance(value, list)
            or len(value) != expected_count
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise ValueError(f"{name} 必须恰含 {expected_count} 个非空 URL。")
        try:
            normalized = tuple(_normalize_endpoint(item) for item in value)
        except ValueError as error:
            raise ValueError(f"{name} 包含非 origin 根 URL。") from error
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{name} 不得包含重复端点。")
        endpoints[service] = normalized
    flattened = tuple(
        endpoint
        for service_endpoints in endpoints.values()
        for endpoint in service_endpoints
    )
    if len(set(flattened)) != len(flattened):
        raise ValueError("模型 fleet 必须使用六个唯一端点。")
    return endpoints


def _load_models(
    environment: Mapping[str, str],
    pipeline: PipelineSpec,
) -> dict[str, str]:
    expected = {
        "embedding": pipeline.embedding_model,
        "reranker": pipeline.reranker_model,
        "llm": pipeline.llm_model,
    }
    models = {}
    for service, name in _MODEL_ENVIRONMENTS.items():
        value = environment.get(name)
        if value != expected[service]:
            raise ValueError(f"{name} 与 pipeline 模型 ID 不一致。")
        models[service] = value
    return models


def _load_manifests(
    directory: Path,
) -> dict[str, tuple[Path, dict[str, object]]]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("模型部署清单路径必须是真实目录。")
    entries = tuple(sorted(directory.iterdir(), key=lambda path: path.name))
    if (
        len(entries) != len(_MANIFEST_NAMES)
        or {path.name for path in entries} != set(_MANIFEST_NAMES)
    ):
        raise ValueError("模型部署清单目录必须恰含固定六份 JSON。")
    manifests = {}
    for path in entries:
        if not path.is_file() or path.is_symlink():
            raise ValueError("模型部署清单必须是普通文件。")
        try:
            manifest = _load_deployment_manifest(path)
            _validate_deployment_manifest_v2_schema(manifest)
            if (
                manifest["manifest_sha256"]
                != _canonical_manifest_sha256(manifest)
            ):
                raise ContractError("DEPLOYMENT_MANIFEST_INVALID")
        except ContractError as error:
            raise ValueError("模型部署清单未通过严格校验。") from error
        manifests[path.name] = (path, manifest)
    return manifests


def _validate_manifest_bindings(
    manifests: Mapping[str, tuple[Path, dict[str, object]]],
    *,
    endpoints: Mapping[str, tuple[str, ...]],
    pipeline: PipelineSpec,
) -> None:
    expected_models = {
        "embedding": pipeline.embedding_model,
        "reranker": pipeline.reranker_model,
        "llm": pipeline.llm_model,
    }
    expected_tokenizer_revisions = {
        "embedding": f"sha256:{pipeline.embedding_tokenizer_sha256}",
        "llm": f"sha256:{pipeline.llm_tokenizer_sha256}",
    }
    for report_spec, manifest_name in zip(
        _REPORT_SPECS,
        _MANIFEST_NAMES,
        strict=True,
    ):
        _, service, endpoint_index = report_spec
        _, manifest = manifests[manifest_name]
        if manifest.get("service") != service:
            raise ValueError(f"{manifest_name} 的 service 不匹配。")
        if manifest.get("endpoint") != endpoints[service][endpoint_index]:
            raise ValueError(f"{manifest_name} 的 endpoint 不匹配。")
        if manifest.get("model") != expected_models[service]:
            raise ValueError(f"{manifest_name} 的 model 不匹配。")
        if (
            service in expected_tokenizer_revisions
            and manifest.get("tokenizer_revision")
            != expected_tokenizer_revisions[service]
        ):
            raise ValueError(
                f"{manifest_name} 的 tokenizer revision 与 pipeline 不一致。"
            )
        contract = manifest.get("service_contract")
        if not isinstance(contract, dict):
            raise ValueError(f"{manifest_name} 缺少 service contract。")
        if (
            service == "embedding"
            and contract.get("dimension") != pipeline.embedding_dimension
        ):
            raise ValueError(
                "embedding manifest dimension 与 pipeline 不一致。"
            )
    _validate_llm_replicas(manifests)


def _validate_llm_replicas(
    manifests: Mapping[str, tuple[Path, dict[str, object]]],
) -> None:
    replica_fields = (
        "model",
        "model_revision",
        "tokenizer_revision",
        "runtime",
        "service_contract",
    )
    replicas = tuple(
        manifests[name][1] for name in _MANIFEST_NAMES[2:]
    )
    baseline = tuple(replicas[0][field] for field in replica_fields)
    if any(
        tuple(replica[field] for field in replica_fields) != baseline
        for replica in replicas[1:]
    ):
        raise ValueError("四个 LLM manifest 除 endpoint 外必须完全一致。")


def _llm_budget(
    manifests: Mapping[str, tuple[Path, dict[str, object]]],
    *,
    retrieval: RetrievalSettings,
    tokenizer_path: Path,
) -> LlmBudgetOptions:
    context_limits = set()
    for name in _MANIFEST_NAMES[2:]:
        contract = manifests[name][1].get("service_contract")
        if not isinstance(contract, dict):
            raise ValueError(f"{name} 缺少 LLM service contract。")
        context_limit = contract.get("max_context_tokens")
        if not isinstance(context_limit, int) or isinstance(
            context_limit,
            bool,
        ):
            raise ValueError(f"{name} 的 max_context_tokens 无效。")
        context_limits.add(context_limit)
    if len(context_limits) != 1:
        raise ValueError("四个 LLM 的 max_context_tokens 必须一致。")
    return LlmBudgetOptions(
        context_limit=context_limits.pop(),
        max_question_tokens=retrieval.max_question_tokens,
        max_evidence_tokens=retrieval.max_evidence_tokens,
        rewrite_output_tokens=retrieval.rewrite_output_tokens,
        answer_output_tokens=retrieval.answer_output_tokens,
        repair_output_tokens=retrieval.repair_output_tokens,
        token_counter=HuggingFaceTokenCounter(tokenizer_path),
    )


def _validate_report_identity(
    report: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
) -> None:
    service = cast(str, manifest["service"])
    expected = {
        "schema_version": "1",
        "status": "passed",
        "service": service,
        "endpoint": cast(str, manifest["endpoint"]),
        "model": cast(str, manifest["model"]),
        "endpoint_revision": cast(str, manifest["model_revision"]),
        "health": "passed",
        "model_id": "passed",
    }
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError(f"{service} 契约报告身份与输入不一致。")
    revision_source = report.get("revision_source")
    if revision_source == "deployment_manifest":
        if (
            report.get("deployment_manifest_sha256")
            != manifest["manifest_sha256"]
        ):
            raise ValueError(f"{service} 契约报告身份与清单不一致。")
    elif revision_source == "endpoint":
        if "deployment_manifest_sha256" in report:
            raise ValueError(f"{service} 契约报告身份与清单不一致。")
    else:
        raise ValueError(f"{service} 契约报告 revision 来源无效。")


def _safe_output_parent(output: Path) -> Path:
    if output.exists() or output.is_symlink():
        raise FileExistsError("模型契约输出已存在，拒绝覆盖。")
    parent = output.parent
    try:
        resolved = parent.resolve(strict=True)
    except OSError as error:
        raise ValueError("模型契约输出父目录不存在。") from error
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("模型契约输出父路径必须是真实目录。")
    if resolved != parent.absolute():
        raise ValueError("模型契约输出父路径不得经过符号链接。")
    return resolved


def _write_read_only_json(path: Path, value: object) -> str:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.S_IRUSR)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_directory_no_replace(staging: Path, output: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "系统不支持 renameat2。")
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(staging),
        _AT_FDCWD,
        os.fsencode(output),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError("模型契约输出发布时已被占用。")
        raise OSError(error_number, os.strerror(error_number), output)
    _fsync_directory(output.parent)


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pipeline", required=True, type=Path)
    parser.add_argument("--retrieval", required=True, type=Path)
    parser.add_argument("--llm-tokenizer", required=True, type=Path)
    parser.add_argument(
        "--deployment-manifest-directory",
        required=True,
        type=Path,
    )
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--attempt-id")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """运行 fleet 契约并只向标准输出发布脱敏摘要。

    Args:
        argv: 不含程序名的参数；为空时读取当前命令行。

    Returns:
        六个端点全部通过且证据原子发布时返回 0，否则返回 1。

    """
    arguments = _arguments(argv)
    try:
        with httpx.Client(
            timeout=arguments.timeout_seconds,
            trust_env=False,
        ) as client:
            report = verify_model_fleet(
                FleetVerificationOptions(
                    pipeline_path=arguments.pipeline,
                    retrieval_path=arguments.retrieval,
                    llm_tokenizer_path=arguments.llm_tokenizer,
                    deployment_manifest_directory=(
                        arguments.deployment_manifest_directory
                    ),
                    output_directory=arguments.output_directory,
                    source_revision=arguments.source_revision,
                    timeout_seconds=arguments.timeout_seconds,
                    attempt_id=arguments.attempt_id,
                ),
                environment=os.environ,
                client=client,
            )
    except (OSError, ValueError) as error:
        print(f"MODEL_FLEET_FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
