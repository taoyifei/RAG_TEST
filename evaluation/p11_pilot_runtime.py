"""现有 release acceptance 的独立 pilot 阶段，预算不足时不触发建索引。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from time import monotonic, sleep

from evaluation.p11_pilot import PilotLiveEvidence, PilotReport, evaluate_pilot
from evaluation.p11_pilot_data import PilotDataset, load_pilot_dataset
from evaluation.v2.models import (
    CaseObservation,
    DatasetDocument,
    EvaluationCase,
)
from evaluation.v2.observations import ObservationContext, observe_case_result
from evaluation.v2.runtime import _effective_cases
from rag_app.adapters.providers.budget_ledger import ProviderBudgetLedger
from rag_app.composition.product_runtime import ProductRuntime
from rag_app.core.identifiers import (
    canonical_sha256,
    deterministic_id,
    document_version_id,
)
from rag_app.core.models import Job, RetrievalPolicy, SearchAnswerResult
from rag_app.core.models.chunk import Chunk
from rag_app.core.models.management import QueuedIngestion
from rag_app.core.tokenization import estimate_tokens
from rag_app.product.live_acceptance import AcceptanceState, StepResult
from rag_app.product.models import ImpactKind, RetrievalProfileDraft
from rag_app.product.quality import QualityValidationRecord
from rag_app.product.verification import profile_specs

SearchCallback = Callable[[str, str, str, str], SearchAnswerResult]
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@dataclass(frozen=True, slots=True)
class _PilotRuntime:
    runtime: ProductRuntime
    config: dict[str, object]
    state: AcceptanceState
    dataset: PilotDataset


@dataclass(frozen=True, slots=True)
class _PilotInventory:
    documents: tuple[DatasetDocument, ...]
    chunks: dict[str, Chunk]
    revisions: dict[str, str]


@dataclass(frozen=True, slots=True)
class _PilotJobScope:
    project_id: str
    knowledge_base_id: str
    profile_revision_id: str
    document_id: str
    document_version_id: str


def query_budget_lower_bound(
    dataset: PilotDataset, query_instruct: str
) -> dict[str, int]:
    """按已固定问题与实际 instruct 计算仅查询 embedding 的预算下限。

    Args:
        dataset: 两条路径共用的独立问题。
        query_instruct: 当前百炼 Query 策略的原值。

    Returns:
        请求与估算 Token 下限；文档、Reranker 和重试还需另计。

    """
    query_tokens = sum(estimate_tokens(case.query) for case in dataset.cases)
    return {
        "requests": len(dataset.cases) * 2,
        "estimated_input_tokens": query_tokens * 2
        + len(dataset.cases) * estimate_tokens(query_instruct),
    }


def run_pilot(
    runtime: ProductRuntime,
    config: dict[str, object],
    state: AcceptanceState,
    search_callback: SearchCallback,
    budget_summary: dict[str, object],
) -> StepResult:
    """在独立知识库完成预标注双路评估，缺少预算不消耗真实请求。

    Args:
        runtime: 当前候选与页面托管连接构造的 Runtime。
        config: 已授权 acceptance 配置，不包含 Key。
        state: 当前 campaign 的持久续跑资源。
        search_callback: 主/备路径的验收专用查询和局部故障注入。
        budget_summary: 当前持久账本的脱敏累计值。

    Returns:
        带真实 V3 观测、固定阈值或预算阻断原因的阶段结果。

    """
    dataset = load_pilot_dataset()
    source = runtime.control.get_profile(
        str(config["source_profile_revision_id"])
    )
    binding_identity = runtime.control.quality.binding_identity(
        source.profile_revision_id
    )
    specs = profile_specs(source, runtime.control.get_connection)
    query_instruct = str(dict(specs[1].query_policy)["query_instruct"])
    lower_bound = query_budget_lower_bound(dataset, query_instruct)
    available = {
        "requests": int(str(budget_summary["request_limit"]))
        - int(str(budget_summary["reserved"])),
        "estimated_input_tokens": int(
            str(budget_summary["estimated_token_limit"])
        )
        - int(str(budget_summary["estimated_input_tokens"])),
    }
    additional = {
        name: max(0, value - available[name])
        for name, value in lower_bound.items()
    }
    if any(additional.values()):
        return StepResult(
            "BLOCKED",
            "BLOCKED_BUDGET",
            {
                "pilot": True,
                "sample_count": len(dataset.cases),
                "dataset_sha256": dataset.dataset_sha256,
                "minimum_additional": additional,
                "query_only_lower_bound": lower_bound,
                "excluded_costs": [
                    "document_embedding",
                    "reranking",
                    "retries",
                ],
                "quality_ready": "BLOCKED_BUDGET",
            },
        )
    if runtime.providers.test_only_transport:
        return StepResult("NOT_RUN", "MOCK_TRANSPORT_IS_NOT_LIVE")
    context = _PilotRuntime(runtime, config, state, dataset)
    documents, profile_ids = _prepare_corpus(context)
    chunks, revisions = _active_inventory(runtime, documents)
    cases = _remap_cases(dataset, documents)
    cases = _effective_cases(cases, chunks, require_fixed_labels=False)
    if (
        runtime.control.quality.binding_identity(source.profile_revision_id)
        != binding_identity
    ):
        return StepResult("BLOCKED", "QUALITY_BINDING_CHANGED_BEFORE_QUERIES")
    ledger = ProviderBudgetLedger(Path(str(config["ledger_path"])))
    observations, attempts, source_ranges = _query_cases(
        context,
        cases,
        _PilotInventory(documents, chunks, revisions),
        search_callback,
        ledger,
    )
    if (
        runtime.control.quality.binding_identity(source.profile_revision_id)
        != binding_identity
    ):
        return StepResult(
            "BLOCKED",
            "QUALITY_BINDING_CHANGED_DURING_RUN",
            {
                "dataset_sha256": dataset.dataset_sha256,
                "source_ranges": source_ranges,
                "observations": {
                    lane: [item.model_dump(mode="json") for item in items]
                    for lane, items in observations.items()
                },
            },
        )
    report = evaluate_pilot(
        cases,
        observations,
        PilotLiveEvidence(
            validation_mode="live",
            profile_revision_id=source.profile_revision_id,
            binding_identity=binding_identity,
            campaign_id=state.campaign_id,
            dataset_sha256=dataset.dataset_sha256,
            case_attempts=attempts,
            provider_models=tuple(
                f"{spec.provider_id}:{spec.model}" for spec in specs
            ),
        ),
    )
    record_ids = _record_quality(context, report, profile_ids)
    evidence = report.model_dump(mode="json")
    evidence["observations"] = {
        lane: [item.model_dump(mode="json") for item in items]
        for lane, items in observations.items()
    }
    evidence["quality_record_ids"] = record_ids
    evidence["source_ranges"] = source_ranges
    return StepResult(report.status, report.reason, evidence)


def _pilot_prefix(context: _PilotRuntime) -> str:
    binding = context.runtime.control.quality.binding_identity(
        str(context.config["source_profile_revision_id"])
    )
    return "p11-pilot:" + canonical_sha256(
        {
            "campaign_id": context.state.campaign_id,
            "dataset_sha256": context.dataset.dataset_sha256,
            "source_binding": binding,
        }
    )


def _prepare_corpus(
    context: _PilotRuntime,
) -> tuple[tuple[DatasetDocument, ...], tuple[str, ...]]:
    runtime, state, dataset = context.runtime, context.state, context.dataset
    prefix = _pilot_prefix(context)
    project = runtime.sdk.create_project(
        "P11 独立质量 pilot", idempotency_key=prefix
    )
    scopes: dict[str, str] = {}
    scoped_profiles: dict[str, str] = {}
    profiles = []
    for logical_kb in sorted(
        {item.document.knowledge_base_id for item in dataset.documents}
    ):
        kb = runtime.sdk.create_knowledge_base(
            project.project_id,
            "P11 公开标签质量 " + logical_kb[-4:],
            description="独立公开合成 pilot，仅用于当前授权验收。",
            idempotency_key=prefix + logical_kb,
        )
        scopes[logical_kb] = kb.knowledge_base_id
        profile_id = _ensure_profile(context, kb.knowledge_base_id)
        scoped_profiles[logical_kb] = profile_id
        profiles.append(profile_id)
    documents = []
    for item in dataset.documents:
        logical = item.document
        kb_id = scopes[logical.knowledge_base_id]
        document_id: str | None = None
        for index, version in enumerate(logical.versions):
            key = prefix + version.fixture_id
            job_id = state.resource(key)
            if job_id is None:
                if document_id is None:
                    job = runtime.sdk.create_document(
                        project.project_id,
                        kb_id,
                        display_name=version.display_name,
                        content=item.content(index),
                        media_type=_MEDIA_TYPE,
                        idempotency_key=key,
                    )
                else:
                    job = runtime.sdk.create_document_version(
                        project.project_id,
                        kb_id,
                        document_id,
                        content=item.content(index),
                        media_type=_MEDIA_TYPE,
                        idempotency_key=key,
                    )
                job_id = job.job_id
                state.resource(key, job_id)
            expected_document_id = document_id or deterministic_id(
                "doc", project.project_id, kb_id, key
            )
            job = _wait_job(
                context,
                key,
                _PilotJobScope(
                    project.project_id,
                    kb_id,
                    scoped_profiles[logical.knowledge_base_id],
                    expected_document_id,
                    document_version_id(
                        expected_document_id,
                        hashlib.sha256(item.content(index)).hexdigest(),
                    ),
                ),
            )
            if job.document_id is None:
                raise ValueError("Pilot job 缺少实际文档身份。")
            document_id = job.document_id
        documents.append(
            logical.model_copy(
                update={
                    "project_id": project.project_id,
                    "knowledge_base_id": kb_id,
                    "document_id": document_id,
                }
            )
        )
    return tuple(documents), tuple(profiles)


def _ensure_profile(context: _PilotRuntime, kb_id: str) -> str:
    runtime, state = context.runtime, context.state
    source = runtime.control.get_profile(
        str(context.config["source_profile_revision_id"])
    )
    key = "pilot-profile:" + kb_id
    profile_id = state.resource(key)
    if profile_id is None:
        draft = {
            key: value
            for key, value in source.model_dump(mode="json").items()
            if key in RetrievalProfileDraft.model_fields
        }
        draft["knowledge_base_id"] = kb_id
        profile_id = runtime.control.create_profile(
            RetrievalProfileDraft.model_validate(draft)
        ).profile_revision_id
        state.resource(key, profile_id)
    profile = runtime.control.get_profile(profile_id)
    if (
        profile.knowledge_base_id != kb_id
        or profile.index_semantic_fingerprint
        != source.index_semantic_fingerprint
        or profile.serving_fingerprint != source.serving_fingerprint
    ):
        raise ValueError("PILOT_PROFILE_BINDING_MISMATCH")
    if profile.status == "draft":
        runtime.control.activate_profile(
            profile_id, confirmed_impact=ImpactKind.NEW_INDEX_REVISION_REQUIRED
        )
    return profile_id


def _wait_job(context: _PilotRuntime, key: str, scope: _PilotJobScope) -> Job:
    runtime = context.runtime
    job_id = context.state.resource(key)
    if job_id is None:
        raise ValueError("PILOT_JOB_REFERENCE_MISSING")
    _validate_pilot_job(context, job_id, scope)
    job = runtime.p09.store.resume_ingestion_job(
        job_id,
        project_id=scope.project_id,
        knowledge_base_id=scope.knowledge_base_id,
        idempotency_key=key,
    )
    if job.state.value == "queued":
        runtime.jobs.submit(job_id)
    deadline = monotonic() + 120
    while job.state.value in {"queued", "running"} and monotonic() < deadline:
        sleep(0.1)
        job = runtime.sdk.get_job(job_id)
    if job.state.value != "succeeded":
        raise ValueError("PILOT_INDEX_JOB_NOT_SUCCEEDED:" + job.state.value)
    return job


def _validate_pilot_job(
    context: _PilotRuntime, job_id: str, scope: _PilotJobScope
) -> None:
    runtime = context.runtime
    job = runtime.sdk.get_job(job_id)
    if (
        job.project_id,
        job.knowledge_base_id,
        job.document_id,
        job.document_version_id,
    ) != (
        scope.project_id,
        scope.knowledge_base_id,
        scope.document_id,
        scope.document_version_id,
    ):
        raise ValueError("PILOT_JOB_SCOPE_MISMATCH")
    with runtime.connections.transaction() as connection:
        row = connection.execute(
            "SELECT request_json FROM ingestion_requests WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise ValueError("PILOT_JOB_REQUEST_MISSING")
    request = QueuedIngestion.model_validate_json(str(row["request_json"]))
    approved = {
        hashlib.sha256(item.content(index)).hexdigest()
        for item in context.dataset.documents
        for index in range(len(item.document.versions))
    }
    if (
        request.job_id != job_id
        or request.revision_id != job.revision_id
        or request.target_document_id != scope.document_id
        or request.target_document_version_id != scope.document_version_id
        or request.retrieval_profile_revision_id != scope.profile_revision_id
        or request.activate_profile
        or not request.documents
        or any(
            item.document.project_id != scope.project_id
            or item.document.knowledge_base_id != scope.knowledge_base_id
            or item.content_sha256 not in approved
            or item.artifact_id != "sha256:" + item.content_sha256
            for item in request.documents
        )
    ):
        raise ValueError("PILOT_JOB_FROZEN_REQUEST_MISMATCH")


def _active_inventory(
    runtime: ProductRuntime, documents: tuple[DatasetDocument, ...]
) -> tuple[dict[str, Chunk], dict[str, str]]:
    chunks: dict[str, Chunk] = {}
    revisions: dict[str, str] = {}
    control = runtime.retrieval_runtime.persistence.control
    for document in documents:
        kb_id = document.knowledge_base_id
        if kb_id in revisions:
            continue
        revision = control.active_revision_id(kb_id)
        if revision is None:
            raise ValueError("Pilot 缺少 Active Revision。")
        inspection = runtime.sdk.inspect_revision(
            document.project_id, kb_id, revision
        )
        if not (
            inspection.active
            and inspection.actual_chunk_count > 0
            and inspection.fts_count == inspection.actual_chunk_count
            and inspection.expected_chunk_count == inspection.actual_chunk_count
            and {item.vector_name for item in inspection.slot_coverages}
            == {"dense_primary", "dense_standby"}
            and all(
                item.coverage_ratio == 1.0 and item.failed_count == 0
                for item in inspection.slot_coverages
            )
            and inspection.validation_evidence_hash is not None
        ):
            raise ValueError("PILOT_DUAL_SLOT_INVENTORY_INCOMPLETE")
        revisions[kb_id] = revision
        chunks.update(
            {chunk.chunk_id: chunk for chunk in control.chunk_rows(revision)}
        )
    return chunks, revisions


def _remap_cases(
    dataset: PilotDataset, documents: tuple[DatasetDocument, ...]
) -> tuple[EvaluationCase, ...]:
    pairs = tuple(zip(dataset.manifest.documents, documents, strict=True))
    document_ids = {old.document_id: new.document_id for old, new in pairs}
    scopes = {old.knowledge_base_id: new for old, new in pairs}
    cases = []
    for case in dataset.cases:
        scope = scopes[case.knowledge_base_id]
        expected = case.expected.model_copy(
            update={
                "relevant_document_ids": tuple(
                    document_ids[item]
                    for item in case.expected.relevant_document_ids
                ),
                "required_source_ranges": tuple(
                    item.model_copy(
                        update={"document_id": document_ids[item.document_id]}
                    )
                    for item in case.expected.required_source_ranges
                ),
            }
        )
        constraints = case.constraints.model_copy(
            update={
                "forbidden_document_ids": tuple(
                    document_ids[item]
                    for item in case.constraints.forbidden_document_ids
                ),
            }
        )
        cases.append(
            case.model_copy(
                update={
                    "project_id": scope.project_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "expected": expected,
                    "constraints": constraints,
                }
            )
        )
    return tuple(cases)


def _query_cases(
    context: _PilotRuntime,
    cases: tuple[EvaluationCase, ...],
    inventory: _PilotInventory,
    callback: SearchCallback,
    ledger: ProviderBudgetLedger,
) -> tuple[
    dict[str, tuple[CaseObservation, ...]],
    dict[str, tuple[str, ...]],
    dict[str, object],
]:
    observations: dict[str, tuple[CaseObservation, ...]] = {}
    attempts: dict[str, tuple[str, ...]] = {}
    source_ranges: dict[str, object] = {}
    for lane in ("primary", "standby"):
        items = []
        for case in cases:
            before = {
                row["attempt_id"]
                for row in ledger.attempts(context.state.campaign_id)
            }
            started = monotonic()
            result = callback(
                case.project_id, case.knowledge_base_id, case.query, lane
            )
            elapsed = (monotonic() - started) * 1000.0
            source_ranges[f"{lane}:{case.case_id}"] = {
                "expected": [
                    item.model_dump(mode="json", exclude={"exact_text"})
                    for item in case.expected.required_source_ranges
                ],
                "observed": [
                    {
                        "document_id": item.document_id,
                        "chunk_id": item.chunk_id,
                        "node_id": span.node_id,
                        "source_start_char": span.source_start_char,
                        "source_end_char": span.source_end_char,
                    }
                    for item in result.evidence
                    for span in item.source_spans
                ],
            }
            current = ledger.attempts(context.state.campaign_id)
            attempts[f"{lane}:{case.case_id}"] = tuple(
                str(row["attempt_id"])
                for row in current
                if row["attempt_id"] not in before
                and row["step_id"] == "citation_quality"
                and row["forwarded"]
                and row["http_status"] == HTTPStatus.OK
                and row["operation"] == "embedding.query"
            )
            profile = context.runtime.control.active_profile(
                case.knowledge_base_id
            )
            if profile is None:
                raise ValueError("Pilot 缺少实际 Profile。")
            items.append(
                observe_case_result(
                    case,
                    result,
                    ObservationContext(
                        chunks=inventory.chunks,
                        documents=inventory.documents,
                        expected_revision=inventory.revisions[
                            case.knowledge_base_id
                        ],
                        expected_vectors=(
                            ("primary", "dense_primary"),
                            ("standby", "dense_standby"),
                        ),
                        variant_id="p11-fixed-profile",
                        lane=f"live-{lane}",
                        evidence_token_budget=RetrievalPolicy.model_validate(
                            dict(profile.retrieval_policy)
                        ).evidence_token_budget,
                    ),
                    latency_ms=elapsed,
                )
            )
        observations[lane] = tuple(items)
    return observations, attempts, source_ranges


def _record_quality(
    context: _PilotRuntime, report: PilotReport, profile_ids: tuple[str, ...]
) -> list[str]:
    if report.status not in {"PASS", "FAIL"}:
        return []
    control = context.runtime.control
    source_id = str(context.config["source_profile_revision_id"])
    source = control.get_profile(source_id)
    record_ids = []
    for profile_id in (source_id, *profile_ids):
        profile = control.get_profile(profile_id)
        if (
            profile.index_semantic_fingerprint,
            profile.serving_fingerprint,
        ) != (source.index_semantic_fingerprint, source.serving_fingerprint):
            raise ValueError(
                "Pilot 实际方案与源方案指纹不同，不能迁移质量记录。"
            )
        gates = {
            f"{lane}:{item.gate_id}": item.passed
            for lane, gate in report.gates.items()
            for item in gate.outcomes
        }
        gates.update(
            dict.fromkeys(
                (
                    "independent_labels",
                    "source_precision",
                    "recall",
                    "negative_leakage",
                ),
                report.status == "PASS",
            )
        )
        records = tuple(report.metrics.values())
        record_ids.append(
            control.quality.record(
                QualityValidationRecord(
                    profile_revision_id=profile_id,
                    kind="retrieval_quality_verified",
                    validation_mode="live",
                    run_id="p11-pilot:" + context.state.campaign_id,
                    dataset_sha256=context.dataset.dataset_sha256.removeprefix(
                        "sha256:"
                    ),
                    artifact_sha256=canonical_sha256(
                        report.model_dump(mode="json")
                    ).removeprefix("sha256:"),
                    index_fingerprint=profile.index_semantic_fingerprint,
                    serving_fingerprint=profile.serving_fingerprint,
                    gates=gates,
                    independent_holdout=True,
                    labeled_queries=report.positive_samples,
                    negative_queries=report.negative_samples,
                    citation_source_precision=min(
                        float(
                            item.metrics["citation_source_precision"].value or 0
                        )
                        for item in records
                    ),
                    recall=min(
                        float(item.metrics["recall_at_5"].value or 0)
                        for item in records
                    ),
                    negative_leakage=max(
                        float(item.metrics["negative_leakage_at_10"].value or 0)
                        for item in records
                    ),
                )
            )
        )
    return record_ids
