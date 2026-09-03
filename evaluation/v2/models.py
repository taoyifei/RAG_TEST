"""P08 数据集、观测、指标和不可变 Run 的严格模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

_SHA256 = r"^sha256:[0-9a-f]{64}$"


class P08Model(BaseModel):
    """拒绝未知字段并冻结实例的 P08 模型基类。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


class SourceRangeExpectation(P08Model):
    """可由 canonical citation 文本和 SourceSpan 核验的来源期望。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    exact_text: str = Field(min_length=1, max_length=4000, repr=False)
    occurrence: StrictInt | None = Field(default=None, gt=0)
    structural_anchor: tuple[str, ...] | None = None
    node_kind: str | None = None
    node_id: str | None = Field(
        default=None, pattern=r"^node_[0-9a-f]{32}$"
    )
    source_start_char: StrictInt | None = Field(default=None, ge=0)
    source_end_char: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_resolved_range(self) -> Self:
        resolved = (
            self.node_id,
            self.source_start_char,
            self.source_end_char,
        )
        if any(value is not None for value in resolved) and not all(
            value is not None for value in resolved
        ):
            raise ValueError("Resolved source range 必须完整。")
        if (
            self.source_start_char is not None
            and self.source_end_char is not None
            and self.source_end_char <= self.source_start_char
        ):
            raise ValueError("Resolved source range 必须非空且前进。")
        return self


class ExpectedResult(P08Model):
    """一条 Case 的逻辑身份、来源和拒答标签。"""

    relevant_document_ids: tuple[str, ...] = ()
    relevant_chunk_ids: tuple[str, ...] = ()
    required_identifiers: tuple[str, ...] = ()
    required_source_ranges: tuple[SourceRangeExpectation, ...] = ()
    answerable: bool
    expected_refusal_reason: str | None = None

    @model_validator(mode="after")
    def _validate_answerability(self) -> Self:
        if self.answerable:
            if not self.relevant_document_ids or not self.relevant_chunk_ids:
                raise ValueError(
                    "可回答 Case 必须绑定 document_id 和 chunk_id。"
                )
            if not self.required_source_ranges:
                raise ValueError("可回答 Case 必须绑定可验证来源文本。")
            if self.expected_refusal_reason is not None:
                raise ValueError("可回答 Case 禁止设置拒答原因。")
        elif self.expected_refusal_reason is None:
            raise ValueError("不可回答 Case 必须设置预期拒答原因。")
        return self

    @field_validator("relevant_document_ids")
    @classmethod
    def _validate_document_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("relevant document IDs 禁止重复。")
        return value

    @field_validator("relevant_chunk_ids")
    @classmethod
    def _validate_chunk_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("relevant chunk IDs 禁止重复。")
        return value


class CaseConstraints(P08Model):
    """结构、通道和负例约束。"""

    required_channels: tuple[str, ...] = ()
    forbidden_document_ids: tuple[str, ...] = ()
    must_preserve_table_column: StrictInt | None = Field(
        default=None,
        ge=0,
    )
    required_embedding_marker: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )

    @model_validator(mode="after")
    def _validate_table_marker(self) -> Self:
        if (
            self.must_preserve_table_column is not None
            and self.required_embedding_marker is None
        ):
            raise ValueError("表格列约束必须绑定 embedding marker。")
        return self


class EvaluationCase(P08Model):
    """严格版本化且不使用显示名作身份的评测 Case。"""

    schema_version: Literal["2", "3"]
    case_id: str = Field(pattern=r"^eval_[a-z0-9_]{3,80}$")
    split: Literal["tuning", "holdout"]
    group_id: str = Field(pattern=r"^grp_[a-z0-9_]{3,80}$")
    category: str = Field(pattern=r"^[a-z][a-z0-9_]{2,80}$")
    difficulty: Literal["basic", "intermediate", "hard"]
    failure_severity: Literal["low", "medium", "high", "critical"]
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    query: str = Field(min_length=1, max_length=8000, repr=False)
    expected: ExpectedResult
    constraints: CaseConstraints = CaseConstraints()


class FixtureVersion(P08Model):
    """一个逻辑文档的显示名和合成字节版本。"""

    fixture_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,80}$")
    display_name: str = Field(min_length=1, max_length=255)


class DatasetDocument(P08Model):
    """数据集中的全局逻辑文档与版本序列。"""

    document_id: str = Field(pattern=r"^doc_[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^prj_[0-9a-f]{32}$")
    knowledge_base_id: str = Field(pattern=r"^kb_[0-9a-f]{32}$")
    family_group_id: str = Field(pattern=r"^grp_[a-z0-9_]{3,80}$")
    coverage_tags: tuple[str, ...] = Field(min_length=1)
    versions: tuple[FixtureVersion, ...] = Field(min_length=1)


class DatasetManifest(P08Model):
    """Group Split 和合成文档目录的版本化 Manifest。"""

    schema_version: Literal["2", "3"]
    dataset_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,80}$")
    dataset_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    description: str = Field(min_length=1, max_length=1000)
    split_algorithm: str = Field(min_length=1, max_length=500)
    content_classification: Literal["synthetic_public"]
    cases_file: Literal["cases.jsonl"] = "cases.jsonl"
    documents: tuple[DatasetDocument, ...] = Field(min_length=1)


class CaseObservation(P08Model):
    """不含 query 或正文的单 Case 实际运行观测。"""

    case_id: str
    split: Literal["tuning", "holdout"]
    group_id: str
    category: str
    failure_severity: str
    variant_id: str
    lane: str
    status: str
    reason_code: str
    active_index_revision_id: str
    index_fingerprint: str = Field(pattern=_SHA256)
    serving_fingerprint: str = Field(pattern=_SHA256)
    selected_embedding_slot: str | None = None
    selected_vector_name: str | None = None
    route_reason_code: str
    rerank_mode: str
    channel_chunk_ids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    fused_chunk_ids: tuple[str, ...] = ()
    reranked_chunk_ids: tuple[str, ...] = ()
    expanded_chunk_ids: tuple[str, ...] = ()
    evidence_document_ids: tuple[str, ...] = ()
    evidence_chunk_ids: tuple[str, ...] = ()
    retrieved_document_ids: tuple[str, ...] = ()
    retrieved_chunk_ids: tuple[str, ...] = ()
    retrieval_origins: tuple[tuple[str, ...], ...] = ()
    cited_document_ids: tuple[str, ...] = ()
    cited_chunk_ids: tuple[str, ...] = ()
    matched_source_range_count: StrictInt = Field(ge=0)
    required_source_range_count: StrictInt = Field(ge=0)
    predicted_source_range_count: StrictInt = Field(default=0, ge=0)
    relevant_predicted_source_range_count: StrictInt = Field(default=0, ge=0)
    citation_present: bool
    citation_valid: bool
    quote_publishable: bool
    unsupported_claim_count: StrictInt = Field(ge=0)
    evidence_budget_overflow_count: StrictInt = Field(ge=0)
    wrong_scope_hit_count: StrictInt = Field(ge=0)
    wrong_revision_hit_count: StrictInt = Field(ge=0)
    wrong_vector_space_attempt_count: StrictInt = Field(ge=0)
    latency_ms: StrictFloat = Field(ge=0.0)
    provider_call_count: StrictInt = Field(ge=0)
    provider_retry_count: StrictInt = Field(ge=0)
    embedding_call_count: StrictInt = Field(default=0, ge=0)
    embedding_retry_count: StrictInt = Field(default=0, ge=0)
    reranker_call_count: StrictInt = Field(default=0, ge=0)
    reranker_retry_count: StrictInt = Field(default=0, ge=0)
    stage_elapsed_ms: tuple[tuple[str, StrictFloat], ...] = ()
    evidence_count: StrictInt = Field(default=0, ge=0)
    evidence_tokens: StrictInt = Field(default=0, ge=0)
    cache_hit: bool = False
    degraded_reason_codes: tuple[str, ...] = ()


class MetricValue(P08Model):
    """带样本量和可选 Bootstrap 区间的指标值。"""

    value: StrictFloat | StrictInt | None
    sample_count: StrictInt = Field(ge=0)
    status: Literal["ok", "insufficient_sample", "not_executed"]
    ci95: tuple[StrictFloat, StrictFloat] | None = None


class MetricReport(P08Model):
    """总体、分类和工程统计的机器可读报告。"""

    schema_version: Literal["1"] = "1"
    lane: str
    variant_id: str
    split: str
    metrics: dict[str, MetricValue]
    categories: dict[str, dict[str, MetricValue]]
    engineering: dict[str, JsonValue]


class GateOutcome(P08Model):
    """单个带来源门槛的判定。"""

    gate_id: str
    metric: str
    source: str
    passed: bool
    observed: StrictFloat | StrictInt | bool | None
    expected: JsonValue
    reason: str


class GateReport(P08Model):
    """P08 回归和安全门禁汇总。"""

    schema_version: Literal["1"] = "1"
    passed: bool
    outcomes: tuple[GateOutcome, ...]


class ErrorRecord(P08Model):
    """不泄漏正文的单 Case 失败根因记录。"""

    case_id: str
    variant_id: str
    category: str
    lane: str
    selected_slot: str | None
    channel_ranks: tuple[tuple[str, StrictInt], ...]
    rerank_mode: str
    expected_document_ids: tuple[str, ...]
    observed_document_ids: tuple[str, ...]
    expected_chunk_ids: tuple[str, ...]
    observed_chunk_ids: tuple[str, ...]
    failure_stage: str
    root_cause_bucket: Literal[
        "Parser",
        "Chunker",
        "Table structure",
        "Identity/version",
        "Exact tokenizer",
        "FTS tokenizer",
        "Embedding primary",
        "Embedding standby",
        "RRF",
        "Reranker",
        "Neighbor/evidence",
        "Confidence/refusal",
        "Dataset label",
        "Infrastructure",
    ]
    safe_evidence_hashes: tuple[str, ...]
    recommended_action: str


class ComponentIdentity(P08Model):
    """Manifest 使用的不含 Secret 的组件身份。"""

    component_id: str
    version: str
    fingerprint: str | None = None


class ProviderRunIdentity(P08Model):
    """一次 Run 实际可用的 Provider slot 身份。"""

    provider: str
    model: str
    slot: str
    vector_name: str
    request_policy_identity: str
    adapter_revision: str


class ResultFile(P08Model):
    """Run 输出文件的相对路径与摘要。"""

    relative_path: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
    sha256: str = Field(pattern=_SHA256)


class RunManifest(P08Model):
    """绑定全部结果影响因素且禁止正文和 Secret 的 Run Manifest。"""

    schema_version: Literal["1"] = "1"
    run_id: str = Field(pattern=r"^p08-[0-9TZ-]+-[0-9a-f]{8}$")
    state: Literal["complete", "failed"]
    started_at: datetime
    finished_at: datetime
    integration_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    index_revision_ids: tuple[str, ...]
    index_fingerprints: tuple[str, ...]
    serving_fingerprints: tuple[str, ...]
    profile_id: str
    providers: tuple[ProviderRunIdentity, ...]
    parser: ComponentIdentity
    chunker: ComponentIdentity
    tokenizer_identities: tuple[str, ...]
    parameters: dict[str, JsonValue]
    dataset_id: str
    dataset_sha256: str = Field(pattern=_SHA256)
    case_ids: tuple[str, ...]
    lane: str
    seed: StrictInt
    network_authorization_mode: str
    external_services_actually_called: tuple[str, ...]
    package_versions: dict[str, str]
    result_files: tuple[ResultFile, ...]
    selected_candidate: str
    holdout_access_count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _validate_times_and_network(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("Run finished_at 不能早于 started_at。")
        if self.lane == "offline-structural":
            if self.network_authorization_mode != "offline":
                raise ValueError("Offline Run 必须声明 offline 网络模式。")
            if self.external_services_actually_called:
                raise ValueError("Offline Run 禁止记录外部服务调用。")
        return self


__all__ = [
    "CaseConstraints",
    "CaseObservation",
    "ComponentIdentity",
    "DatasetDocument",
    "DatasetManifest",
    "ErrorRecord",
    "EvaluationCase",
    "ExpectedResult",
    "FixtureVersion",
    "GateOutcome",
    "GateReport",
    "MetricReport",
    "MetricValue",
    "ProviderRunIdentity",
    "ResultFile",
    "RunManifest",
    "SourceRangeExpectation",
]
