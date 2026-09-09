"""V3-02C 固定 Chunk 离线质量与 Trace 开销基线。

本模块只编排当前 Product 查询执行链，不实现第二套检索逻辑。调用方负责
提供一个 :class:`FixedChunkRuntime` 适配器；编排器只允许构建一次语料，并在
完全相同的 Chunk、Revision、Profile 和指纹上执行 SAFE、DIAGNOSTIC、FULL。

B0 是进入 V3-02 前的冻结产物。其查询链进入点绑定 ``0505fac``，但允许
使用只刷新当前 Chunk 标签的 ``5505e8b`` 执行和标注。本模块只能执行 B1，
避免在查询链变化后反向重算 B0。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluation.v2.dataset import LoadedDataset, load_dataset_directory
from evaluation.v2.fixtures import fixture_bytes
from evaluation.v2.gates import evaluate_gates, load_gate_configuration
from evaluation.v2.metrics import MetricContext, compute_metric_report
from evaluation.v2.models import CaseObservation, EvaluationCase
from evaluation.v2.observations import ObservationContext, observe_case_result
from evaluation.v2.runtime import (
    _build_corpus,
    _effective_cases,
    _offline_persistent_profile,
)
from evaluation.v2.variants import EvaluationVariant, offline_variants
from rag_app.adapters.stores import InMemoryRetrievalCache, SqliteFtsStore
from rag_app.application.retrieval import RetrievalService
from rag_app.composition.p07_runtime import P07Runtime, build_p07_runtime
from rag_app.composition.profiles import RagProfile, load_profile
from rag_app.core.identifiers import canonical_sha256
from rag_app.core.models import (
    ConfidenceStatus,
    KnowledgeBaseScope,
    RetrievalDiagnostics,
    SearchAnswerResult,
    SearchRequest,
)
from rag_app.core.models.chunk import Chunk
from rag_app.core.ports import ExactStorePort
from rag_app.tracing.models import (
    SpanKind,
    TraceIdentity,
    TraceMode,
    TraceStatus,
)
from rag_app.tracing.reasons import DecisionCode
from rag_app.tracing.recorder import (
    TraceRecorder,
    TraceRecorderConfig,
    TraceSession,
    TraceSpanSpec,
)
from rag_app.tracing.store import TraceStore

B0_SOURCE_REVISION = "0505fac076ee468916f988b35345c71027049678"
B0_EXECUTION_REVISION = "5505e8b7b348ce17eb451eee77c0a7633d1ae0b4"
B1_LABEL_REVISION = "5505e8b7b348ce17eb451eee77c0a7633d1ae0b4"

CacheCondition = Literal["cold", "warm"]
BaselineId = Literal["B1"]
PublicSlice = Literal[
    "exact",
    "enumeration",
    "multilevel_list",
    "table",
    "colloquial_synonym",
    "numeric_unit_negation_exception",
    "unanswerable",
    "ocr_only",
    "accepted_relation_consumer",
]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

TRACE_MODE_ORDER = (
    TraceMode.SAFE,
    TraceMode.DIAGNOSTIC,
    TraceMode.FULL,
)
CACHE_CONDITION_ORDER: tuple[CacheCondition, ...] = ("cold", "warm")
REQUIRED_PUBLIC_SLICES: frozenset[PublicSlice] = frozenset(
    {
        "exact",
        "enumeration",
        "multilevel_list",
        "table",
        "colloquial_synonym",
        "numeric_unit_negation_exception",
        "unanswerable",
        "ocr_only",
        "accepted_relation_consumer",
    }
)

_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_REVISION_PATTERN = r"^[0-9a-f]{40}$"
_CHUNK_ID_PATTERN = r"^chunk_[0-9a-z_:-]{4,160}$"

_DEFAULT_DATASET = Path("evaluation/datasets/synthetic")
_DEFAULT_PROFILE = Path("configs/profiles/dev-offline.json")
_DEFAULT_GATES = Path("evaluation/gates/p08-gates.json")
_PRODUCTION_VARIANT_ID = "fts5-only"
_PUBLIC_CASE_COUNT = 52
_REVISION_LENGTH = 40
_TRACE_CONSUMER_CASES: tuple[tuple[PublicSlice, str], ...] = (
    ("exact", "eval_identifier_standard"),
    ("enumeration", "eval_long_cross_chunk_02"),
    ("multilevel_list", "eval_hierarchy_long"),
    ("table", "eval_table_merged"),
    ("colloquial_synonym", "eval_cjk_noise_01"),
    ("numeric_unit_negation_exception", "eval_table_empty_middle"),
    ("unanswerable", "eval_unanswerable_01"),
    ("ocr_only", "eval_text_box"),
    ("accepted_relation_consumer", "eval_hierarchy_preamble"),
)


class _FrozenModel(BaseModel):
    """禁止隐式字段和运行中修改的内部模型基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ComponentIdentity(_FrozenModel):
    """解析器或 Chunker 的版本化身份。"""

    component_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN)


class FrozenChunk(_FrozenModel):
    """不携带正文的单 Chunk 固定身份。"""

    chunk_id: str = Field(pattern=_CHUNK_ID_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_spans_sha256: str = Field(pattern=_SHA256_PATTERN)


class FixedChunkSet(_FrozenModel):
    """按 Chunk ID 排序且带整体摘要的不可变 Chunk 集合。"""

    chunks: tuple[FrozenChunk, ...] = Field(min_length=1)
    digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_digest_and_order(self) -> FixedChunkSet:
        expected = tuple(sorted(self.chunks, key=lambda item: item.chunk_id))
        if self.chunks != expected:
            raise ValueError("固定 Chunk 必须按 chunk_id 排序。")
        identifiers = tuple(item.chunk_id for item in self.chunks)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("固定 Chunk 包含重复 chunk_id。")
        if self.digest != fixed_chunk_digest(self.chunks):
            raise ValueError("固定 Chunk 摘要与实际身份不一致。")
        return self


class ActiveRevision(_FrozenModel):
    """一个 Project/KB scope 的活动 Revision。"""

    project_id: str = Field(min_length=1)
    knowledge_base_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)


class EmbeddingIdentity(_FrozenModel):
    """一个不可跨用的 Embedding slot 身份。"""

    slot_id: str = Field(min_length=1)
    vector_name: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    adapter_revision: str = Field(min_length=1)
    request_policy_sha256: str = Field(pattern=_SHA256_PATTERN)


class ProfileIdentity(_FrozenModel):
    """检索、生成、缓存和 serving 的固定身份。"""

    profile_id: str = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    retrieval_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    generation_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    cache_identity: str = Field(min_length=1)
    index_fingerprints: tuple[str, ...] = Field(min_length=1)
    serving_fingerprints: tuple[str, ...] = Field(min_length=1)


class HardwareIdentity(_FrozenModel):
    """本次基线实际运行硬件的安全身份。"""

    system: str = Field(min_length=1)
    machine: str = Field(min_length=1)
    cpu_model: str = Field(min_length=1)
    logical_cpu_count: int = Field(gt=0)
    memory_bytes: int = Field(gt=0)
    accelerator: str = Field(min_length=1)
    hardware_fingerprint: str = Field(pattern=_SHA256_PATTERN)


class ProcessIdentity(_FrozenModel):
    """解释器、进程、包锁和可选镜像的运行身份。"""

    process_id: int = Field(gt=0)
    python_version: str = Field(min_length=1)
    executable_sha256: str = Field(pattern=_SHA256_PATTERN)
    dependency_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    image_identity: str = Field(min_length=1)
    process_fingerprint: str = Field(pattern=_SHA256_PATTERN)


class FrozenDocument(_FrozenModel):
    """语料中的逻辑文档及私有本地内容摘要。"""

    document_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)


class SourceIdentity(_FrozenModel):
    """B1 源码、标签来源和语料身份。"""

    execution_source_revision: str = Field(pattern=_REVISION_PATTERN)
    label_source_revision: str = Field(pattern=_REVISION_PATTERN)
    dataset_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=_SHA256_PATTERN)
    documents: tuple[FrozenDocument, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_label_source(self) -> SourceIdentity:
        if self.label_source_revision != B1_LABEL_REVISION:
            raise ValueError("B1 必须使用 5505e8b 冻结的当前 Chunk 标签。")
        expected = tuple(
            sorted(self.documents, key=lambda item: item.document_id)
        )
        if self.documents != expected:
            raise ValueError("语料文档身份必须按 document_id 排序。")
        return self


class FrozenBaselineIdentity(_FrozenModel):
    """V3-02C 构建完成后冻结的全部运行身份。"""

    parser: ComponentIdentity
    chunker: ComponentIdentity
    chunks: FixedChunkSet
    active_revisions: tuple[ActiveRevision, ...] = Field(min_length=1)
    embedding_slots: tuple[EmbeddingIdentity, ...] = Field(min_length=1)
    profile: ProfileIdentity
    cache_conditions: tuple[CacheCondition, ...] = CACHE_CONDITION_ORDER
    hardware: HardwareIdentity
    process: ProcessIdentity
    source: SourceIdentity

    @model_validator(mode="after")
    def _validate_fixed_order(self) -> FrozenBaselineIdentity:
        if self.cache_conditions != CACHE_CONDITION_ORDER:
            raise ValueError("基线必须同时冻结 cold/warm 缓存条件。")
        revisions = tuple(
            sorted(
                self.active_revisions,
                key=lambda item: (item.project_id, item.knowledge_base_id),
            )
        )
        if self.active_revisions != revisions:
            raise ValueError("活动 Revision 必须按 scope 排序。")
        slots = tuple(
            sorted(self.embedding_slots, key=lambda item: item.slot_id)
        )
        if self.embedding_slots != slots:
            raise ValueError("Embedding slot 必须按 slot_id 排序。")
        return self


class FrozenBuild(_FrozenModel):
    """一次且仅一次语料构建的固定结果。"""

    identity: FrozenBaselineIdentity
    build_duration_ns: int = Field(ge=0)


class FixedChunkCase(_FrozenModel):
    """公开合成查询及其固定 Chunk 期望。"""

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,100}$")
    slice_id: PublicSlice
    query: str = Field(min_length=1, max_length=1000, exclude=True)
    relevant_chunk_ids: tuple[str, ...]
    answerable: bool

    @model_validator(mode="after")
    def _validate_expected_chunks(self) -> FixedChunkCase:
        if self.answerable and not self.relevant_chunk_ids:
            raise ValueError("可回答 Case 必须固定 relevant_chunk_ids。")
        if not self.answerable and self.relevant_chunk_ids:
            raise ValueError("不可回答 Case 不能固定 relevant_chunk_ids。")
        return self

    @property
    def query_sha256(self) -> str:
        """返回不泄漏公开查询正文的稳定摘要。

        Args:
            无参数；读取当前固定 Case。

        Returns:
            带 ``sha256:`` 前缀的问题正文摘要。

        """
        return _sha256_bytes(self.query.encode("utf-8"))


class ChannelCandidates(_FrozenModel):
    """单通道保持排名顺序的候选 Chunk。"""

    channel: str = Field(min_length=1)
    chunk_ids: tuple[str, ...]


class StageDuration(_FrozenModel):
    """单个查询阶段的单调时钟耗时。"""

    stage: str = Field(min_length=1)
    duration_ns: int = Field(ge=0)


class TraceMeasurement(_FrozenModel):
    """单次查询产生的 writer、逻辑存储和 Artifact 增量。"""

    submitted_count: int = Field(ge=0)
    written_count: int = Field(ge=0)
    dropped_count: int = Field(ge=0)
    queue_high_water: int = Field(ge=0)
    storage_bytes: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    artifact_bytes: int = Field(ge=0)
    synchronous_artifact_read_count: int = Field(ge=0)


class QueryMeasurement(_FrozenModel):
    """Product 同一查询链返回的无正文测量。"""

    case_id: str
    mode: TraceMode
    cache_condition: CacheCondition
    chunk_digest: str = Field(pattern=_SHA256_PATTERN)
    active_revision_ids: tuple[str, ...] = Field(min_length=1)
    index_fingerprints: tuple[str, ...] = Field(min_length=1)
    serving_fingerprints: tuple[str, ...] = Field(min_length=1)
    profile_sha256: str = Field(pattern=_SHA256_PATTERN)
    channel_candidates: tuple[ChannelCandidates, ...]
    evidence_chunk_ids: tuple[str, ...]
    cited_chunk_ids: tuple[str, ...]
    answered: bool
    answer_correct: bool | None
    refusal_reason: str | None = None
    total_duration_ns: int = Field(ge=0)
    retrieval_duration_ns: int = Field(ge=0)
    trace_capture_overhead_ns: int = Field(ge=0)
    provider_duration_ns: int = Field(ge=0)
    stage_durations: tuple[StageDuration, ...]
    provider_call_count: int = Field(ge=0)
    provider_retry_count: int = Field(ge=0)
    observed_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_microunits: int | None = Field(default=None, ge=0)
    external_service_call_count: int = Field(ge=0)
    cache_hit: bool
    singleflight_hit: bool
    retrieval_execution_count: int = Field(ge=0)
    generation_execution_count: int = Field(ge=0)
    trace: TraceMeasurement

    @model_validator(mode="after")
    def _validate_timing_and_execution(self) -> QueryMeasurement:
        if self.retrieval_duration_ns > self.total_duration_ns:
            raise ValueError("检索耗时不能大于含 Trace 捕获的总耗时。")
        if (
            self.trace_capture_overhead_ns
            != self.total_duration_ns - self.retrieval_duration_ns
        ):
            raise ValueError("Trace 捕获开销必须等于总耗时减检索耗时。")
        if self.provider_duration_ns > self.total_duration_ns:
            raise ValueError("Provider 耗时不能大于总耗时。")
        if self.retrieval_execution_count > 1:
            raise ValueError("Trace 不得重复执行检索。")
        if self.generation_execution_count > 1:
            raise ValueError("Trace 不得重复执行生成。")
        expected_cache_hit = self.cache_condition == "warm"
        if self.cache_hit is not expected_cache_hit:
            raise ValueError("实际 cache hit 与冻结 cold/warm 条件不一致。")
        return self


class ModeMetrics(_FrozenModel):
    """一个 Trace 模式和缓存条件的质量与工程聚合。"""

    mode: TraceMode
    cache_condition: CacheCondition
    case_count: int = Field(gt=0)
    evidence_recall_at_5: float = Field(ge=0.0, le=1.0)
    citation_accuracy: float = Field(ge=0.0, le=1.0)
    answer_accuracy: float = Field(ge=0.0, le=1.0)
    erroneous_refusal_rate: float = Field(ge=0.0, le=1.0)
    erroneous_answer_rate: float = Field(ge=0.0, le=1.0)
    p50_total_duration_ns: int = Field(ge=0)
    p95_total_duration_ns: int = Field(ge=0)
    p50_non_provider_duration_ns: int = Field(ge=0)
    p95_non_provider_duration_ns: int = Field(ge=0)
    p50_trace_capture_overhead_ns: int = Field(ge=0)
    p95_trace_capture_overhead_ns: int = Field(ge=0)
    p50_added_vs_safe_ns: int
    p95_added_vs_safe_ns: int
    stage_duration_ns: dict[str, dict[str, int]]
    channel_candidate_count: dict[str, int]
    provider_call_count: int = Field(ge=0)
    provider_retry_count: int = Field(ge=0)
    observed_tokens: int | None = Field(default=None, ge=0)
    unknown_usage_count: int = Field(ge=0)
    estimated_cost_microunits: int | None = Field(default=None, ge=0)
    cache_hit_count: int = Field(ge=0)
    singleflight_hit_count: int = Field(ge=0)
    trace_submitted_count: int = Field(ge=0)
    trace_written_count: int = Field(ge=0)
    trace_dropped_count: int = Field(ge=0)
    trace_queue_high_water: int = Field(ge=0)
    trace_storage_bytes: int = Field(ge=0)
    trace_artifact_count: int = Field(ge=0)
    trace_artifact_bytes: int = Field(ge=0)


class UpstreamGateEvidence(_FrozenModel):
    """既有 Evaluation V3 等上游门禁的实际终态。"""

    gate_id: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    reason: str = Field(min_length=1)
    metrics: dict[str, JsonScalar] = Field(default_factory=dict)


class ProductionRetrievalReceipt(_FrozenModel):
    """固定 Chunk 上直接执行当前检索链的 52 Case 收据。"""

    execution_path: Literal["production_retrieval_path"] = (
        "production_retrieval_path"
    )
    variant_id: Literal["fts5-only"] = "fts5-only"
    changed_variable: Literal["enabled_channels"] = "enabled_channels"
    case_count: int = Field(gt=0)
    build_execution_count: Literal[1] = 1
    observations_sha256: str = Field(pattern=_SHA256_PATTERN)
    cases_sha256: str = Field(pattern=_SHA256_PATTERN)
    total_duration_ns: int = Field(ge=0)
    metric_report: dict[str, object]
    gate_report: dict[str, object]
    status: Literal["PASS", "FAIL"]
    candidate_selection_bypassed: Literal[True] = True
    external_service_call_count: Literal[0] = 0
    quality_claim_scope: Literal["public_synthetic_product_retrieval"] = (
        "public_synthetic_product_retrieval"
    )


class TraceConsumerContract(_FrozenModel):
    """一个 Trace 开销 Case 对应的真实 Evaluation V3 查询。"""

    slice_id: PublicSlice
    source_case_id: str = Field(min_length=1)
    source_contract: Literal["synthetic_trace_consumer"] = (
        "synthetic_trace_consumer"
    )
    product_quality_claimed: Literal[False] = False
    visual_quality_claimed: Literal[False] = False


class TraceConsumerReceipt(_FrozenModel):
    """同一实际 Chunk 上执行的九切片 Trace 开销边界。"""

    execution_path: Literal["synthetic_trace_consumer"] = (
        "synthetic_trace_consumer"
    )
    contracts: tuple[TraceConsumerContract, ...] = Field(min_length=1)
    mode_count: Literal[3] = 3
    cache_conditions: tuple[CacheCondition, ...] = CACHE_CONDITION_ORDER
    retrieval_or_generation_simulated: Literal[False] = False
    provider_success_simulated: Literal[False] = False
    product_quality_claimed: Literal[False] = False
    visual_quality_claimed: Literal[False] = False


class GateOutcome(_FrozenModel):
    """固定基线质量或上游门禁的可审计判定。"""

    gate_id: str = Field(min_length=1)
    source: Literal["fixed_chunk_b1", "upstream"]
    passed: bool
    observed: JsonScalar
    operator: Literal["eq", "ge", "le"]
    expected: JsonScalar
    reason: str = Field(min_length=1)


class CaseResult(_FrozenModel):
    """报告中不携带查询正文的单次查询结果。"""

    execution_path: Literal["synthetic_trace_consumer"] = (
        "synthetic_trace_consumer"
    )
    case_id: str
    slice_id: PublicSlice
    query_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurement: QueryMeasurement


class B0Reference(_FrozenModel):
    """不可由当前代码回算的 V3-02 前基线引用。"""

    baseline_id: Literal["B0"] = "B0"
    entry_source_revision: str = Field(pattern=_REVISION_PATTERN)
    execution_source_revision: str = Field(pattern=_REVISION_PATTERN)
    label_source_revision: str = Field(pattern=_REVISION_PATTERN)
    state: Literal["frozen_artifact_required"] = "frozen_artifact_required"
    manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_source_revision(self) -> B0Reference:
        if self.entry_source_revision != B0_SOURCE_REVISION:
            raise ValueError("B0 查询链进入点必须绑定 0505fac。")
        if self.execution_source_revision != B0_EXECUTION_REVISION:
            raise ValueError("B0 执行必须绑定只刷新标签的 5505e8b。")
        if self.label_source_revision != B1_LABEL_REVISION:
            raise ValueError("B0/B1 必须复用 5505e8b 的当前 Chunk 标签。")
        return self


class BaselineReport(_FrozenModel):
    """B1 当前执行结果及不可回算的 B0 引用。"""

    schema_version: Literal["v3-02-fixed-chunk-1"] = "v3-02-fixed-chunk-1"
    baseline_id: BaselineId = "B1"
    b0: B0Reference
    build: FrozenBuild
    case_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    mode_metrics: tuple[ModeMetrics, ...]
    results: tuple[CaseResult, ...]
    gates: tuple[GateOutcome, ...]
    upstream_gates: tuple[UpstreamGateEvidence, ...]
    production_retrieval: ProductionRetrievalReceipt | None = None
    trace_consumer: TraceConsumerReceipt | None = None
    status: Literal["PASS", "FAIL"]
    quality_equivalent_across_trace_modes: bool
    provider_calls_equivalent_across_trace_modes: bool
    external_services_actually_called: tuple[str, ...] = ()
    real_provider_quality_claimed: Literal[False] = False
    real_visual_quality_claimed: Literal[False] = False


class BaselineManifest(_FrozenModel):
    """B1 报告文件、输入身份和比较边界的稳定 Manifest。"""

    schema_version: Literal["v3-02-manifest-1"] = "v3-02-manifest-1"
    baseline_id: BaselineId = "B1"
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    fixed_chunk_digest: str = Field(pattern=_SHA256_PATTERN)
    case_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    b0_entry_source_revision: str = Field(pattern=_REVISION_PATTERN)
    b0_execution_source_revision: str = Field(pattern=_REVISION_PATTERN)
    b0_label_source_revision: str = Field(pattern=_REVISION_PATTERN)
    b0_state: Literal["frozen_artifact_required"]
    report_status: Literal["PASS", "FAIL"]
    b1_execution_source_revision: str = Field(pattern=_REVISION_PATTERN)
    b1_label_source_revision: str = Field(pattern=_REVISION_PATTERN)
    trace_modes: tuple[str, ...]
    cache_conditions: tuple[CacheCondition, ...]
    external_services_actually_called: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BaselineBundle:
    """待写出的 canonical 报告、Manifest 与摘要清单。"""

    report_json: bytes
    manifest_json: bytes
    checksums: bytes
    report_sha256: str
    manifest_sha256: str


class FixedChunkRuntime(Protocol):
    """当前 Product 查询链需提供的最小基线适配合同。"""

    def build_once(self) -> FrozenBuild:
        """构建一次语料并返回全部固定身份。

        Args:
            无参数；使用实现已配置的数据集与 Profile。

        Returns:
            单次构建得到的固定身份与构建耗时。

        """

    def reset_query_cache(self, mode: TraceMode) -> None:
        """在每个 Trace 模式开始前恢复同一 cold 条件。

        Args:
            mode: 即将测量的 Trace 捕获模式。

        Returns:
            无返回值。

        """

    def execute(
        self,
        case: FixedChunkCase,
        mode: TraceMode,
        cache_condition: CacheCondition,
    ) -> QueryMeasurement:
        """通过当前 Product 同一查询执行链执行一次测量。

        Args:
            case: 绑定固定 Chunk 标签的公开合成 Case。
            mode: 本次 Trace 捕获模式。
            cache_condition: 本次必须满足的 cold 或 warm 条件。

        Returns:
            不含正文的单次查询与 Trace 测量。

        """


@dataclass(frozen=True, slots=True)
class _CorpusState:
    """一次真实构建后供生产检索和 Trace 消费器共享的状态。"""

    runtime: P07Runtime
    revisions: dict[tuple[str, str], str]
    chunks: dict[str, Chunk]
    cases: tuple[EvaluationCase, ...]
    build: FrozenBuild


class DeterministicProductBaselineRuntime:
    """用 P07 Product 检索链执行固定 Chunk 基线。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        dataset: LoadedDataset,
        requested_profile: RagProfile,
        data_directory: Path,
        trace_database: Path,
        execution_revision: str,
        gate_configuration: object,
    ) -> None:
        """保存输入，并创建独立 Operational Trace writer。

        Args:
            dataset: 已验证的公开合成 Evaluation V3 数据集。
            requested_profile: 用户显式选择的离线 Profile。
            data_directory: 本次运行独占的 P07 状态目录。
            trace_database: 本次运行独占的 Operational Trace 数据库。
            execution_revision: 当前已提交源码 SHA。
            gate_configuration: 已由 Evaluation V3 加载器验证的门禁。

        """
        self._dataset = dataset
        self._requested_profile = requested_profile
        self._data_directory = data_directory
        self._execution_revision = execution_revision
        self._gate_configuration = gate_configuration
        self._variant = _fts5_only_variant()
        self._state: _CorpusState | None = None
        self._build_execution_count = 0
        self._consumer_cases: dict[str, EvaluationCase] = {}
        self._closed = False
        trace_database.parent.mkdir(parents=True, exist_ok=False)
        self._trace_store = TraceStore(trace_database)
        self._trace_store.initialize()
        self._trace_recorder = TraceRecorder(
            self._trace_store,
            config=TraceRecorderConfig(
                full_artifact_reservation_bytes=64 * 1024
            ),
        )

    @property
    def build_execution_count(self) -> int:
        """返回真实语料构建次数。

        Args:
            无参数；读取当前 runtime 计数。

        Returns:
            已完成的语料构建次数。

        """
        return self._build_execution_count

    def build_once(self) -> FrozenBuild:
        """构建且只构建一次 Evaluation V3 固定语料。

        Args:
            无参数；使用初始化时冻结的数据集与 Profile。

        Returns:
            固定 Chunk、Revision、组件和运行环境身份。

        """
        if self._closed:
            raise RuntimeError("固定 Chunk runtime 已关闭。")
        if self._state is not None:
            return self._state.build
        started = time.perf_counter_ns()
        profile = _offline_persistent_profile(
            self._requested_profile, self._variant
        )
        runtime = build_p07_runtime(
            profile,
            data_dir=self._data_directory,
            policy=self._variant.retrieval_policy,
        )
        try:
            revisions, chunks = _build_corpus(self._dataset, runtime)
            cases = _effective_cases(
                self._dataset.cases,
                chunks,
                require_fixed_labels=True,
            )
            identity = _freeze_product_identity(
                dataset=self._dataset,
                profile=profile,
                runtime=runtime,
                variant=self._variant,
                revisions=revisions,
                chunks=chunks,
                execution_revision=self._execution_revision,
            )
            build = FrozenBuild(
                identity=identity,
                build_duration_ns=time.perf_counter_ns() - started,
            )
            self._state = _CorpusState(
                runtime=runtime,
                revisions=revisions,
                chunks=chunks,
                cases=cases,
                build=build,
            )
            self._build_execution_count += 1
            self._consumer_cases = _trace_consumer_case_map(cases)
            return build
        except Exception:
            runtime.close()
            raise

    def fixed_trace_cases(self) -> tuple[FixedChunkCase, ...]:
        """返回绑定实际 Chunk 的九切片 Trace 消费器 Case。

        Args:
            无参数；读取构建后的公开合成 Case。

        Returns:
            按合同顺序排列的九个固定 Trace Case。

        """
        self.build_once()
        by_source = self._consumer_cases
        return tuple(
            FixedChunkCase(
                case_id=source_case_id,
                slice_id=slice_id,
                query=by_source[source_case_id].query,
                relevant_chunk_ids=(
                    by_source[source_case_id].expected.relevant_chunk_ids
                ),
                answerable=by_source[source_case_id].expected.answerable,
            )
            for slice_id, source_case_id in _TRACE_CONSUMER_CASES
        )

    def trace_consumer_receipt(self) -> TraceConsumerReceipt:
        """返回九切片只测 Trace 消费行为的明确声明。

        Args:
            无参数；读取固定切片映射。

        Returns:
            不声明 Product 或视觉质量的 Trace consumer 收据。

        """
        return TraceConsumerReceipt(
            contracts=tuple(
                TraceConsumerContract(
                    slice_id=slice_id,
                    source_case_id=source_case_id,
                )
                for slice_id, source_case_id in _TRACE_CONSUMER_CASES
            )
        )

    def execute_production_retrieval(self) -> ProductionRetrievalReceipt:
        """绕过候选选择，直接执行 fts5-only 的全部 52 Case。

        Args:
            无参数；复用同一次固定语料构建。

        Returns:
            当前 Production Retrieval 的指标与门禁收据。

        """
        self.build_once()
        state = self._require_state()
        self._replace_query_cache(TraceMode.SAFE)
        observations: list[CaseObservation] = []
        started = time.perf_counter_ns()
        for case in state.cases:
            query_started = time.perf_counter_ns()
            result = state.runtime.retrieval.search_and_answer(
                SearchRequest(
                    scope=KnowledgeBaseScope(
                        project_id=case.project_id,
                        knowledge_base_id=case.knowledge_base_id,
                    ),
                    text=case.query,
                    limit=10,
                )
            )
            observations.append(
                observe_case_result(
                    case,
                    result,
                    self._observation_context(case),
                    latency_ms=(time.perf_counter_ns() - query_started)
                    / 1_000_000,
                )
            )
        total_duration_ns = time.perf_counter_ns() - started
        metric_report = compute_metric_report(
            state.cases,
            observations,
            context=MetricContext(
                lane="production-retrieval-path",
                variant_id=self._variant.variant_id,
                split="all-52-public-synthetic",
                seed=20260908,
            ),
        )
        gate_report = evaluate_gates(metric_report, self._gate_configuration)
        if self._build_execution_count != 1:
            raise RuntimeError("固定语料并非只构建一次。")
        return ProductionRetrievalReceipt(
            case_count=len(state.cases),
            observations_sha256=canonical_sha256(
                [item.model_dump(mode="json") for item in observations]
            ),
            cases_sha256=canonical_sha256(
                [item.model_dump(mode="json") for item in state.cases]
            ),
            total_duration_ns=total_duration_ns,
            metric_report=cast(
                dict[str, object], metric_report.model_dump(mode="json")
            ),
            gate_report=cast(
                dict[str, object], gate_report.model_dump(mode="json")
            ),
            status="PASS" if gate_report.passed else "FAIL",
        )

    def reset_query_cache(self, mode: TraceMode) -> None:
        """为一种 Trace 模式创建新的进程内查询缓存。

        Args:
            mode: 即将测量的 Trace 模式；只用于建立测量边界。

        Returns:
            无返回值。

        """
        self.build_once()
        self._replace_query_cache(mode)

    def execute(
        self,
        case: FixedChunkCase,
        mode: TraceMode,
        cache_condition: CacheCondition,
    ) -> QueryMeasurement:
        """执行一次实际 P07 查询并消费其诊断形成 Operational Trace。

        Args:
            case: 当前公开合成固定 Case。
            mode: SAFE、DIAGNOSTIC 或 FULL。
            cache_condition: 预期的 cold 或 warm 缓存条件。

        Returns:
            质量、耗时、调用量、缓存与 Trace 资源测量。

        """
        state = self._require_state()
        source_case = self._consumer_cases[case.case_id]
        metrics_before = self._trace_recorder.metrics
        trace_id = _trace_id(
            case.case_id, mode, cache_condition, case.query_sha256
        )
        started = time.perf_counter_ns()
        session = self._trace_recorder.begin_query(
            trace_id,
            mode,
            datetime.now(UTC),
            self._trace_identity(source_case),
            question_sha256=case.query_sha256.removeprefix("sha256:"),
        )
        retrieval_started = time.perf_counter_ns()
        result = state.runtime.retrieval.search_and_answer(
            SearchRequest(
                scope=KnowledgeBaseScope(
                    project_id=source_case.project_id,
                    knowledge_base_id=source_case.knowledge_base_id,
                ),
                text=source_case.query,
                limit=10,
                trace_id=trace_id,
            )
        )
        retrieval_duration_ns = time.perf_counter_ns() - retrieval_started
        self._record_trace_consumer(session, result, mode)
        answered = result.status is ConfidenceStatus.ANSWERABLE
        session.finish(
            status=(TraceStatus.ANSWERED if answered else TraceStatus.REFUSED),
            reason_code=(
                DecisionCode.ANSWERED if answered else DecisionCode.REFUSED
            ),
            refusal_code=None if answered else result.reason_code,
        )
        total_duration_ns = time.perf_counter_ns() - started
        self._trace_recorder.flush()
        detail = self._trace_store.get_trace(trace_id)
        canonical_export = self._trace_store.export_trace(trace_id)
        metrics_after = self._trace_recorder.metrics
        diagnostics = _require_diagnostics(result)
        provider_calls = sum(
            item.call_count for item in diagnostics.provider_calls
        )
        provider_retries = sum(
            item.retry_count for item in diagnostics.provider_calls
        )
        observed_usage = [
            item.observed_tokens
            for item in diagnostics.provider_call_details
            if item.observed_tokens is not None
        ]
        provider_duration_ns = (
            sum(item.elapsed_ms for item in diagnostics.provider_call_details)
            * 1_000_000
        )
        evidence_chunk_ids = tuple(item.chunk_id for item in result.evidence)
        correct = _answer_correct(source_case, result)
        identity = state.build.identity
        return QueryMeasurement(
            case_id=case.case_id,
            mode=mode,
            cache_condition="warm" if result.cache_hit else "cold",
            chunk_digest=identity.chunks.digest,
            active_revision_ids=tuple(
                item.revision_id for item in identity.active_revisions
            ),
            index_fingerprints=identity.profile.index_fingerprints,
            serving_fingerprints=identity.profile.serving_fingerprints,
            profile_sha256=identity.profile.profile_sha256,
            channel_candidates=tuple(
                ChannelCandidates(channel=channel, chunk_ids=chunk_ids)
                for channel, chunk_ids in diagnostics.channel_chunk_ids
            ),
            evidence_chunk_ids=evidence_chunk_ids,
            cited_chunk_ids=evidence_chunk_ids if answered else (),
            answered=answered,
            answer_correct=correct,
            refusal_reason=None if answered else result.reason_code,
            total_duration_ns=total_duration_ns,
            retrieval_duration_ns=retrieval_duration_ns,
            trace_capture_overhead_ns=(
                total_duration_ns - retrieval_duration_ns
            ),
            provider_duration_ns=min(provider_duration_ns, total_duration_ns),
            stage_durations=tuple(
                StageDuration(
                    stage=item.stage,
                    duration_ns=round(item.elapsed_ms * 1_000_000),
                )
                for item in diagnostics.stage_timings
            ),
            provider_call_count=provider_calls,
            provider_retry_count=provider_retries,
            observed_tokens=sum(observed_usage) if observed_usage else None,
            estimated_cost_microunits=None,
            external_service_call_count=0,
            cache_hit=result.cache_hit,
            singleflight_hit=False,
            retrieval_execution_count=int(not result.cache_hit),
            generation_execution_count=int(
                result.generation_called_this_request
            ),
            trace=TraceMeasurement(
                submitted_count=(
                    metrics_after["submitted"] - metrics_before["submitted"]
                ),
                written_count=(
                    metrics_after["written"] - metrics_before["written"]
                ),
                dropped_count=(
                    metrics_after["dropped"] - metrics_before["dropped"]
                ),
                queue_high_water=metrics_after["queue_high_water"],
                storage_bytes=len(canonical_export),
                artifact_count=len(detail.artifacts),
                artifact_bytes=sum(
                    item.original_bytes for item in detail.artifacts
                ),
                synchronous_artifact_read_count=0,
            ),
        )

    def close(self) -> None:
        """关闭 Trace writer、缓存与 P07 持久资源。

        Args:
            无参数；关闭当前 runtime 持有的资源。

        Returns:
            无返回值。

        """
        if self._closed:
            return
        self._closed = True
        self._trace_recorder.close()
        if self._state is not None:
            self._state.runtime.close()

    def __enter__(self) -> DeterministicProductBaselineRuntime:
        """进入由调用方管理的基线运行作用域。"""
        return self

    def __exit__(self, *args: object) -> None:
        """离开作用域并关闭所有本地资源。"""
        del args
        self.close()

    def _require_state(self) -> _CorpusState:
        if self._state is None:
            self.build_once()
        if self._state is None:
            raise RuntimeError("固定语料构建未完成。")
        return self._state

    def _observation_context(self, case: EvaluationCase) -> ObservationContext:
        state = self._require_state()
        topology = state.runtime.persistence.components.embedding_topology
        return ObservationContext(
            chunks=state.chunks,
            documents=self._dataset.manifest.documents,
            expected_revision=state.revisions[
                (case.project_id, case.knowledge_base_id)
            ],
            expected_vectors=tuple(
                (slot.slot_id, slot.vector_name) for slot in topology.slots
            ),
            variant_id=self._variant.variant_id,
            lane="production-retrieval-path",
            evidence_token_budget=(
                self._variant.retrieval_policy.evidence_token_budget
            ),
        )

    def _replace_query_cache(self, mode: TraceMode) -> None:
        del mode
        state = self._require_state()
        runtime = state.runtime
        components = runtime.persistence.components
        if not isinstance(components.lexical_store, SqliteFtsStore):
            raise TypeError("固定基线要求 sqlite-fts5 Product 检索链。")
        runtime.cache.close()
        cache = InMemoryRetrievalCache()
        serving = canonical_sha256(
            {
                "base_serving_fingerprint": components.serving_fingerprint,
                "retrieval_policy": self._variant.retrieval_policy.model_dump(
                    mode="json"
                ),
            }
        )
        runtime.retrieval = RetrievalService(
            source=runtime.persistence.control,
            exact_store=cast(ExactStorePort, components.lexical_store),
            lexical_store=components.lexical_store,
            vector_store=components.vector_store,
            query_embedding=components.query_embedding_router,
            reranker=components.reranker,
            generator=components.generator,
            trace=components.trace_sink,
            cache=cache,
            serving_fingerprint=serving,
            egress_policy=components.profile.security,
            policy=self._variant.retrieval_policy,
        )
        runtime.cache = cache

    def _trace_identity(self, case: EvaluationCase) -> TraceIdentity:
        state = self._require_state()
        identity = state.build.identity
        revision = state.revisions[(case.project_id, case.knowledge_base_id)]
        return TraceIdentity(
            pipeline_fingerprint=identity.chunks.digest,
            serving_fingerprint=identity.profile.serving_fingerprints[0],
            release_revision=self._execution_revision,
            active_collection=revision,
            index_manifest_sha256=identity.chunks.digest.removeprefix(
                "sha256:"
            ),
            payload_schema_version=1,
            project_id=case.project_id,
            knowledge_base_id=case.knowledge_base_id,
            owner_sha256=canonical_sha256(
                "public-synthetic-baseline"
            ).removeprefix("sha256:"),
            revision_id=revision,
            profile_id=identity.profile.profile_id,
            index_fingerprint=identity.profile.index_fingerprints[0],
            source_revision=self._execution_revision,
        )

    def _record_trace_consumer(
        self,
        session: TraceSession,
        result: SearchAnswerResult,
        mode: TraceMode,
    ) -> None:
        diagnostics = _require_diagnostics(result)
        for timing in diagnostics.stage_timings:
            session.completed_span(
                TraceSpanSpec(
                    name=f"rag.retrieval.{timing.stage}",
                    kind=SpanKind.RETRIEVER,
                    parent_span_id=session.root.span_id,
                    reason_code=DecisionCode.RETRIEVAL_OK,
                    duration_ms=round(timing.elapsed_ms),
                )
            )
        if mode is not TraceMode.SAFE:
            for channel, chunk_ids in diagnostics.channel_chunk_ids:
                for rank, chunk_id in enumerate(chunk_ids, start=1):
                    session.decision(
                        stage=channel,
                        chunk_id=chunk_id,
                        selected=chunk_id
                        in {item.chunk_id for item in diagnostics.reranked},
                        reason_code=DecisionCode.SELECTED,
                        details={"consumer": "fixed-chunk-baseline"},
                        channel=channel,
                        rank=rank,
                    )
        session.artifact(
            "retrieval-diagnostics",
            diagnostics.model_dump(mode="json"),
        )


def _fts5_only_variant() -> EvaluationVariant:
    """返回仓库定义的唯一 fts5-only 候选。"""
    matches = tuple(
        item
        for item in offline_variants()
        if item.variant_id == _PRODUCTION_VARIANT_ID
    )
    if len(matches) != 1:
        raise RuntimeError("fts5-only Evaluation V3 候选不唯一。")
    return matches[0]


def _freeze_product_identity(  # noqa: PLR0913
    *,
    dataset: LoadedDataset,
    profile: RagProfile,
    runtime: P07Runtime,
    variant: EvaluationVariant,
    revisions: Mapping[tuple[str, str], str],
    chunks: Mapping[str, Chunk],
    execution_revision: str,
) -> FrozenBaselineIdentity:
    """从实际 P07 corpus 和组件冻结不含正文的身份。"""
    components = runtime.persistence.components
    frozen_chunks = freeze_chunk_set(
        tuple(
            FrozenChunk(
                chunk_id=chunk.chunk_id,
                content_sha256=_sha256_bytes(
                    chunk.citation_text.encode("utf-8")
                ),
                source_spans_sha256=canonical_sha256(
                    [
                        span.model_dump(mode="json")
                        for span in chunk.source_spans
                    ]
                ),
            )
            for chunk in chunks.values()
        )
    )
    topology = components.embedding_topology
    profile_sha256 = canonical_sha256(profile.model_dump(mode="json"))
    index_fingerprints = (components.index_fingerprint,)
    serving_fingerprint = _retrieval_serving_fingerprint(
        components.serving_fingerprint, variant
    )
    hardware = _hardware_identity()
    process = _process_identity()
    return FrozenBaselineIdentity(
        parser=ComponentIdentity(
            component_id=components.parser.descriptor.name,
            version=components.parser.descriptor.version,
            policy_sha256=canonical_sha256(
                components.parsing_policy.model_dump(mode="json")
            ),
        ),
        chunker=ComponentIdentity(
            component_id=components.chunker.descriptor.name,
            version=components.chunker.descriptor.version,
            policy_sha256=canonical_sha256(
                components.chunking_policy.model_dump(mode="json")
            ),
        ),
        chunks=frozen_chunks,
        active_revisions=tuple(
            ActiveRevision(
                project_id=project_id,
                knowledge_base_id=knowledge_base_id,
                revision_id=revision_id,
            )
            for (project_id, knowledge_base_id), revision_id in sorted(
                revisions.items()
            )
        ),
        embedding_slots=tuple(
            EmbeddingIdentity(
                slot_id=slot.slot_id,
                vector_name=slot.vector_name,
                provider_id=slot.provider_id,
                model=slot.model,
                dimension=slot.dimension,
                adapter_revision=slot.adapter_revision,
                request_policy_sha256=canonical_sha256(
                    slot.query_request_policy
                ),
            )
            for slot in sorted(topology.slots, key=lambda item: item.slot_id)
        ),
        profile=ProfileIdentity(
            profile_id=profile.profile_id,
            profile_sha256=profile_sha256,
            retrieval_policy_sha256=canonical_sha256(
                variant.retrieval_policy.model_dump(mode="json")
            ),
            generation_policy_sha256=canonical_sha256(
                {
                    "generator": profile.components.generator,
                    "egress": profile.security.model_dump(mode="json"),
                }
            ),
            cache_identity="memory-retrieval-cache-v1",
            index_fingerprints=index_fingerprints,
            serving_fingerprints=(serving_fingerprint,),
        ),
        hardware=hardware,
        process=process,
        source=SourceIdentity(
            execution_source_revision=execution_revision,
            label_source_revision=B1_LABEL_REVISION,
            dataset_id=dataset.manifest.dataset_id,
            dataset_version=dataset.manifest.dataset_version,
            corpus_sha256=dataset.dataset_sha256,
            documents=tuple(
                FrozenDocument(
                    document_id=document.document_id,
                    content_sha256=_sha256_bytes(
                        fixture_bytes(document.versions[-1].fixture_id)
                    ),
                )
                for document in sorted(
                    dataset.manifest.documents,
                    key=lambda item: item.document_id,
                )
            ),
        ),
    )


def _retrieval_serving_fingerprint(
    base_serving_fingerprint: str,
    variant: EvaluationVariant,
) -> str:
    """复现 P07 组合根和 RetrievalService 的查询缓存身份。"""
    configured = canonical_sha256(
        {
            "base_serving_fingerprint": base_serving_fingerprint,
            "retrieval_policy": variant.retrieval_policy.model_dump(
                mode="json"
            ),
        }
    )
    return canonical_sha256(
        {
            "configured_serving": configured,
            "retrieval_implementation": "v3-04-semantic-query-v3",
        }
    )


def _hardware_identity() -> HardwareIdentity:
    """读取本地硬件身份，不发起外部探测。"""
    cpu_model = platform.processor().strip() or _linux_cpu_model()
    logical_cpu_count = os.cpu_count() or 1
    memory_bytes = _physical_memory_bytes()
    values: dict[str, str | int] = {
        "system": platform.system() or "unknown-system",
        "machine": platform.machine() or "unknown-machine",
        "cpu_model": cpu_model or "unknown-cpu",
        "logical_cpu_count": logical_cpu_count,
        "memory_bytes": memory_bytes,
        "accelerator": os.environ.get("RAG_ACCELERATOR_ID", "none"),
    }
    return HardwareIdentity(
        system=str(values["system"]),
        machine=str(values["machine"]),
        cpu_model=str(values["cpu_model"]),
        logical_cpu_count=logical_cpu_count,
        memory_bytes=memory_bytes,
        accelerator=str(values["accelerator"]),
        hardware_fingerprint=canonical_sha256(values),
    )


def _linux_cpu_model() -> str:
    """从 Linux procfs 读取首个 CPU 型号。"""
    try:
        lines = Path("/proc/cpuinfo").read_text("utf-8").splitlines()
    except (OSError, UnicodeError):
        return "unknown-cpu"
    for line in lines:
        if line.lower().startswith("model name"):
            return line.partition(":")[2].strip() or "unknown-cpu"
    return "unknown-cpu"


def _physical_memory_bytes() -> int:
    """返回当前系统物理内存容量，未知时使用最小正值。"""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 1
    if not isinstance(pages, int) or not isinstance(page_size, int):
        return 1
    return max(1, pages * page_size)


def _process_identity() -> ProcessIdentity:
    """冻结解释器、依赖锁和可选镜像身份。"""
    executable = Path(sys.executable).resolve()
    lock_file = next(
        (
            candidate
            for candidate in (Path("uv.lock"), Path("pyproject.toml"))
            if candidate.is_file()
        ),
        None,
    )
    lock_sha256 = (
        _file_sha256(lock_file)
        if lock_file is not None
        else canonical_sha256("dependency-lock-unavailable")
    )
    image_identity = os.environ.get("RAG_IMAGE_ID", "source-tree")
    values: dict[str, str | int] = {
        "process_id": os.getpid(),
        "python_version": platform.python_version(),
        "executable_sha256": _file_sha256(executable),
        "dependency_lock_sha256": lock_sha256,
        "image_identity": image_identity,
    }
    return ProcessIdentity(
        process_id=os.getpid(),
        python_version=platform.python_version(),
        executable_sha256=str(values["executable_sha256"]),
        dependency_lock_sha256=str(values["dependency_lock_sha256"]),
        image_identity=image_identity,
        process_fingerprint=canonical_sha256(values),
    )


def _file_sha256(path: Path) -> str:
    """流式计算普通文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _trace_consumer_case_map(
    cases: Sequence[EvaluationCase],
) -> dict[str, EvaluationCase]:
    """校验九切片映射全部来自实际 Evaluation V3 Case。"""
    mapped = {case.case_id: case for case in cases}
    required = {case_id for _, case_id in _TRACE_CONSUMER_CASES}
    missing = sorted(required - set(mapped))
    if missing:
        raise ValueError(f"Trace consumer 缺少实际 Case：{missing}")
    return {case_id: mapped[case_id] for case_id in required}


def _trace_id(
    case_id: str,
    mode: TraceMode,
    cache_condition: CacheCondition,
    query_sha256: str,
) -> str:
    """为一次矩阵单元生成无碰撞稳定 Trace ID。"""
    digest = hashlib.sha256(
        f"{case_id}:{mode.value}:{cache_condition}:{query_sha256}".encode()
    ).hexdigest()
    return f"trace_{digest[:32]}"


def _require_diagnostics(result: SearchAnswerResult) -> RetrievalDiagnostics:
    """返回真实完整诊断；缺失时失败关闭。"""
    if result.diagnostics is None:
        raise ValueError("固定基线要求 Product RetrievalDiagnostics。")
    return result.diagnostics


def _answer_correct(
    case: EvaluationCase, result: SearchAnswerResult
) -> bool | None:
    """按固定相关 Chunk 判断公开合成可回答 Case。"""
    if not case.expected.answerable:
        return None
    relevant = set(case.expected.relevant_chunk_ids)
    return result.status is ConfidenceStatus.ANSWERABLE and bool(
        relevant & {item.chunk_id for item in result.evidence}
    )


def run_concrete_fixed_chunk_baseline(  # noqa: PLR0913
    *,
    output_directory: Path,
    dataset_directory: Path,
    profile_path: Path,
    gate_path: Path,
    b0_revision: str,
    execution_revision: str,
    upstream_b0_failure: Path,
    upstream_b1_failure: Path,
    b0_manifest_sha256: str | None = None,
) -> BaselineBundle:
    """执行可复现的 52 Case Product 路径和九切片 Trace 矩阵。

    Args:
        output_directory: 尚不存在的正式基线输出目录。
        dataset_directory: Evaluation V3 公开合成数据集目录。
        profile_path: 无网络 deterministic Profile。
        gate_path: Evaluation V3 门禁配置。
        b0_revision: 进入 V3-02 前固定源码 SHA。
        execution_revision: 必须等于当前 Git HEAD 的 B1 源码 SHA。
        upstream_b0_failure: 已实际执行的 B0 失败收据。
        upstream_b1_failure: 已实际执行的 B1 失败收据。
        b0_manifest_sha256: 可选既有 B0 Manifest 摘要。

    Returns:
        已写入输出目录的 canonical baseline bundle。

    Raises:
        ValueError: Revision、失败收据或固定 Case 数不符合合同。
        FileExistsError: 输出目录已经存在。

    """
    _validate_execution_revisions(b0_revision, execution_revision)
    if output_directory.exists():
        raise FileExistsError(output_directory)
    dataset = load_dataset_directory(dataset_directory)
    if len(dataset.cases) != _PUBLIC_CASE_COUNT:
        raise ValueError("固定 Product 检索路径必须执行全部 52 Case。")
    profile = load_profile(profile_path)
    gate_configuration = load_gate_configuration(gate_path)
    upstream = (
        _load_failure_receipt(
            upstream_b0_failure,
            gate_id="evaluation_v3_b0_candidate_selection",
            revision=B0_EXECUTION_REVISION,
        ),
        _load_failure_receipt(
            upstream_b1_failure,
            gate_id="evaluation_v3_b1_candidate_selection",
            revision=execution_revision,
        ),
    )
    with tempfile.TemporaryDirectory(prefix="rag-v3-02c-") as temporary:
        temporary_root = Path(temporary)
        with DeterministicProductBaselineRuntime(
            dataset=dataset,
            requested_profile=profile,
            data_directory=temporary_root / "product-state",
            trace_database=temporary_root / "trace" / "trace.sqlite3",
            execution_revision=execution_revision,
            gate_configuration=gate_configuration,
        ) as runtime:
            production = runtime.execute_production_retrieval()
            production_gate = UpstreamGateEvidence(
                gate_id="production_retrieval_fts5_only_all_52",
                status=production.status,
                reason=("fts5-only 直接 Product 检索路径的 Evaluation V3 门禁"),
                metrics=_production_gate_metrics(production),
            )
            report = run_b1_fixed_chunk_baseline(
                runtime,
                runtime.fixed_trace_cases(),
                b0_manifest_sha256=b0_manifest_sha256,
                upstream_gates=(*upstream, production_gate),
            )
            report = BaselineReport.model_validate(
                {
                    **report.model_dump(mode="json"),
                    "production_retrieval": production.model_dump(mode="json"),
                    "trace_consumer": (
                        runtime.trace_consumer_receipt().model_dump(mode="json")
                    ),
                }
            )
    bundle = create_baseline_bundle(report)
    write_baseline_bundle(output_directory, bundle)
    return bundle


def _validate_execution_revisions(
    b0_revision: str, execution_revision: str
) -> None:
    """确认 B0 入口和 B1 执行 SHA 与当前 checkout 一致。"""
    if b0_revision != B0_SOURCE_REVISION:
        raise ValueError("--b0-revision 必须是 0505fac 固定进入点。")
    if not _is_full_revision(execution_revision):
        raise ValueError("--execution-revision 必须是 40 位 Git SHA。")
    git = shutil.which("git")
    if git is None:
        raise ValueError("当前环境缺少 Git，无法核对执行源码身份。")
    completed = subprocess.run(  # noqa: S603
        (git, "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    )
    observed = completed.stdout.strip()
    if observed != execution_revision:
        raise ValueError(
            "--execution-revision 与当前 Git HEAD 不一致，拒绝生成基线。"
        )


def _is_full_revision(value: str) -> bool:
    """判断字符串是否为 40 位小写 Git SHA。"""
    return len(value) == _REVISION_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def _load_failure_receipt(
    path: Path, *, gate_id: str, revision: str
) -> UpstreamGateEvidence:
    """加载已存在的失败收据，不把失败改写为未运行或通过。"""
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Evaluation V3 失败收据不可读取。") from error
    if not isinstance(value, dict) or value.get("state") != "failed":
        raise ValueError("Evaluation V3 上游收据必须明确 state=failed。")
    safe_message = value.get("safe_message")
    error_type = value.get("error_type")
    if not isinstance(safe_message, str) or not safe_message.strip():
        raise ValueError("Evaluation V3 上游收据缺少 safe_message。")
    if not isinstance(error_type, str) or not error_type.strip():
        raise ValueError("Evaluation V3 上游收据缺少 error_type。")
    return UpstreamGateEvidence(
        gate_id=gate_id,
        status="FAIL",
        reason=safe_message,
        metrics={
            "error_type": error_type,
            "receipt_sha256": _file_sha256(path),
            "revision": revision,
        },
    )


def _production_gate_metrics(
    receipt: ProductionRetrievalReceipt,
) -> dict[str, JsonScalar]:
    """提取足以定位生产检索门禁的有限指标。"""
    metrics = receipt.metric_report.get("metrics")
    if not isinstance(metrics, dict):
        return {"case_count": receipt.case_count}
    output: dict[str, JsonScalar] = {"case_count": receipt.case_count}
    for name in (
        "fusion_recall_at_5",
        "citation_chunk_precision",
        "source_range_precision",
        "source_range_recall",
        "source_range_f1",
        "irrelevant_evidence_count",
    ):
        metric = metrics.get(name)
        if isinstance(metric, dict):
            value = metric.get("value")
            if isinstance(value, (str, int, float, bool)) or value is None:
                output[name] = value
    return output


def _build_argument_parser() -> argparse.ArgumentParser:
    """构造 V3-02C concrete runner 命令行。"""
    parser = argparse.ArgumentParser(
        description="执行 V3-02C 固定 Chunk Product/Trace 基线。"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET)
    parser.add_argument("--profile", type=Path, default=_DEFAULT_PROFILE)
    parser.add_argument("--gates", type=Path, default=_DEFAULT_GATES)
    parser.add_argument("--b0-revision", required=True)
    parser.add_argument(
        "--execution-revision",
        "--b1-revision",
        dest="execution_revision",
        required=True,
    )
    parser.add_argument("--upstream-b0-failure", type=Path, required=True)
    parser.add_argument("--upstream-b1-failure", type=Path, required=True)
    parser.add_argument("--b0-manifest-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行命令行并以报告门禁状态决定退出码。

    Args:
        argv: 可选的显式命令行参数；省略时读取当前进程参数。

    Returns:
        报告全部门禁通过时返回 0，否则返回 1。

    """
    parsed = _build_argument_parser().parse_args(argv)
    bundle = run_concrete_fixed_chunk_baseline(
        output_directory=parsed.output,
        dataset_directory=parsed.dataset,
        profile_path=parsed.profile,
        gate_path=parsed.gates,
        b0_revision=parsed.b0_revision,
        execution_revision=parsed.execution_revision,
        upstream_b0_failure=parsed.upstream_b0_failure,
        upstream_b1_failure=parsed.upstream_b1_failure,
        b0_manifest_sha256=parsed.b0_manifest_sha256,
    )
    report = BaselineReport.model_validate_json(bundle.report_json)
    print(
        json.dumps(
            {
                "baseline_id": report.baseline_id,
                "manifest_sha256": bundle.manifest_sha256,
                "output": str(parsed.output),
                "report_sha256": bundle.report_sha256,
                "status": report.status,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report.status == "PASS" else 1


def fixed_chunk_digest(chunks: Sequence[FrozenChunk]) -> str:
    """计算不含正文但绑定内容与 SourceSpan 的 Chunk 集合摘要。

    Args:
        chunks: 已按稳定顺序提供的 Chunk 身份。

    Returns:
        带 ``sha256:`` 前缀的 canonical 摘要。

    """
    return canonical_sha256([chunk.model_dump(mode="json") for chunk in chunks])


def freeze_chunk_set(chunks: Sequence[FrozenChunk]) -> FixedChunkSet:
    """排序并冻结一组 Chunk 身份。

    Args:
        chunks: Product active revision 中的实际 Chunk 身份。

    Returns:
        可跨 Trace 模式复用的稳定 Chunk 集合。

    """
    ordered = tuple(sorted(chunks, key=lambda item: item.chunk_id))
    return FixedChunkSet(chunks=ordered, digest=fixed_chunk_digest(ordered))


def run_b1_fixed_chunk_baseline(
    runtime: FixedChunkRuntime,
    cases: Sequence[FixedChunkCase],
    *,
    b0_manifest_sha256: str | None = None,
    upstream_gates: Sequence[UpstreamGateEvidence] = (),
) -> BaselineReport:
    """构建一次并执行 B1 的三种 Trace 与冷暖缓存矩阵。

    Args:
        runtime: 复用当前 Product 查询执行链的适配器。
        cases: 覆盖全部必需公开合成切片的固定 Case。
        b0_manifest_sha256: 可选的既有 B0 冻结 Manifest 摘要。
        upstream_gates: 已实际运行的 Evaluation V3 等上游门禁结果。

    Returns:
        不含查询正文、可生成稳定 JSON 与摘要的 B1 报告。

    Raises:
        ValueError: 切片、身份、缓存、调用量或 Trace 边界发生漂移。

    """
    ordered_cases = _validate_and_order_cases(cases)
    build = runtime.build_once()
    _validate_case_chunks(ordered_cases, build.identity.chunks)
    results = _execute_matrix(runtime, ordered_cases, build.identity)
    _validate_cross_mode_boundaries(results)
    metrics = _aggregate_metrics(ordered_cases, results)
    ordered_upstream = _validate_upstream_gates(upstream_gates)
    gates = _evaluate_gates(metrics, ordered_upstream)
    return BaselineReport(
        b0=B0Reference(
            entry_source_revision=B0_SOURCE_REVISION,
            execution_source_revision=B0_EXECUTION_REVISION,
            label_source_revision=B1_LABEL_REVISION,
            manifest_sha256=b0_manifest_sha256,
        ),
        build=build,
        case_manifest_sha256=_case_manifest_sha256(ordered_cases),
        mode_metrics=metrics,
        results=tuple(
            CaseResult(
                case_id=case.case_id,
                slice_id=case.slice_id,
                query_sha256=case.query_sha256,
                measurement=measurement,
            )
            for measurement in results
            for case in ordered_cases
            if case.case_id == measurement.case_id
        ),
        gates=gates,
        upstream_gates=ordered_upstream,
        status="PASS" if all(item.passed for item in gates) else "FAIL",
        quality_equivalent_across_trace_modes=True,
        provider_calls_equivalent_across_trace_modes=True,
    )


def create_baseline_bundle(report: BaselineReport) -> BaselineBundle:
    """为 B1 报告创建 canonical JSON、Manifest 和 SHA 清单。

    Args:
        report: 已完成边界验证的 B1 报告。

    Returns:
        三个待写文件的确定性字节与摘要。

    """
    report_json = _canonical_json(report.model_dump(mode="json"))
    report_sha256 = _sha256_bytes(report_json)
    identity = report.build.identity
    manifest = BaselineManifest(
        report_sha256=report_sha256,
        identity_sha256=canonical_sha256(identity.model_dump(mode="json")),
        fixed_chunk_digest=identity.chunks.digest,
        case_manifest_sha256=report.case_manifest_sha256,
        b0_entry_source_revision=report.b0.entry_source_revision,
        b0_execution_source_revision=report.b0.execution_source_revision,
        b0_label_source_revision=report.b0.label_source_revision,
        b0_state=report.b0.state,
        report_status=report.status,
        b1_execution_source_revision=(
            identity.source.execution_source_revision
        ),
        b1_label_source_revision=identity.source.label_source_revision,
        trace_modes=tuple(mode.value for mode in TRACE_MODE_ORDER),
        cache_conditions=CACHE_CONDITION_ORDER,
        external_services_actually_called=(
            report.external_services_actually_called
        ),
    )
    manifest_json = _canonical_json(manifest.model_dump(mode="json"))
    manifest_sha256 = _sha256_bytes(manifest_json)
    checksums = (
        f"{manifest_sha256.removeprefix('sha256:')}  manifest.json\n"
        f"{report_sha256.removeprefix('sha256:')}  report.json\n"
    ).encode("ascii")
    return BaselineBundle(
        report_json=report_json,
        manifest_json=manifest_json,
        checksums=checksums,
        report_sha256=report_sha256,
        manifest_sha256=manifest_sha256,
    )


def write_baseline_bundle(directory: Path, bundle: BaselineBundle) -> None:
    """排他写出 B1 报告，不覆盖任何已有基线。

    Args:
        directory: 尚不存在的单次基线目录。
        bundle: :func:`create_baseline_bundle` 生成的稳定字节。

    Returns:
        无返回值。

    Raises:
        FileExistsError: 目标目录或任一目标文件已存在。

    """
    directory.mkdir(parents=True, exist_ok=False)
    for name, payload in (
        ("report.json", bundle.report_json),
        ("manifest.json", bundle.manifest_json),
        ("MANIFEST.sha256", bundle.checksums),
    ):
        with (directory / name).open("xb") as output:
            output.write(payload)


def validate_frozen_b0_manifest(value: Mapping[str, object]) -> B0Reference:
    """验证外部 B0 引用来自进入 V3-02 前的冻结提交。

    Args:
        value: 既有 B0 Manifest 的进入点、执行、标签和摘要字段。

    Returns:
        可写入 B1 报告的不可回算引用。

    Raises:
        ValueError: baseline ID、源码提交或 Manifest 摘要无效。

    """
    if value.get("baseline_id") != "B0":
        raise ValueError("B0 Manifest 的 baseline_id 必须为 B0。")
    entry_source_revision = value.get("entry_source_revision")
    execution_source_revision = value.get("execution_source_revision")
    label_source_revision = value.get("label_source_revision")
    manifest_sha256 = value.get("manifest_sha256")
    if not isinstance(entry_source_revision, str):
        raise ValueError("B0 Manifest 缺少 entry_source_revision。")
    if not isinstance(execution_source_revision, str):
        raise ValueError("B0 Manifest 缺少 execution_source_revision。")
    if not isinstance(label_source_revision, str):
        raise ValueError("B0 Manifest 缺少 label_source_revision。")
    if not isinstance(manifest_sha256, str):
        raise ValueError("B0 Manifest 缺少 manifest_sha256。")
    return B0Reference(
        entry_source_revision=entry_source_revision,
        execution_source_revision=execution_source_revision,
        label_source_revision=label_source_revision,
        manifest_sha256=manifest_sha256,
    )


def _validate_and_order_cases(
    cases: Sequence[FixedChunkCase],
) -> tuple[FixedChunkCase, ...]:
    ordered = tuple(sorted(cases, key=lambda item: item.case_id))
    if not ordered:
        raise ValueError("V3-02C 至少需要一个公开合成 Case。")
    identifiers = tuple(case.case_id for case in ordered)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("V3-02C Case ID 不能重复。")
    missing = REQUIRED_PUBLIC_SLICES - {case.slice_id for case in ordered}
    if missing:
        raise ValueError(f"V3-02C 缺少公开合成切片：{sorted(missing)}")
    return ordered


def _validate_case_chunks(
    cases: Sequence[FixedChunkCase], chunks: FixedChunkSet
) -> None:
    known = {chunk.chunk_id for chunk in chunks.chunks}
    for case in cases:
        unknown = set(case.relevant_chunk_ids) - known
        if unknown:
            raise ValueError(
                f"{case.case_id}: 标签引用了未知固定 Chunk：{sorted(unknown)}"
            )


def _execute_matrix(
    runtime: FixedChunkRuntime,
    cases: Sequence[FixedChunkCase],
    identity: FrozenBaselineIdentity,
) -> tuple[QueryMeasurement, ...]:
    results: list[QueryMeasurement] = []
    for mode in TRACE_MODE_ORDER:
        runtime.reset_query_cache(mode)
        for case in cases:
            for cache_condition in CACHE_CONDITION_ORDER:
                measurement = runtime.execute(case, mode, cache_condition)
                _validate_measurement(
                    measurement,
                    case=case,
                    mode=mode,
                    cache_condition=cache_condition,
                    identity=identity,
                )
                results.append(measurement)
    return tuple(results)


def _validate_measurement(
    measurement: QueryMeasurement,
    *,
    case: FixedChunkCase,
    mode: TraceMode,
    cache_condition: CacheCondition,
    identity: FrozenBaselineIdentity,
) -> None:
    expected_revisions = tuple(
        item.revision_id for item in identity.active_revisions
    )
    expected = {
        "case_id": case.case_id,
        "mode": mode,
        "cache_condition": cache_condition,
        "chunk_digest": identity.chunks.digest,
        "active_revision_ids": expected_revisions,
        "index_fingerprints": identity.profile.index_fingerprints,
        "serving_fingerprints": identity.profile.serving_fingerprints,
        "profile_sha256": identity.profile.profile_sha256,
    }
    observed = {
        "case_id": measurement.case_id,
        "mode": measurement.mode,
        "cache_condition": measurement.cache_condition,
        "chunk_digest": measurement.chunk_digest,
        "active_revision_ids": measurement.active_revision_ids,
        "index_fingerprints": measurement.index_fingerprints,
        "serving_fingerprints": measurement.serving_fingerprints,
        "profile_sha256": measurement.profile_sha256,
    }
    if observed != expected:
        raise ValueError(f"{case.case_id}: 查询期间固定身份发生漂移。")
    if measurement.external_service_call_count:
        raise ValueError("离线 B1 禁止调用外部服务。")
    if mode is TraceMode.SAFE and (
        measurement.trace.synchronous_artifact_read_count
        or measurement.trace.artifact_count
        or measurement.trace.artifact_bytes
    ):
        raise ValueError("SAFE Trace 禁止同步读取或写入大 Artifact。")


def _validate_cross_mode_boundaries(
    results: Sequence[QueryMeasurement],
) -> None:
    grouped: dict[tuple[str, CacheCondition], list[QueryMeasurement]] = (
        defaultdict(list)
    )
    for result in results:
        grouped[(result.case_id, result.cache_condition)].append(result)
    for key, measurements in grouped.items():
        if len(measurements) != len(TRACE_MODE_ORDER):
            raise ValueError(f"{key}: Trace 模式测量不完整。")
        quality = {_quality_signature(item) for item in measurements}
        if len(quality) != 1:
            raise ValueError(f"{key}: Trace 模式改变了质量结果。")
        provider_calls = {
            (item.provider_call_count, item.provider_retry_count)
            for item in measurements
        }
        if len(provider_calls) != 1:
            raise ValueError(f"{key}: Trace 模式增加或改变了 Provider 调用。")
    by_case_and_mode: dict[tuple[str, TraceMode], list[QueryMeasurement]] = (
        defaultdict(list)
    )
    for result in results:
        by_case_and_mode[(result.case_id, result.mode)].append(result)
    for cache_key, measurements in by_case_and_mode.items():
        if len(measurements) != len(CACHE_CONDITION_ORDER):
            raise ValueError(f"{cache_key}: cold/warm cache 测量不完整。")
        quality = {_cache_quality_signature(item) for item in measurements}
        if len(quality) != 1:
            raise ValueError(f"{cache_key}: cold/warm cache 改变了质量结果。")


def _quality_signature(measurement: QueryMeasurement) -> str:
    return canonical_sha256(
        {
            "channels": [
                item.model_dump(mode="json")
                for item in measurement.channel_candidates
            ],
            "evidence": measurement.evidence_chunk_ids,
            "cited": measurement.cited_chunk_ids,
            "answered": measurement.answered,
            "answer_correct": measurement.answer_correct,
            "refusal_reason": measurement.refusal_reason,
        }
    )


def _cache_quality_signature(measurement: QueryMeasurement) -> str:
    """返回不包含本次检索诊断的缓存前后质量签名。"""
    return canonical_sha256(
        {
            "evidence": measurement.evidence_chunk_ids,
            "cited": measurement.cited_chunk_ids,
            "answered": measurement.answered,
            "answer_correct": measurement.answer_correct,
            "refusal_reason": measurement.refusal_reason,
        }
    )


def _aggregate_metrics(
    cases: Sequence[FixedChunkCase],
    results: Sequence[QueryMeasurement],
) -> tuple[ModeMetrics, ...]:
    case_by_id = {case.case_id: case for case in cases}
    grouped: dict[tuple[TraceMode, CacheCondition], list[QueryMeasurement]] = (
        defaultdict(list)
    )
    for result in results:
        grouped[(result.mode, result.cache_condition)].append(result)
    safe_timings = {
        cache: _timing_summary(grouped[(TraceMode.SAFE, cache)])
        for cache in CACHE_CONDITION_ORDER
    }
    output: list[ModeMetrics] = []
    for mode in TRACE_MODE_ORDER:
        for cache in CACHE_CONDITION_ORDER:
            measurements = tuple(grouped[(mode, cache)])
            timings = _timing_summary(measurements)
            output.append(
                _mode_metrics(
                    mode,
                    cache,
                    measurements=measurements,
                    cases=case_by_id,
                    safe_timings=safe_timings[cache],
                    timings=timings,
                )
            )
    return tuple(output)


def _validate_upstream_gates(
    gates: Sequence[UpstreamGateEvidence],
) -> tuple[UpstreamGateEvidence, ...]:
    ordered = tuple(sorted(gates, key=lambda item: item.gate_id))
    identifiers = tuple(item.gate_id for item in ordered)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("上游门禁 gate_id 不能重复。")
    return ordered


def _evaluate_gates(
    metrics: Sequence[ModeMetrics],
    upstream: Sequence[UpstreamGateEvidence],
) -> tuple[GateOutcome, ...]:
    safe_cold = next(
        item
        for item in metrics
        if item.mode is TraceMode.SAFE and item.cache_condition == "cold"
    )
    specifications: tuple[
        tuple[str, JsonScalar, Literal["eq", "ge", "le"], JsonScalar, str],
        ...,
    ] = (
        (
            "evidence_recall_at_5",
            safe_cold.evidence_recall_at_5,
            "ge",
            0.9,
            "Evaluation V3 provisional recall gate",
        ),
        (
            "citation_accuracy",
            safe_cold.citation_accuracy,
            "eq",
            1.0,
            "固定 Chunk 引用必须全部正确",
        ),
        (
            "answer_accuracy",
            safe_cold.answer_accuracy,
            "eq",
            1.0,
            "公开合成可回答 Case 必须全部正确",
        ),
        (
            "erroneous_refusal_rate",
            safe_cold.erroneous_refusal_rate,
            "eq",
            0.0,
            "固定 Chunk 不允许错误拒答",
        ),
        (
            "erroneous_answer_rate",
            safe_cold.erroneous_answer_rate,
            "eq",
            0.0,
            "无答案 Case 不允许错误作答",
        ),
        (
            "trace_dropped_count",
            sum(item.trace_dropped_count for item in metrics),
            "eq",
            0,
            "三种 Trace 模式不允许静默丢弃记录",
        ),
    )
    outcomes = [
        GateOutcome(
            gate_id=gate_id,
            source="fixed_chunk_b1",
            passed=_compare(observed, operator, expected),
            observed=observed,
            operator=operator,
            expected=expected,
            reason=reason,
        )
        for gate_id, observed, operator, expected, reason in specifications
    ]
    outcomes.extend(
        GateOutcome(
            gate_id=item.gate_id,
            source="upstream",
            passed=item.status == "PASS",
            observed=item.status,
            operator="eq",
            expected="PASS",
            reason=item.reason,
        )
        for item in upstream
    )
    return tuple(outcomes)


def _compare(
    observed: JsonScalar,
    operator: Literal["eq", "ge", "le"],
    expected: JsonScalar,
) -> bool:
    if operator == "eq":
        return observed == expected
    if isinstance(observed, bool) or isinstance(expected, bool):
        raise TypeError("数值门禁不能使用布尔值。")
    if not isinstance(observed, (int, float)) or not isinstance(
        expected, (int, float)
    ):
        raise TypeError("ge/le 门禁必须使用数值。")
    if operator == "ge":
        return observed >= expected
    return observed <= expected


def _mode_metrics(  # noqa: PLR0913
    mode: TraceMode,
    cache: CacheCondition,
    *,
    measurements: Sequence[QueryMeasurement],
    cases: Mapping[str, FixedChunkCase],
    safe_timings: tuple[int, int, int, int],
    timings: tuple[int, int, int, int],
) -> ModeMetrics:
    answerable = [
        item for item in measurements if cases[item.case_id].answerable
    ]
    unanswerable = [
        item for item in measurements if not cases[item.case_id].answerable
    ]
    recalls = [_recall_at_5(item, cases[item.case_id]) for item in answerable]
    citations = [
        float(
            bool(item.cited_chunk_ids)
            and set(item.cited_chunk_ids)
            <= set(cases[item.case_id].relevant_chunk_ids)
        )
        for item in answerable
    ]
    answer_accuracy = [
        float(item.answer_correct is True) for item in answerable
    ]
    stage_values: dict[str, list[int]] = defaultdict(list)
    channel_counts: Counter[str] = Counter()
    for item in measurements:
        for stage in item.stage_durations:
            stage_values[stage.stage].append(stage.duration_ns)
        for channel in item.channel_candidates:
            channel_counts[channel.channel] += len(channel.chunk_ids)
    observed_usage = [
        item.observed_tokens
        for item in measurements
        if item.observed_tokens is not None
    ]
    observed_cost = [
        item.estimated_cost_microunits
        for item in measurements
        if item.estimated_cost_microunits is not None
    ]
    return ModeMetrics(
        mode=mode,
        cache_condition=cache,
        case_count=len(measurements),
        evidence_recall_at_5=_mean(recalls),
        citation_accuracy=_mean(citations),
        answer_accuracy=_mean(answer_accuracy),
        erroneous_refusal_rate=_ratio(
            sum(not item.answered for item in answerable), len(answerable)
        ),
        erroneous_answer_rate=_ratio(
            sum(item.answered for item in unanswerable), len(unanswerable)
        ),
        p50_total_duration_ns=timings[0],
        p95_total_duration_ns=timings[1],
        p50_non_provider_duration_ns=timings[2],
        p95_non_provider_duration_ns=timings[3],
        p50_trace_capture_overhead_ns=_percentile(
            [item.trace_capture_overhead_ns for item in measurements], 50
        ),
        p95_trace_capture_overhead_ns=_percentile(
            [item.trace_capture_overhead_ns for item in measurements], 95
        ),
        p50_added_vs_safe_ns=timings[0] - safe_timings[0],
        p95_added_vs_safe_ns=timings[1] - safe_timings[1],
        stage_duration_ns={
            name: {
                "p50": _percentile(values, 50),
                "p95": _percentile(values, 95),
            }
            for name, values in sorted(stage_values.items())
        },
        channel_candidate_count=dict(sorted(channel_counts.items())),
        provider_call_count=sum(
            item.provider_call_count for item in measurements
        ),
        provider_retry_count=sum(
            item.provider_retry_count for item in measurements
        ),
        observed_tokens=(sum(observed_usage) if observed_usage else None),
        unknown_usage_count=sum(
            item.observed_tokens is None and item.provider_call_count > 0
            for item in measurements
        ),
        estimated_cost_microunits=(
            sum(observed_cost) if observed_cost else None
        ),
        cache_hit_count=sum(item.cache_hit for item in measurements),
        singleflight_hit_count=sum(
            item.singleflight_hit for item in measurements
        ),
        trace_submitted_count=sum(
            item.trace.submitted_count for item in measurements
        ),
        trace_written_count=sum(
            item.trace.written_count for item in measurements
        ),
        trace_dropped_count=sum(
            item.trace.dropped_count for item in measurements
        ),
        trace_queue_high_water=max(
            item.trace.queue_high_water for item in measurements
        ),
        trace_storage_bytes=sum(
            item.trace.storage_bytes for item in measurements
        ),
        trace_artifact_count=sum(
            item.trace.artifact_count for item in measurements
        ),
        trace_artifact_bytes=sum(
            item.trace.artifact_bytes for item in measurements
        ),
    )


def _timing_summary(
    measurements: Sequence[QueryMeasurement],
) -> tuple[int, int, int, int]:
    total = [item.total_duration_ns for item in measurements]
    non_provider = [
        item.total_duration_ns - item.provider_duration_ns
        for item in measurements
    ]
    return (
        _percentile(total, 50),
        _percentile(total, 95),
        _percentile(non_provider, 50),
        _percentile(non_provider, 95),
    )


def _recall_at_5(measurement: QueryMeasurement, case: FixedChunkCase) -> float:
    """按最终返回 evidence 计算冷、暖缓存同义的 Recall@5。"""
    relevant = set(case.relevant_chunk_ids)
    return _ratio(
        len(set(measurement.evidence_chunk_ids[:5]) & relevant),
        len(relevant),
    )


def _case_manifest_sha256(cases: Sequence[FixedChunkCase]) -> str:
    return canonical_sha256(
        [
            {
                "case_id": case.case_id,
                "slice_id": case.slice_id,
                "query_sha256": case.query_sha256,
                "relevant_chunk_ids": case.relevant_chunk_ids,
                "answerable": case.answerable,
            }
            for case in cases
        ]
    )


def _percentile(values: Sequence[int], percentile: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def _mean(values: Sequence[float]) -> float:
    return 0.0 if not values else sum(values) / len(values)


def _ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _canonical_json(value: JsonValue) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


__all__ = [
    "B0_EXECUTION_REVISION",
    "B0_SOURCE_REVISION",
    "B1_LABEL_REVISION",
    "CACHE_CONDITION_ORDER",
    "REQUIRED_PUBLIC_SLICES",
    "TRACE_MODE_ORDER",
    "ActiveRevision",
    "B0Reference",
    "BaselineBundle",
    "BaselineManifest",
    "BaselineReport",
    "CacheCondition",
    "ChannelCandidates",
    "ComponentIdentity",
    "DeterministicProductBaselineRuntime",
    "EmbeddingIdentity",
    "FixedChunkCase",
    "FixedChunkRuntime",
    "FixedChunkSet",
    "FrozenBaselineIdentity",
    "FrozenBuild",
    "FrozenChunk",
    "FrozenDocument",
    "GateOutcome",
    "HardwareIdentity",
    "ModeMetrics",
    "ProcessIdentity",
    "ProductionRetrievalReceipt",
    "ProfileIdentity",
    "PublicSlice",
    "QueryMeasurement",
    "SourceIdentity",
    "StageDuration",
    "TraceConsumerContract",
    "TraceConsumerReceipt",
    "TraceMeasurement",
    "UpstreamGateEvidence",
    "create_baseline_bundle",
    "fixed_chunk_digest",
    "freeze_chunk_set",
    "run_b1_fixed_chunk_baseline",
    "run_concrete_fixed_chunk_baseline",
    "validate_frozen_b0_manifest",
    "write_baseline_bundle",
]


if __name__ == "__main__":
    raise SystemExit(main())
