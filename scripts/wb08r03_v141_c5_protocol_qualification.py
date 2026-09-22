"""按编号阶段执行 C5 原协议诊断或真实字段协议资格探针。

脚本只使用合成公开数据，或显式传入的私有 ``RECONSTRUCTED_REQUEST``
fixture。两种阶段必须分开调用，绝不在一次业务请求内轮询 schema 或重试。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx

from rag_app.adapters.providers.aliyun_chat import ChatMessage
from rag_app.adapters.providers.field_resolution_wire import (
    FieldResolutionContract,
    FieldResolutionContractContext,
    build_field_resolution_contract,
    render_wire_schema,
    request_payload,
)
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
    openai_compatible_chat_payload,
)
from rag_app.adapters.providers.private_http_diagnostics import (
    PrivateProviderDiagnosticRecorder,
)
from rag_app.adapters.providers.structured_contract import (
    StructuredOutputCapabilityProfile,
    StructuredSchemaFamilyQualification,
    wb08r_structured_output_profile,
)
from rag_app.application.answering.field_resolution import (
    default_field_resolutions,
    merge_field_resolutions,
)
from rag_app.application.retrieval.adaptive import (
    FieldResolutionExecutionState,
    FieldResolutionOutcome,
)
from rag_app.application.retrieval.source_scope import (
    resolve_query_source_context,
)
from rag_app.core.errors import (
    ProviderInputTooLarge,
    ProviderInvalidResponse,
    ProviderRequestRejected,
    RagError,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    FieldCandidate,
    FieldResolutionStatus,
    KnowledgeBaseScope,
    ResolvedQueryView,
    SearchRequest,
)
from rag_app.core.models.query_plan import (
    AtomAnswerShape,
    QueryAtom,
    QueryPlan,
    make_query_plan,
)
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.product.model_settings import KnowledgeBaseModelSettings
from rag_app.wanshitong.internal_model_settings import (
    InternalCredentialSettings,
    InternalModelSettings,
)

_ACK = "wb08r03-v141-c5-real-probe-v1"
_ORIGINAL_OUTPUT_TOKENS = 160
_ORIGINAL_SCHEMA_REVISION = "wb08r-field-resolution-v1"
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    """一个不含真实文档内容的资格候选。"""

    identity: str
    field_label: str
    value_preview: str


@dataclass(frozen=True, slots=True)
class AtomSpec:
    """一个资格 Atom、候选真值和预期模型状态。"""

    atom_id: str
    target: str
    relation: str
    fragment: str
    candidates: tuple[CandidateSpec, ...]
    expected_status: FieldResolutionStatus
    expected_candidate_identities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QualificationCase:
    """一次独立真实请求的完整公开合成真值。"""

    case_id: str
    question: str
    atoms: tuple[AtomSpec, ...]


@dataclass(frozen=True, slots=True)
class PreparedCase:
    """已经绑定真实领域类型、合同和动态 ID 的资格请求。"""

    case: QualificationCase
    request: SearchRequest
    query_view: ResolvedQueryView
    query_plan: QueryPlan
    atoms: tuple[QueryAtom, ...]
    candidates: tuple[FieldCandidate, ...]
    identities_by_candidate_id: dict[str, str]
    contract: FieldResolutionContract


@dataclass(frozen=True, slots=True)
class QualifiedProbe:
    """一个实际逐 Atom HTTP 小包及其结果。"""

    atom_id: str
    contract: FieldResolutionContract
    schema: dict[str, object]
    outcome: FieldResolutionOutcome


def _candidate(
    identity: str, field_label: str, value_preview: str
) -> CandidateSpec:
    return CandidateSpec(identity, field_label, value_preview)


def _qualification_cases() -> tuple[QualificationCase, ...]:
    """返回 D3 固定五类公开合成请求，最后一类覆盖最大形状。"""
    paraphrase = QualificationCase(
        case_id="PARAPHRASE",
        question="合成协议检查：请找提交资料对应的字段。",
        atoms=(
            AtomSpec(
                atom_id="A1",
                target="合成任务",
                relation="提交资料",
                fragment="请找提交资料对应的字段",
                candidates=(
                    _candidate("department", "办理部门", "合成部门"),
                    _candidate("input_materials", "需提交资料", "合成资料"),
                ),
                expected_status=FieldResolutionStatus.SUPPORTED_PARAPHRASE,
                expected_candidate_identities=("input_materials",),
            ),
        ),
    )
    ambiguous = QualificationCase(
        case_id="AMBIGUOUS",
        question="合成协议检查：请找办理时间对应的字段。",
        atoms=(
            AtomSpec(
                atom_id="A1",
                target="合成任务",
                relation="办理时间",
                fragment="请找办理时间对应的字段",
                candidates=(
                    _candidate("start_time", "开始时间", "09:00"),
                    _candidate("end_time", "结束时间", "17:00"),
                ),
                expected_status=FieldResolutionStatus.AMBIGUOUS,
                expected_candidate_identities=("start_time", "end_time"),
            ),
        ),
    )
    not_found = QualificationCase(
        case_id="NOT_FOUND",
        question="合成协议检查：请找联系电话对应的字段。",
        atoms=(
            AtomSpec(
                atom_id="A1",
                target="合成任务",
                relation="联系电话",
                fragment="请找联系电话对应的字段",
                candidates=(
                    _candidate("department", "办理部门", "合成部门"),
                    _candidate("materials", "材料名称", "合成材料"),
                ),
                expected_status=FieldResolutionStatus.NOT_FOUND,
                expected_candidate_identities=(),
            ),
        ),
    )
    two_atoms = QualificationCase(
        case_id="TWO_ATOMS",
        question="甲任务需要什么输入，乙任务产生什么输出？",
        atoms=(
            AtomSpec(
                atom_id="A1",
                target="甲任务",
                relation="输入",
                fragment="甲任务需要什么输入",
                candidates=(
                    _candidate("a_input", "输入", "甲合成输入"),
                    _candidate("a_output", "输出", "甲合成输出"),
                ),
                expected_status=FieldResolutionStatus.EXACT,
                expected_candidate_identities=("a_input",),
            ),
            AtomSpec(
                atom_id="A2",
                target="乙任务",
                relation="输出",
                fragment="乙任务产生什么输出",
                candidates=(
                    _candidate("b_input", "输入", "乙合成输入"),
                    _candidate("b_output", "输出", "乙合成输出"),
                ),
                expected_status=FieldResolutionStatus.EXACT,
                expected_candidate_identities=("b_output",),
            ),
        ),
    )
    max_atoms: list[AtomSpec] = []
    fragments = (
        ("A1", "甲任务", "要交哪些材料", "甲任务要交哪些材料", "申报材料"),
        ("A2", "乙任务", "最后产出什么", "乙任务最后产出什么", "成果物"),
        ("A3", "丙任务", "由谁牵头", "丙任务由谁牵头", "责任人"),
        ("A4", "丁任务", "多久办结", "丁任务一般多久办结", "办理时限"),
    )
    for atom_index, (
        atom_id,
        target,
        relation,
        fragment,
        expected_field,
    ) in enumerate(fragments, start=1):
        expected_identity = f"a{atom_index}_expected"
        candidates = (
            _candidate(
                expected_identity,
                expected_field,
                f"{target}合成真值",
            ),
            *tuple(
                _candidate(
                    f"a{atom_index}_distractor_{index:02d}",
                    f"无关字段{index:02d}",
                    f"{target}合成干扰值{index:02d}",
                )
                for index in range(1, 16)
            ),
        )
        max_atoms.append(
            AtomSpec(
                atom_id=atom_id,
                target=target,
                relation=relation,
                fragment=fragment,
                candidates=candidates,
                expected_status=(FieldResolutionStatus.SUPPORTED_PARAPHRASE),
                expected_candidate_identities=(expected_identity,),
            )
        )
    maximum = QualificationCase(
        case_id="MAX_SHAPE",
        question=(
            "甲任务要交哪些材料，乙任务最后产出什么，"
            "丙任务由谁牵头，丁任务一般多久办结？"
        ),
        atoms=tuple(max_atoms),
    )
    return paraphrase, ambiguous, not_found, two_atoms, maximum


def _prepare_case(case: QualificationCase) -> PreparedCase:
    query_view = resolve_query_source_context(
        case.question,
        (),
        registry_revision="wb08r-c5-protocol-qualification-v1",
    ).query_view
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "wb08r-c5-qualification"),
        knowledge_base_id=deterministic_id("kb", "wb08r-c5-qualification"),
    )
    request = SearchRequest(scope=scope, text=query_view.business_query)
    atoms = tuple(
        QueryAtom(
            atom_id=atom.atom_id,
            target=atom.target,
            relation=atom.relation,
            answer_shape=AtomAnswerShape.FACT,
            original_fragment=atom.fragment,
        )
        for atom in case.atoms
    )
    query_plan = make_query_plan(
        standalone_query=query_view.business_query,
        original_query=query_view.original_query,
        intent="FACT",
        effort="DIRECT",
        atoms=atoms,
        reason_code="C5_PROTOCOL_QUALIFICATION",
        planner_called=False,
    )
    candidates: list[FieldCandidate] = []
    identities: dict[str, str] = {}
    next_candidate_id = 1
    for atom_index, atom in enumerate(case.atoms, start=1):
        for column_index, candidate in enumerate(atom.candidates, start=1):
            candidate_id = f"F{next_candidate_id}"
            next_candidate_id += 1
            identities[candidate_id] = candidate.identity
            candidates.append(
                FieldCandidate(
                    candidate_id=candidate_id,
                    atom_id=atom.atom_id,
                    fact_id=canonical_sha256(
                        (case.case_id, atom.atom_id, candidate.identity)
                    ),
                    document_id=deterministic_id(
                        "doc", "wb08r-c5-qualification", case.case_id
                    ),
                    document_version_id=deterministic_id(
                        "dver", "wb08r-c5-qualification", case.case_id
                    ),
                    table_key=canonical_sha256(
                        ("wb08r-c5-qualification", case.case_id)
                    ),
                    row_index=atom_index,
                    value_column_index=column_index,
                    target_label=atom.target,
                    field_label=candidate.field_label,
                    value_preview=candidate.value_preview,
                    dependency_support_ids=(f"S{next_candidate_id - 1}",),
                )
            )
    candidate_tuple = tuple(candidates)
    contract = build_field_resolution_contract(
        FieldResolutionContractContext(
            query_view=query_view,
            atoms=atoms,
            candidates=candidate_tuple,
        )
    )
    return PreparedCase(
        case=case,
        request=request,
        query_view=query_view,
        query_plan=query_plan,
        atoms=atoms,
        candidates=candidate_tuple,
        identities_by_candidate_id=identities,
        contract=contract,
    )


def _fixture_case(path: Path) -> QualificationCase:
    """读取所有者私有的历史 C5 请求重建输入。"""
    resolved = _private_input(path)
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("label") != "RECONSTRUCTED_REQUEST":
        raise ValueError("RECONSTRUCTED_REQUEST_LABEL_REQUIRED")
    atoms: list[AtomSpec] = []
    raw_atoms = raw.get("atoms")
    if not isinstance(raw_atoms, list) or not raw_atoms:
        raise ValueError("RECONSTRUCTED_REQUEST_ATOMS_INVALID")
    for item in raw_atoms:
        if not isinstance(item, dict):
            raise ValueError("RECONSTRUCTED_REQUEST_ATOMS_INVALID")
        raw_candidates = item.get("candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise ValueError("RECONSTRUCTED_REQUEST_CANDIDATES_INVALID")
        candidates = tuple(
            CandidateSpec(
                identity=str(candidate["identity"]),
                field_label=str(candidate["field_label"]),
                value_preview=str(candidate["value_preview"]),
            )
            for candidate in raw_candidates
            if isinstance(candidate, dict)
        )
        if len(candidates) != len(raw_candidates):
            raise ValueError("RECONSTRUCTED_REQUEST_CANDIDATES_INVALID")
        atoms.append(
            AtomSpec(
                atom_id=str(item["atom_id"]),
                target=str(item["target"]),
                relation=str(item["relation"]),
                fragment=str(item["fragment"]),
                candidates=candidates,
                # 原始诊断只观察协议，不预设模型语义结论。
                expected_status=FieldResolutionStatus.AMBIGUOUS,
                expected_candidate_identities=(),
            )
        )
    question = raw.get("question")
    if not isinstance(question, str) or not question:
        raise ValueError("RECONSTRUCTED_REQUEST_QUESTION_INVALID")
    return QualificationCase("RECONSTRUCTED_C5", question, tuple(atoms))


def _profile(
    settings: InternalModelSettings,
    *,
    allow_unique_items: bool,
    output_tokens: int,
    diagnostic_phase: Literal[
        "reconstructed-original", "reconstructed-transferred"
    ]
    | None,
) -> StructuredOutputCapabilityProfile:
    required = (
        settings.llm_structured_profile_revision,
        settings.llm_structured_service_identity_sha256,
        settings.llm_structured_chat_template_revision,
        settings.llm_structured_grammar_backend,
        settings.llm_structured_qualification_evidence_sha256,
    )
    if not all(required) or settings.llm_structured_output_mode == "none":
        raise ValueError("STRUCTURED_OUTPUT_CAPABILITY_PROFILE_MISSING")
    profile = wb08r_structured_output_profile(
        profile_revision=(
            f"wb08r-c5-{diagnostic_phase}-v1"
            if diagnostic_phase is not None
            else str(settings.llm_structured_profile_revision)
        ),
        service_identity_sha256=str(
            settings.llm_structured_service_identity_sha256
        ),
        model=settings.llm_model,
        chat_template_revision=str(
            settings.llm_structured_chat_template_revision
        ),
        mode=settings.llm_structured_output_mode,
        grammar_backend=str(settings.llm_structured_grammar_backend),
        deadline_ms=round(settings.llm_field_resolution_timeout_seconds * 1000),
        field_resolution_output_tokens=output_tokens,
        qualification_evidence_sha256=str(
            settings.llm_structured_qualification_evidence_sha256
        ),
        allow_unique_items=allow_unique_items,
    )
    if diagnostic_phase is None:
        return profile
    return StructuredOutputCapabilityProfile(
        **{
            **profile.model_dump(mode="python", exclude={"families"}),
            "families": (
                *profile.families,
                StructuredSchemaFamilyQualification(
                    schema_revision=_ORIGINAL_SCHEMA_REVISION,
                    max_schema_bytes=262_144,
                    max_atoms=4,
                    max_candidates_per_atom=16,
                    max_candidate_id_length=16,
                    max_query_fragment_length=None,
                ),
            ),
        }
    )


def _original_wire_schema(
    prepared: PreparedCase, *, allow_unique_items: bool
) -> dict[str, object]:
    """逐字段重建 ``de251f0`` 的 v1 schema，仅可切换 uniqueItems。"""
    atom_ids = tuple(
        dict.fromkeys(item.atom_id for item in prepared.candidates)
    )
    candidate_ids = sorted({item.candidate_id for item in prepared.candidates})
    candidate_array: dict[str, object] = {
        "type": "array",
        "maxItems": 16,
        "items": {"type": "string", "enum": candidate_ids},
    }
    if allow_unique_items:
        candidate_array["uniqueItems"] = True
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["r"],
        "properties": {
            "r": {
                "type": "array",
                "minItems": len(atom_ids),
                "maxItems": len(atom_ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["a", "s", "c", "q"],
                    "properties": {
                        "a": {"type": "string", "enum": list(atom_ids)},
                        "s": {
                            "type": "string",
                            "enum": [
                                "SUPPORTED_PARAPHRASE",
                                "RELATED_FIELD",
                                "AMBIGUOUS",
                                "NOT_FOUND",
                            ],
                        },
                        "c": candidate_array,
                        "q": {"type": "string", "minLength": 1},
                    },
                },
            }
        },
    }


def _original_messages(prepared: PreparedCase) -> tuple[ChatMessage, ...]:
    """逐字保留旧字段解析提示和业务 payload，避免诊断混入 v2 改动。"""
    return (
        ChatMessage(
            role="system",
            content=(
                "只在服务端给出的真实表结构字段候选中解析用户所问的"
                "基础字段，不回答问题，不判断必须、先后、禁止等附加命题。"
                "SUPPORTED_PARAPHRASE 仅用于同一目标下可接受的等义字段；"
                "RELATED_FIELD 表示只能作为相关资料；多个字段仍可能成立时"
                "返回 AMBIGUOUS；没有候选时返回 NOT_FOUND。"
                "c 只填给定短 ID，q 必须逐字摘自用户问题。"
                "输出只包含符合 JSON Schema 的对象。"
            ),
        ),
        ChatMessage(
            role="user",
            content=json.dumps(
                {
                    "question": prepared.request.text,
                    "candidates": [
                        {
                            "id": item.candidate_id,
                            "atom": item.atom_id,
                            "target": item.target_label,
                            "field": item.field_label,
                            "value_preview": item.value_preview,
                        }
                        for item in prepared.candidates
                    ],
                },
                ensure_ascii=False,
            ),
        ),
    )


def _original_diagnostic_outcome(
    model: ProductGroundedModel,
    prepared: PreparedCase,
    schema: dict[str, object],
    profile: StructuredOutputCapabilityProfile,
) -> FieldResolutionOutcome:
    """经真实 Adapter 只发送一次旧 v1 请求，不消费其语义内容。"""
    started = perf_counter()
    timeout = 8.0
    adapter = cast(OpenAICompatibleChatAdapter, model.adapter)
    try:
        completion = adapter.complete(
            _original_messages(prepared),
            operation="query.interpret",
            max_output_tokens=_ORIGINAL_OUTPUT_TOKENS,
            timeout_seconds=timeout,
            json_schema=schema,
            schema_revision=_ORIGINAL_SCHEMA_REVISION,
            request_label="RECONSTRUCTED_REQUEST",
        )
    except RagError as error:
        if isinstance(error, (ProviderRequestRejected, ProviderInputTooLarge)):
            execution_state = FieldResolutionExecutionState.REQUEST_REJECTED
        elif isinstance(error, ProviderInvalidResponse):
            execution_state = FieldResolutionExecutionState.OUTPUT_INVALID
        else:
            execution_state = FieldResolutionExecutionState.TRANSPORT_FAILED
        provider_call = getattr(error, "provider_call", None)
        return FieldResolutionOutcome(
            calls=() if provider_call is None else (provider_call,),
            execution_state=execution_state,
            reason_code="RECONSTRUCTED_FIELD_PROTOCOL_REJECTED",
            attempted=True,
            failure_category=str(
                dict(error.details).get("reason_code", error.code)
            ),
            latency_ms=round((perf_counter() - started) * 1000),
            transport_timeout_ms=round(timeout * 1000),
            schema_revision=_ORIGINAL_SCHEMA_REVISION,
            schema_sha256=canonical_sha256(schema),
            contract_sha256=prepared.contract.contract_sha256,
            capability_profile_sha256=profile.profile_sha256,
        )
    usage = getattr(completion, "usage", None)
    return FieldResolutionOutcome(
        calls=(completion.call,),
        execution_state=FieldResolutionExecutionState.SUCCEEDED,
        reason_code="RECONSTRUCTED_FIELD_PROTOCOL_ACCEPTED",
        attempted=True,
        latency_ms=round((perf_counter() - started) * 1000),
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        finish_reason=completion.finish_reason,
        transport_timeout_ms=round(timeout * 1000),
        schema_revision=_ORIGINAL_SCHEMA_REVISION,
        schema_sha256=canonical_sha256(schema),
        contract_sha256=prepared.contract.contract_sha256,
        capability_profile_sha256=profile.profile_sha256,
    )


def _credential(settings: InternalCredentialSettings) -> str:
    if settings.source == "none":
        return ""
    if settings.source == "environment":
        value = os.environ.get(settings.environment_name or "", "")
        if not value:
            raise ValueError("LLM_CREDENTIAL_ENVIRONMENT_MISSING")
        return value
    return settings.database_secret()


def _model(
    settings: InternalModelSettings,
    profile: StructuredOutputCapabilityProfile,
    recorder: PrivateProviderDiagnosticRecorder,
) -> ProductGroundedModel:
    """按候选环境的真实 URL、模型、模式和 Adapter 构造生产方法。"""
    client = ProviderHttpClient(
        settings.llm_base_url,
        client=httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=90.0, write=30.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
        ),
        max_attempts=1,
        allow_http=urlsplit(settings.llm_base_url).scheme == "http",
        use_budget_transport=False,
        private_diagnostic_recorder=recorder,
        defer_success_observation=True,
    )
    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model=settings.llm_model,
            egress_allowed=True,
            max_input_tokens=6144,
            max_output_tokens=1536,
            disable_thinking_supported=(
                settings.llm_disable_thinking_supported
            ),
            disable_thinking=settings.llm_disable_thinking,
            structured_output_mode=settings.llm_structured_output_mode,
            structured_output_profile=profile,
        ),
        http_client=client,
        api_key_resolver=lambda: _credential(settings.llm_credential),
    )
    model = object.__new__(ProductGroundedModel)
    model._campaign_required = False
    model.adapter = adapter
    model.settings = KnowledgeBaseModelSettings(
        planner_transport_timeout_seconds=8.0,
        field_resolution_max_output_tokens=(
            profile.field_resolution_output_tokens
        ),
        field_resolution_transport_timeout_seconds=(
            settings.llm_field_resolution_timeout_seconds
        ),
        field_resolution_total_deadline_seconds=(
            settings.llm_field_resolution_total_deadline_seconds
        ),
    )
    return model


def _qualified_case_outcome(
    model: ProductGroundedModel,
    prepared: PreparedCase,
    profile: StructuredOutputCapabilityProfile,
) -> tuple[FieldResolutionOutcome, tuple[QualifiedProbe, ...]]:
    """按产品路径先做局部精确选择，再逐 Atom 发送完整候选。"""
    resolutions = default_field_resolutions(
        prepared.query_plan,
        prepared.candidates,
        prepared.query_view,
    )
    exact_atom_ids = {
        item.atom_id
        for item in resolutions
        if item.status is FieldResolutionStatus.EXACT
    }
    probes: list[QualifiedProbe] = []
    started = perf_counter()
    deadline = started + model.field_resolution_total_deadline_seconds
    for atom in prepared.atoms:
        if atom.atom_id in exact_atom_ids:
            continue
        candidates = tuple(
            item for item in prepared.candidates if item.atom_id == atom.atom_id
        )
        contract = build_field_resolution_contract(
            FieldResolutionContractContext(
                query_view=prepared.query_view,
                atoms=(atom,),
                candidates=candidates,
            )
        )
        schema = render_wire_schema(contract, profile)
        remaining = deadline - perf_counter()
        if remaining <= 0:
            outcome = FieldResolutionOutcome(
                reason_code="FIELD_RESOLUTION_TOTAL_DEADLINE_EXCEEDED",
                failure_category=("FIELD_RESOLUTION_TOTAL_DEADLINE_EXCEEDED"),
                execution_state=(
                    FieldResolutionExecutionState.TRANSPORT_FAILED
                ),
                schema_revision=contract.schema_revision,
                schema_sha256=canonical_sha256(schema),
                contract_sha256=contract.contract_sha256,
                capability_profile_sha256=profile.profile_sha256,
            )
        else:
            outcome = model.resolve_fields(
                prepared.request,
                candidates,
                query_view=prepared.query_view,
                atoms=(atom,),
                timeout_seconds=remaining,
            )
        probes.append(
            QualifiedProbe(
                atom_id=atom.atom_id,
                contract=contract,
                schema=schema,
                outcome=outcome,
            )
        )
        if outcome.execution_state is FieldResolutionExecutionState.SUCCEEDED:
            resolutions = merge_field_resolutions(
                resolutions,
                outcome.resolutions,
                prepared.candidates,
                prepared.query_view,
            )
    failures = tuple(
        probe.outcome
        for probe in probes
        if probe.outcome.execution_state
        is not FieldResolutionExecutionState.SUCCEEDED
    )
    first_failure = failures[0] if failures else None
    outcomes = tuple(probe.outcome for probe in probes)

    def token_sum(name: Literal["input_tokens", "output_tokens"]) -> int | None:
        values = tuple(
            cast(int | None, getattr(outcome, name)) for outcome in outcomes
        )
        if not values or any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    finish_reasons = {
        outcome.finish_reason
        for outcome in outcomes
        if outcome.finish_reason is not None
    }
    finish_reason = (
        next(iter(finish_reasons))
        if len(finish_reasons) == 1
        and all(outcome.finish_reason is not None for outcome in outcomes)
        else None
    )
    calls = tuple(call for outcome in outcomes for call in outcome.calls)
    return (
        FieldResolutionOutcome(
            resolutions=resolutions,
            calls=calls,
            execution_state=(
                first_failure.execution_state
                if first_failure is not None
                else FieldResolutionExecutionState.SUCCEEDED
            ),
            reason_code=(
                first_failure.reason_code
                if first_failure is not None
                else "FIELD_RESOLUTION_ACCEPTED"
                if probes
                else "FIELD_RESOLUTION_LOCAL_EXACT"
            ),
            attempted=any(outcome.attempted for outcome in outcomes),
            failure_category=(
                first_failure.failure_category
                if first_failure is not None
                else None
            ),
            latency_ms=round((perf_counter() - started) * 1000),
            input_tokens=token_sum("input_tokens"),
            output_tokens=token_sum("output_tokens"),
            finish_reason=finish_reason,
            transport_timeout_ms=max(
                (outcome.transport_timeout_ms for outcome in outcomes),
                default=0,
            ),
            schema_revision=(
                outcomes[0].schema_revision
                if outcomes
                and len({outcome.schema_revision for outcome in outcomes}) == 1
                else None
            ),
            schema_sha256=(
                canonical_sha256(
                    tuple(
                        (probe.atom_id, probe.outcome.schema_sha256)
                        for probe in probes
                    )
                )
                if probes
                else None
            ),
            contract_sha256=(
                canonical_sha256(
                    tuple(
                        (probe.atom_id, probe.contract.contract_sha256)
                        for probe in probes
                    )
                )
                if probes
                else None
            ),
            capability_profile_sha256=profile.profile_sha256,
            response_content_sha256=(
                first_failure.response_content_sha256
                if first_failure is not None
                else None
            ),
            response_content_length=(
                first_failure.response_content_length
                if first_failure is not None
                else None
            ),
            invalid_atom_id=(
                first_failure.invalid_atom_id
                if first_failure is not None
                else None
            ),
            invalid_status=(
                first_failure.invalid_status
                if first_failure is not None
                else None
            ),
            invalid_candidate_count=(
                first_failure.invalid_candidate_count
                if first_failure is not None
                else None
            ),
            invalid_query_fragment_length=(
                first_failure.invalid_query_fragment_length
                if first_failure is not None
                else None
            ),
            private_diagnostic_status=(
                first_failure.private_diagnostic_status
                if first_failure is not None
                else None
            ),
        ),
        tuple(probes),
    )


def _outcome_record(
    prepared: PreparedCase,
    outcome: FieldResolutionOutcome,
    schema_identity: object,
    profile: StructuredOutputCapabilityProfile,
) -> dict[str, object]:
    resolutions = [
        {
            "atom_id": item.atom_id,
            "status": item.status.value,
            "candidate_ids": list(item.candidate_ids),
            "candidate_identity_sha256s": [
                canonical_sha256(
                    prepared.identities_by_candidate_id[candidate_id]
                )
                for candidate_id in item.candidate_ids
            ],
            "query_fragment_sha256": (
                None
                if item.query_fragment is None
                else canonical_sha256(item.query_fragment)
            ),
            "query_span": [item.query_span_start, item.query_span_end],
            "span_basis": item.span_basis,
            "query_view_digest": item.query_view_digest,
            "reason_code": item.reason_code,
        }
        for item in outcome.resolutions
    ]
    return {
        "case_id": prepared.case.case_id,
        "question_sha256": canonical_sha256(prepared.case.question),
        "execution_state": outcome.execution_state.value,
        "reason_code": outcome.reason_code,
        "failure_category": outcome.failure_category,
        "attempted": outcome.attempted,
        "request_count": sum(call.call_count for call in outcome.calls),
        "finish_reason": outcome.finish_reason,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "schema_revision": outcome.schema_revision,
        "schema_sha256": canonical_sha256(schema_identity),
        "contract_sha256": (
            outcome.contract_sha256 or prepared.contract.contract_sha256
        ),
        "capability_profile_sha256": profile.profile_sha256,
        "grammar_backend_fingerprint": profile.grammar_backend_fingerprint,
        "resolutions": resolutions,
        "provider_calls": [
            {
                "operation": call.operation,
                "status_category": call.status_category,
                "reason_code": call.reason_code,
                "attempt_count": call.attempt_count,
                "transport_diagnostics": dict(call.transport_diagnostics),
            }
            for call in outcome.calls
        ],
    }


def _case_passes(  # noqa: PLR0912
    prepared: PreparedCase,
    outcome: FieldResolutionOutcome,
    profile: StructuredOutputCapabilityProfile,
) -> tuple[bool, tuple[str, ...]]:
    failures: list[str] = []
    defaults = default_field_resolutions(
        prepared.query_plan,
        prepared.candidates,
        prepared.query_view,
    )
    expected_request_count = sum(
        item.status is not FieldResolutionStatus.EXACT for item in defaults
    )
    if outcome.execution_state is not FieldResolutionExecutionState.SUCCEEDED:
        failures.append("EXECUTION_NOT_SUCCEEDED")
    actual_request_count = sum(call.call_count for call in outcome.calls)
    if actual_request_count != expected_request_count:
        failures.append("REQUEST_COUNT_MISMATCH")
    if expected_request_count:
        if (
            not outcome.attempted
            or len(outcome.calls) != expected_request_count
        ):
            failures.append("ATTEMPT_LEDGER_MISMATCH")
        if outcome.finish_reason != "stop":
            failures.append("FINISH_REASON_NOT_STOP")
        if outcome.output_tokens is None or outcome.output_tokens > (
            profile.field_resolution_output_tokens * expected_request_count
        ):
            failures.append("OUTPUT_USAGE_INVALID")
    elif outcome.attempted or outcome.calls:
        failures.append("LOCAL_EXACT_CALLED_PROVIDER")
    actual = {item.atom_id: item for item in outcome.resolutions}
    if set(actual) != {item.atom_id for item in prepared.case.atoms}:
        failures.append("ATOM_SET_MISMATCH")
    for expected in prepared.case.atoms:
        resolution = actual.get(expected.atom_id)
        if resolution is None:
            continue
        identities = {
            prepared.identities_by_candidate_id[candidate_id]
            for candidate_id in resolution.candidate_ids
        }
        if resolution.status is not expected.expected_status:
            failures.append(f"{expected.atom_id}_STATUS_MISMATCH")
        if identities != set(expected.expected_candidate_identities):
            failures.append(f"{expected.atom_id}_CANDIDATE_MISMATCH")
        if resolution.status is not FieldResolutionStatus.EXACT and (
            resolution.query_span_start is None
            or resolution.query_span_end is None
            or resolution.query_fragment is None
            or prepared.query_view.business_query[
                resolution.query_span_start : resolution.query_span_end
            ]
            != resolution.query_fragment
        ):
            failures.append(f"{expected.atom_id}_QUERY_ANCHOR_INVALID")
    return not failures, tuple(failures)


def _private_input(path: Path) -> Path:
    resolved = path.expanduser()
    if not resolved.is_absolute() or resolved.is_symlink():
        raise ValueError("PRIVATE_INPUT_PATH_INVALID")
    resolved = resolved.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("PRIVATE_INPUT_NOT_FILE")
    if os.name == "posix" and stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise ValueError("PRIVATE_INPUT_PERMISSIONS")
    return resolved


def _private_output_directory(path: Path) -> Path:
    resolved = path.expanduser()
    if not resolved.is_absolute() or resolved.exists() or resolved.is_symlink():
        raise ValueError("PRIVATE_OUTPUT_PATH_INVALID")
    if any((parent / ".git").exists() for parent in resolved.parents):
        raise ValueError("PRIVATE_OUTPUT_INSIDE_GIT")
    resolved.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=False)
    if (
        os.name == "posix"
        and stat.S_IMODE(resolved.stat().st_mode) != _PRIVATE_DIRECTORY_MODE
    ):
        raise ValueError("PRIVATE_OUTPUT_PERMISSIONS")
    return resolved


def _write_private(path: Path, value: object) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        _PRIVATE_FILE_MODE,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(  # noqa: PLR0915
    *,
    phase: Literal[
        "reconstructed-original",
        "reconstructed-transferred",
        "qualified",
    ],
    output_dir: Path,
    acknowledgement: str,
    fixture: Path | None = None,
) -> dict[str, object]:
    """执行明确的一种真实探针阶段并写出私有/安全证据。"""
    if acknowledgement != _ACK:
        raise ValueError("REAL_PROBE_ACK_REQUIRED")
    diagnostic_phase = phase in {
        "reconstructed-original",
        "reconstructed-transferred",
    }
    if diagnostic_phase != (fixture is not None):
        raise ValueError("RECONSTRUCTED_FIXTURE_PHASE_MISMATCH")
    output = _private_output_directory(output_dir)
    settings = InternalModelSettings.from_environment()
    recorder = PrivateProviderDiagnosticRecorder.from_environment()
    if recorder is None:
        raise ValueError("PRIVATE_PROVIDER_DIAGNOSTIC_REQUIRED")
    if diagnostic_phase:
        cases: tuple[QualificationCase, ...] = (
            _fixture_case(fixture),  # type: ignore[arg-type]
        )
        allow_unique_items = phase == "reconstructed-original"
        output_tokens = _ORIGINAL_OUTPUT_TOKENS
    else:
        cases = _qualification_cases()
        if settings.llm_structured_allow_unique_items is None:
            raise ValueError("UNIQUE_ITEMS_CAPABILITY_NOT_FIXED")
        allow_unique_items = settings.llm_structured_allow_unique_items
        output_tokens = settings.llm_field_resolution_max_output_tokens
    profile = _profile(
        settings,
        allow_unique_items=allow_unique_items,
        output_tokens=output_tokens,
        diagnostic_phase=(phase if diagnostic_phase else None),  # type: ignore[arg-type]
    )
    model = _model(settings, profile, recorder)
    safe_records: list[dict[str, object]] = []
    private_records: list[dict[str, object]] = []
    request_count = 0
    try:
        for case in cases:
            prepared = _prepare_case(case)
            probes: tuple[QualifiedProbe, ...] = ()
            if diagnostic_phase:
                schema = _original_wire_schema(
                    prepared, allow_unique_items=allow_unique_items
                )
                outcome = _original_diagnostic_outcome(
                    model, prepared, schema, profile
                )
                adapter = cast(OpenAICompatibleChatAdapter, model.adapter)
                final_http_payload = openai_compatible_chat_payload(
                    _original_messages(prepared),
                    adapter.compatible_config,
                    max_output_tokens=_ORIGINAL_OUTPUT_TOKENS,
                    json_schema=schema,
                    schema_revision=_ORIGINAL_SCHEMA_REVISION,
                )
                schema_identity: object = schema
            else:
                outcome, probes = _qualified_case_outcome(
                    model,
                    prepared,
                    profile,
                )
                schema_identity = tuple(
                    (probe.atom_id, canonical_sha256(probe.schema))
                    for probe in probes
                )
                final_http_payload = None
            record = _outcome_record(
                prepared,
                outcome,
                schema_identity,
                profile,
            )
            record["atom_attempts"] = [
                {
                    "atom_id": probe.atom_id,
                    "candidate_count": len(probe.contract.candidates),
                    "execution_state": probe.outcome.execution_state.value,
                    "reason_code": probe.outcome.reason_code,
                    "failure_category": probe.outcome.failure_category,
                    "request_count": sum(
                        call.call_count for call in probe.outcome.calls
                    ),
                    "latency_ms": probe.outcome.latency_ms,
                    "input_tokens": probe.outcome.input_tokens,
                    "output_tokens": probe.outcome.output_tokens,
                    "finish_reason": probe.outcome.finish_reason,
                    "response_content_sha256": (
                        probe.outcome.response_content_sha256
                    ),
                    "response_content_length": (
                        probe.outcome.response_content_length
                    ),
                    "invalid_status": probe.outcome.invalid_status,
                    "invalid_candidate_count": (
                        probe.outcome.invalid_candidate_count
                    ),
                    "private_diagnostic_status": (
                        probe.outcome.private_diagnostic_status
                    ),
                    "schema_sha256": canonical_sha256(probe.schema),
                    "contract_sha256": probe.contract.contract_sha256,
                }
                for probe in probes
            ]
            request_count += sum(call.call_count for call in outcome.calls)
            if phase == "qualified":
                passed, failures = _case_passes(prepared, outcome, profile)
            else:
                passed = (
                    outcome.attempted
                    and len(outcome.calls) == 1
                    and sum(call.call_count for call in outcome.calls) == 1
                )
                failures = () if passed else ("DIAGNOSTIC_CALL_NOT_ONE",)
            safe_records.append(
                {**record, "passed": passed, "failures": list(failures)}
            )
            private_records.append(
                {
                    **record,
                    "label": (
                        "RECONSTRUCTED_REQUEST"
                        if diagnostic_phase
                        else "SYNTHETIC_QUALIFICATION_REQUEST"
                    ),
                    "request_payload": (
                        final_http_payload
                        if final_http_payload is not None
                        else [
                            {
                                "atom_id": probe.atom_id,
                                "payload": request_payload(probe.contract),
                            }
                            for probe in probes
                        ]
                    ),
                    "wire_schema": (
                        schema
                        if diagnostic_phase
                        else [
                            {
                                "atom_id": probe.atom_id,
                                "schema": probe.schema,
                            }
                            for probe in probes
                        ]
                    ),
                }
            )
    finally:
        model.adapter.close()
    passed = all(bool(item["passed"]) for item in safe_records)
    protocol_accepted = all(
        item["execution_state"] == FieldResolutionExecutionState.SUCCEEDED.value
        for item in safe_records
    )
    manifest: dict[str, object] = {
        "schema_version": "wb08r03-v141-c5-protocol-qualification-v2",
        "phase": phase,
        "gate_kind": "DIAGNOSTIC" if diagnostic_phase else "QUALIFICATION",
        "passed": passed,
        "protocol_accepted": protocol_accepted,
        "request_count": request_count,
        "case_count": len(safe_records),
        "model_sha256": canonical_sha256(settings.llm_model),
        "service_identity_sha256": profile.service_identity_sha256,
        "chat_template_revision_sha256": canonical_sha256(
            profile.chat_template_revision
        ),
        "mode": profile.mode,
        "grammar_backend_fingerprint": profile.grammar_backend_fingerprint,
        "capability_profile_sha256": profile.profile_sha256,
        "unique_items_execution": (
            "PROVIDER_PROBE_UNQUALIFIED"
            if phase == "reconstructed-original"
            else "LOCAL_VALIDATOR_PROBE_UNQUALIFIED"
            if phase == "reconstructed-transferred"
            else "PROVIDER"
            if allow_unique_items
            else "LOCAL_VALIDATOR"
        ),
        "field_resolution_output_tokens": output_tokens,
        "observations": safe_records,
    }
    private_path = output / "protocol-probes.private.json"
    safe_path = output / "protocol-probes.safe.json"
    _write_private(private_path, private_records)
    _write_private(safe_path, manifest)
    summary = {
        "phase": phase,
        "passed": passed,
        "request_count": manifest["request_count"],
        "case_count": manifest["case_count"],
        "safe_sha256": _sha256_file(safe_path),
        "private_sha256": _sha256_file(private_path),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return manifest


def main() -> None:
    """解析一次显式真实探针阶段并以非零码报告资格失败。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=(
            "reconstructed-original",
            "reconstructed-transferred",
            "qualified",
        ),
        required=True,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--ack", required=True)
    args = parser.parse_args()
    manifest = run(
        phase=args.phase,
        output_dir=args.output_dir,
        acknowledgement=args.ack,
        fixture=args.fixture,
    )
    if args.phase == "qualified" and not manifest["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
