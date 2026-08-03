"""定义检索配置冻结决策及其不可变证据绑定。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_app.contracts import PipelineSpec
from rag_app.strict_json import load_json_file

__all__ = [
    "FreezeCandidate",
    "FreezeCandidateConfig",
    "FreezeDecision",
    "FreezeEvidence",
    "FreezeModelRevisions",
    "FreezeThresholds",
    "ModelContractReportDigest",
    "ModelFleetIdentity",
    "RetrievalModelEndpoints",
    "build_candidate_pipeline",
    "canonical_sha256",
    "canonical_tuning_digest",
    "verify_model_fleet",
]

_MODEL_REPORT_NAMES = (
    "model-contract-embedding.json",
    "model-contract-reranker.json",
    "model-contract-llm-1.json",
    "model-contract-llm-2.json",
    "model-contract-llm-3.json",
    "model-contract-llm-4.json",
)
_RECALL_AT_20_REQUIRED = 0.95
_RERANK_RECALL_AT_5_REQUIRED = 0.90
_FLEET_REPORT_NAME = "FLEET_REPORT.json"
_LLM_REPORT_COUNT = 4
_REPORT_DIRECTORY_ENTRY_COUNT = 7
_EXPECTED_SERVICES = (
    "embedding",
    "reranker",
    "llm",
    "llm",
    "llm",
    "llm",
)


class ModelContractReportDigest(BaseModel):
    """单份脱敏模型契约报告的文件身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class FreezeEvidence(BaseModel):
    """冻结决策直接绑定的全部输入证据摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pipeline_file_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    retrieval_file_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    calibration_corpus_manifest_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    final_corpus_manifest_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    final_evaluation_manifest_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    final_corpus_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    final_corpus_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tuning_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    calibration_structural_report_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    calibration_retrieval_report_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    model_contract_reports: tuple[ModelContractReportDigest, ...] = Field(
        min_length=6,
        max_length=6,
    )
    model_contract_summary_sha256: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_model_report_set(self) -> Self:
        """拒绝缺失、重复或来自其他尝试的报告名称集合。"""
        names = tuple(report.name for report in self.model_contract_reports)
        if names != _MODEL_REPORT_NAMES:
            raise ValueError("模型契约证据必须是固定顺序的同次六份报告。")
        return self


class FreezeModelRevisions(BaseModel):
    """不含 endpoint 的源码和模型 revision 绑定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    calibration_source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    embedding_revision: str = Field(min_length=1, max_length=200)
    reranker_revision: str = Field(min_length=1, max_length=200)
    llm_revisions: tuple[tuple[str, str], ...] = Field(
        min_length=4,
        max_length=4,
    )

    @model_validator(mode="after")
    def _validate_llm_labels(self) -> Self:
        """拒绝 endpoint 标签、重复标签和临时 revision。"""
        expected_labels = ("llm-1", "llm-2", "llm-3", "llm-4")
        if tuple(label for label, _ in self.llm_revisions) != expected_labels:
            raise ValueError("LLM revision 必须使用固定的非 endpoint 标签。")
        revisions = (
            self.embedding_revision,
            self.reranker_revision,
            *(revision for _, revision in self.llm_revisions),
        )
        if any(
            marker in revision.casefold()
            for revision in revisions
            for marker in ("provisional", "pending", "unknown", "latest")
        ):
            raise ValueError("冻结模型 revision 仍含临时值。")
        return self


class ModelFleetIdentity(BaseModel):
    """不含 endpoint 的一次模型 fleet 验收身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    calibration_source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    summary_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reports: tuple[ModelContractReportDigest, ...] = Field(
        min_length=6,
        max_length=6,
    )
    model_revisions: FreezeModelRevisions

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        """拒绝报告集合或源码 revision 的内部漂移。"""
        names = tuple(report.name for report in self.reports)
        if names != _MODEL_REPORT_NAMES:
            raise ValueError("模型 fleet 必须绑定固定顺序的六份报告。")
        if (
            self.model_revisions.calibration_source_revision
            != self.calibration_source_revision
        ):
            raise ValueError("模型 fleet 的源码 revision 内部不一致。")
        return self


@dataclass(frozen=True, slots=True)
class RetrievalModelEndpoints:
    """本次真实检索实际使用的两个模型 origin。"""

    embedding: tuple[str, ...]
    reranker: tuple[str, ...]


class _ModelContractReport(BaseModel):
    """模型契约验证器产生的一份严格 passed 报告。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"]
    status: Literal["passed"]
    service: Literal["embedding", "reranker", "llm"]
    endpoint: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=512)
    endpoint_revision: str = Field(min_length=1, max_length=200)
    revision_source: Literal["endpoint", "deployment_manifest"]
    health: Literal["passed"]
    model_id: Literal["passed"]
    probe: dict[str, object]
    deployment_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        """拒绝临时 revision、危险 endpoint 或无效探针。"""
        _normalize_endpoint(self.endpoint)
        lowered = self.endpoint_revision.casefold()
        if any(
            marker in lowered
            for marker in ("provisional", "pending", "unknown", "latest")
        ):
            raise ValueError("模型契约 revision 仍是临时值。")
        if (
            self.revision_source == "deployment_manifest"
            and self.deployment_manifest_sha256 is None
        ):
            raise ValueError("deployment manifest revision 缺少摘要。")
        if (
            self.revision_source == "endpoint"
            and self.deployment_manifest_sha256 is not None
        ):
            raise ValueError(
                "endpoint revision 不应附带 deployment manifest 摘要。"
            )
        _validate_probe(self.service, self.probe)
        return self


class _FleetReportItem(BaseModel):
    """fleet summary 中一份模型报告的文件摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    service: Literal["embedding", "reranker", "llm"]
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class _FleetReport(BaseModel):
    """同次 1+1+4 模型验收的严格汇总。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"]
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: Literal["passed"]
    reports: tuple[_FleetReportItem, ...] = Field(
        min_length=6,
        max_length=6,
    )

    @model_validator(mode="after")
    def _validate_report_set(self) -> Self:
        """拒绝未排序、重复或角色数量不正确的汇总。"""
        names = tuple(report.name for report in self.reports)
        if names != tuple(sorted(_MODEL_REPORT_NAMES)):
            raise ValueError("fleet summary 必须按 name 排序绑定六份报告。")
        services = tuple(report.service for report in self.reports)
        if (
            services.count("embedding") != 1
            or services.count("reranker") != 1
            or services.count("llm") != _LLM_REPORT_COUNT
        ):
            raise ValueError("fleet summary 必须严格包含 1+1+4 服务。")
        return self


class FreezeCandidateConfig(BaseModel):
    """入选 section-pack 候选的三个冻结整数参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_tokens: int = Field(gt=0)
    hard_max_tokens: int = Field(gt=0)
    overlap_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        """拒绝目标上限倒置或无效 overlap。"""
        if (
            self.target_tokens > self.hard_max_tokens
            or self.overlap_tokens >= self.target_tokens
        ):
            raise ValueError("chunk 候选参数的目标、硬上限或 overlap 无效。")
        return self


class FreezeCandidate(BaseModel):
    """冻结决策记录的唯一入选候选。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=128)
    strategy: Literal["section_pack_v2"]
    chunker_revision: str = Field(min_length=1, max_length=200)
    config: FreezeCandidateConfig

    @model_validator(mode="after")
    def _validate_revision(self) -> Self:
        """拒绝把临时 chunker revision 写入冻结决策。"""
        lowered = self.chunker_revision.casefold()
        if any(
            marker in lowered
            for marker in ("provisional", "pending", "unknown")
        ):
            raise ValueError("冻结候选的 chunker revision 仍是临时值。")
        return self


class FreezeThresholds(BaseModel):
    """检索冻结所需的固定阈值和实测通过结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recall_at_20: float = Field(ge=0.0, le=1.0)
    recall_at_20_required: float = _RECALL_AT_20_REQUIRED
    recall_at_20_passed: bool
    rerank_recall_at_5: float = Field(ge=0.0, le=1.0)
    rerank_recall_at_5_required: float = _RERANK_RECALL_AT_5_REQUIRED
    rerank_recall_at_5_passed: bool

    @model_validator(mode="after")
    def _validate_results(self) -> Self:
        """拒绝可调低阈值或与实测值矛盾的通过标记。"""
        if (
            self.recall_at_20_required != _RECALL_AT_20_REQUIRED
            or self.rerank_recall_at_5_required
            != _RERANK_RECALL_AT_5_REQUIRED
        ):
            raise ValueError("冻结阈值必须保持 0.95 和 0.90。")
        expected_recall = self.recall_at_20 >= self.recall_at_20_required
        expected_rerank = (
            self.rerank_recall_at_5 >= self.rerank_recall_at_5_required
        )
        if (
            not self.recall_at_20_passed
            or not self.rerank_recall_at_5_passed
            or self.recall_at_20_passed != expected_recall
            or self.rerank_recall_at_5_passed != expected_rerank
        ):
            raise ValueError("冻结指标未达到固定检索阈值。")
        return self


class FreezeDecision(BaseModel):
    """将配置指纹、候选、阈值和证据摘要绑定为冻结凭据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    attempt_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    index_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    serving_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_revisions: FreezeModelRevisions
    selected_candidate: FreezeCandidate
    thresholds: FreezeThresholds
    evidence: FreezeEvidence

    @classmethod
    def load(cls, path: Path) -> FreezeDecision:
        """从严格 JSON 文件加载冻结决策。

        Args:
            path: `FREEZE_DECISION.json` 文件路径。

        Returns:
            已完成严格 schema 校验的冻结决策。

        """
        return cls.model_validate(load_json_file(path, label="freeze decision"))

    def sha256(self) -> str:
        """计算冻结决策的规范化 SHA256。

        Args:
            无参数。

        Returns:
            带算法前缀的规范化决策摘要。

        """
        return canonical_sha256(self.model_dump(mode="json"))


def canonical_sha256(value: object) -> str:
    """计算 JSON 兼容值的规范化 SHA256。

    Args:
        value: 不含非 JSON 类型的待摘要值。

    Returns:
        带 `sha256:` 前缀的规范化摘要。

    Raises:
        TypeError: 输入含不可序列化对象。

    """
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def canonical_tuning_digest(
    documents: Mapping[str, str],
    cases: Sequence[Mapping[str, object]],
) -> str:
    """计算只覆盖文档映射和 tuning 题目的稳定摘要。

    Holdout 标签可在 OCR 人工复核后补齐，但不得改变调参问题或文档映射。
    因此该摘要有意忽略全部 holdout 字段以及数据集展示性元数据。

    Args:
        documents: 数据集中的稳定文档键到相对路径映射。
        cases: 已完成严格数据集校验的全部题目 JSON 记录。

    Returns:
        带算法前缀的规范化 tuning 摘要。

    Raises:
        ValueError: 文档映射、split、题号或 tuning 集合无效。

    """
    if not documents or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or not value
        for key, value in documents.items()
    ):
        raise ValueError("tuning digest 的文档映射无效。")
    tuning_cases: list[Mapping[str, object]] = []
    for case in cases:
        split = case.get("split")
        identifier = case.get("id")
        if split not in {"tuning", "holdout"} or not isinstance(
            identifier, str
        ):
            raise ValueError("tuning digest 的题目 split 或 ID 无效。")
        if split == "tuning":
            tuning_cases.append(case)
    identifiers = tuple(str(case["id"]) for case in tuning_cases)
    if not tuning_cases or len(set(identifiers)) != len(identifiers):
        raise ValueError("tuning digest 的调参题为空或含重复 ID。")
    serialized_cases = [
        cast(dict[str, object], dict(case)) for case in tuning_cases
    ]
    serialized_cases.sort(key=_case_identifier)
    return canonical_sha256(
        {
            "documents": dict(documents),
            "tuning_cases": serialized_cases,
        }
    )


def _case_identifier(case: dict[str, object]) -> str:
    """返回已校验 tuning 题的排序 ID。

    Args:
        case: 已通过数据集 schema 校验的 tuning 题。

    Returns:
        可用于确定性排序的题号字符串。

    """
    return str(case["id"])


def build_candidate_pipeline(
    base_pipeline: PipelineSpec,
    config: FreezeCandidateConfig,
    *,
    strategy: str,
    fleet: ModelFleetIdentity,
) -> PipelineSpec:
    """用已验收模型 revision 构造本次候选的精确 pipeline。

    Args:
        base_pipeline: calibration 使用的 operator pipeline 文件。
        config: 当前候选的三个分块参数。
        strategy: 当前候选的分块策略标识。
        fleet: 已验证且不含 endpoint 的同次模型身份。

    Returns:
        可同时计算候选索引和服务指纹的严格 PipelineSpec。

    Raises:
        ValueError: 策略标识为空或含临时语义。

    """
    normalized_strategy = strategy.strip()
    if not normalized_strategy or any(
        marker in normalized_strategy.casefold()
        for marker in ("provisional", "pending", "unknown")
    ):
        raise ValueError("候选策略不能是空值或临时标识。")
    revisions = fleet.model_revisions
    payload = base_pipeline.model_dump(mode="json")
    payload.update(
        {
            "chunker_revision": (
                f"{normalized_strategy}@"
                f"{fleet.calibration_source_revision}"
            ),
            "chunker_parameters": sorted(
                (
                    ("target_tokens", str(config.target_tokens)),
                    ("hard_max_tokens", str(config.hard_max_tokens)),
                    ("overlap_tokens", str(config.overlap_tokens)),
                )
            ),
            "embedding_revision": revisions.embedding_revision,
            "reranker_revision": revisions.reranker_revision,
            "llm_revisions": revisions.llm_revisions,
        }
    )
    return PipelineSpec.model_validate(payload)


def verify_model_fleet(
    fleet_report_path: Path,
    model_contract_directory: Path,
    *,
    pipeline: PipelineSpec,
    calibration_source_revision: str,
    retrieval_endpoints: RetrievalModelEndpoints | None = None,
) -> ModelFleetIdentity:
    """验证同次模型报告并返回不含 endpoint 的安全身份。

    Args:
        fleet_report_path: 必须位于报告目录内的 `FLEET_REPORT.json`。
        model_contract_directory: 恰含 summary 与六份普通报告的目录。
        pipeline: 含模型 ID、embedding 维度的严格 PipelineSpec。
        calibration_source_revision: 本次检索代码的 40 位源码 revision。
        retrieval_endpoints: 可选的本次实际 embedding/reranker origin。

    Returns:
        只含报告摘要、attempt 与安全模型 revision 的身份。

    Raises:
        ValueError: 文件集合、summary、模型、端点或探针不一致。

    """
    _require_report_directory(fleet_report_path, model_contract_directory)
    summary = _FleetReport.model_validate(
        load_json_file(fleet_report_path, label="fleet report")
    )
    if summary.source_revision != calibration_source_revision:
        raise ValueError("fleet summary 与 retrieval 源码 revision 不一致。")
    report_paths = tuple(
        model_contract_directory / name for name in _MODEL_REPORT_NAMES
    )
    reports = tuple(
        _ModelContractReport.model_validate(
            load_json_file(path, label="model contract report")
        )
        for path in report_paths
    )
    if tuple(report.service for report in reports) != _EXPECTED_SERVICES:
        raise ValueError("模型契约报告服务角色或顺序无效。")
    endpoints = tuple(
        _normalize_endpoint(report.endpoint) for report in reports
    )
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("六份模型契约报告必须来自六个唯一端点。")
    digests = tuple(
        ModelContractReportDigest(name=path.name, sha256=_sha256_file(path))
        for path in report_paths
    )
    actual_summary = {
        item.name: (item.service, item.sha256) for item in summary.reports
    }
    expected_summary = {
        path.name: (report.service, digest.sha256)
        for path, report, digest in zip(
            report_paths,
            reports,
            digests,
            strict=True,
        )
    }
    if actual_summary != expected_summary:
        raise ValueError("fleet summary 与六份模型报告摘要或角色不一致。")
    _require_pipeline_models(pipeline, reports)
    if retrieval_endpoints is not None:
        _require_exact_service_endpoint(
            "embedding",
            retrieval_endpoints.embedding,
            reports[0].endpoint,
        )
        _require_exact_service_endpoint(
            "reranker",
            retrieval_endpoints.reranker,
            reports[1].endpoint,
        )
    revisions = FreezeModelRevisions(
        calibration_source_revision=calibration_source_revision,
        embedding_revision=reports[0].endpoint_revision,
        reranker_revision=reports[1].endpoint_revision,
        llm_revisions=tuple(
            (f"llm-{index}", report.endpoint_revision)
            for index, report in enumerate(reports[2:], start=1)
        ),
    )
    return ModelFleetIdentity(
        attempt_id=summary.attempt_id,
        calibration_source_revision=calibration_source_revision,
        summary_sha256=_sha256_file(fleet_report_path),
        reports=digests,
        model_revisions=revisions,
    )


def _require_report_directory(
    fleet_report_path: Path,
    model_contract_directory: Path,
) -> None:
    if (
        not model_contract_directory.is_dir()
        or model_contract_directory.is_symlink()
    ):
        raise ValueError("模型契约 attempt 必须是真实目录。")
    if (
        fleet_report_path.name != _FLEET_REPORT_NAME
        or not fleet_report_path.is_file()
        or fleet_report_path.is_symlink()
        or fleet_report_path.parent.resolve(strict=True)
        != model_contract_directory.resolve(strict=True)
    ):
        raise ValueError("FLEET_REPORT.json 必须是报告目录内的普通文件。")
    entries = tuple(
        sorted(model_contract_directory.iterdir(), key=lambda path: path.name)
    )
    expected_names = {*_MODEL_REPORT_NAMES, _FLEET_REPORT_NAME}
    if (
        {path.name for path in entries} != expected_names
        or len(entries) != _REPORT_DIRECTORY_ENTRY_COUNT
    ):
        raise ValueError("模型契约目录必须恰含 summary 和六份报告。")
    if any(not path.is_file() or path.is_symlink() for path in entries):
        raise ValueError("模型契约目录只能包含七份普通文件。")


def _require_pipeline_models(
    pipeline: PipelineSpec,
    reports: tuple[_ModelContractReport, ...],
) -> None:
    if (
        reports[0].model != pipeline.embedding_model
        or reports[1].model != pipeline.reranker_model
        or any(report.model != pipeline.llm_model for report in reports[2:])
    ):
        raise ValueError("模型契约 model ID 与 pipeline 不一致。")
    if reports[0].probe.get("dimension") != pipeline.embedding_dimension:
        raise ValueError("embedding 报告维度与 pipeline 不一致。")


def _require_exact_service_endpoint(
    service: str,
    actual_endpoints: tuple[str, ...],
    report_endpoint: str,
) -> None:
    if len(actual_endpoints) != 1:
        raise ValueError(f"{service} 必须恰有一个已验收 endpoint。")
    if _normalize_endpoint(actual_endpoints[0]) != _normalize_endpoint(
        report_endpoint
    ):
        raise ValueError(f"{service} 报告 endpoint 与本次实际端点不一致。")


def _normalize_endpoint(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("模型契约 endpoint 不是安全 HTTP URL。")
    return normalized


def _validate_probe(service: str, probe: dict[str, object]) -> None:
    if service == "embedding":
        expected = {
            "count": 2,
            "dimension": probe.get("dimension"),
            "indexes": [0, 1],
            "finite": True,
        }
        dimension = probe.get("dimension")
        if (
            probe != expected
            or not isinstance(dimension, int)
            or isinstance(dimension, bool)
            or dimension <= 0
        ):
            raise ValueError("embedding 模型契约探针结构无效。")
        return
    if service == "reranker":
        if probe != {
            "count": 2,
            "indexes": [0, 1],
            "score_range": [0.0, 1.0],
        }:
            raise ValueError("reranker 模型契约探针结构无效。")
        return
    required = {
        "rewrite",
        "answer_initial_max",
        "answer_repair_max",
        "temperature",
        "thinking_enabled",
    }
    if (
        set(probe) != required
        or probe.get("temperature") != 0
        or probe.get("thinking_enabled") is not False
        or any(
            not isinstance(probe.get(name), dict)
            for name in required - {"temperature", "thinking_enabled"}
        )
    ):
        raise ValueError("LLM 模型契约探针结构无效。")


def _sha256_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise ValueError("证据必须是普通文件。")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"
