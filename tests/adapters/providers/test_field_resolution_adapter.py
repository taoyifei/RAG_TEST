"""真实 OpenAI-compatible Adapter 与假 HTTP 边界的 C5 协议门禁。"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from rag_app.adapters.providers.field_resolution_wire import (
    FieldResolutionContractContext,
    build_field_resolution_contract,
    render_wire_schema,
)
from rag_app.adapters.providers.http_common import ProviderHttpClient
from rag_app.adapters.providers.openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleChatConfig,
)
from rag_app.adapters.providers.structured_contract import (
    StructuredOutputCapabilityProfile,
    wb08r_structured_output_profile,
)
from rag_app.application.retrieval.adaptive import (
    FieldResolutionExecutionState,
)
from rag_app.application.retrieval.source_scope import (
    resolve_query_source_context,
)
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    FieldCandidate,
    KnowledgeBaseScope,
    ResolvedQueryView,
    SearchRequest,
)
from rag_app.core.models.query_plan import AtomAnswerShape, QueryAtom
from rag_app.product.grounded_runtime import ProductGroundedModel
from rag_app.product.model_settings import KnowledgeBaseModelSettings

_MODEL = "Qwen/C5-Unit"


def _profile(*, output_tokens: int = 512) -> StructuredOutputCapabilityProfile:
    return wb08r_structured_output_profile(
        profile_revision="c5-actual-adapter-unit-v1",
        service_identity_sha256=canonical_sha256("unit-service"),
        model=_MODEL,
        chat_template_revision="unit-template",
        mode="response_format",
        grammar_backend="xgrammar-unit",
        deadline_ms=8000,
        field_resolution_output_tokens=output_tokens,
        qualification_evidence_sha256=canonical_sha256("unit-qualification"),
    )


def _candidate(candidate_id: str, column: int) -> FieldCandidate:
    return FieldCandidate(
        candidate_id=candidate_id,
        atom_id="A1",
        fact_id=canonical_sha256((candidate_id, "fact")),
        document_id=deterministic_id("doc", "actual-adapter"),
        document_version_id=deterministic_id("dver", "actual-adapter"),
        table_key=canonical_sha256("actual-adapter-table"),
        row_index=1,
        value_column_index=column,
        target_label="需求快验",
        field_label="输入" if column == 1 else "输出",
        value_preview="公开合成预览",
        dependency_support_ids=(f"S{column}",),
    )


def _context() -> tuple[
    SearchRequest,
    ResolvedQueryView,
    tuple[QueryAtom, ...],
    tuple[FieldCandidate, ...],
]:
    request = SearchRequest(
        scope=KnowledgeBaseScope(
            project_id=deterministic_id("prj", "actual-adapter"),
            knowledge_base_id=deterministic_id("kb", "actual-adapter"),
        ),
        text="需求快验开始前必须准备哪些材料？",
    )
    query_view = resolve_query_source_context(
        request.text,
        (),
        registry_revision="actual-adapter-unit-v1",
    ).query_view
    atoms = (
        QueryAtom(
            atom_id="A1",
            target="需求快验",
            relation="准备材料",
            answer_shape=AtomAnswerShape.FACT,
            original_fragment=request.text,
        ),
    )
    return (
        request,
        query_view,
        atoms,
        (_candidate("F1", 1), _candidate("F2", 2)),
    )


def _model(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    profile: StructuredOutputCapabilityProfile | None = None,
    qualified: bool = True,
) -> ProductGroundedModel:
    capability = profile or (_profile() if qualified else None)
    adapter = OpenAICompatibleChatAdapter(
        OpenAICompatibleChatConfig(
            model=_MODEL,
            egress_allowed=True,
            max_input_tokens=6144,
            max_output_tokens=1536,
            structured_output_mode="response_format",
            structured_output_profile=capability,
        ),
        http_client=ProviderHttpClient(
            "http://provider.example/v1",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=3,
            allow_http=True,
            use_budget_transport=False,
        ),
        api_key_resolver=lambda: "unit-secret",
    )
    model = object.__new__(ProductGroundedModel)
    model._campaign_required = False  # type: ignore[assignment]
    model.adapter = adapter  # type: ignore[assignment]
    model.settings = KnowledgeBaseModelSettings(  # type: ignore[assignment]
        planner_transport_timeout_seconds=8,
        field_resolution_max_output_tokens=(
            160
            if capability is None
            else capability.field_resolution_output_tokens
        ),
    )
    return model


def _chat_response(content: str) -> dict[str, object]:
    return {
        "model": _MODEL,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 30,
            "total_tokens": 230,
        },
    }


def test_actual_adapter_sends_exact_rendered_schema_and_fixed_mode() -> None:
    requests: list[dict[str, object]] = []
    content = json.dumps(
        {
            "r": [
                {
                    "a": "A1",
                    "s": "SUPPORTED_PARAPHRASE",
                    "c": ["F1"],
                    "q": "准备哪些材料",
                }
            ]
        },
        ensure_ascii=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response(content))

    profile = _profile()
    model = _model(handler, profile=profile)
    request, query_view, atoms, candidates = _context()
    outcome = model.resolve_fields(
        request,
        candidates,
        query_view=query_view,
        atoms=atoms,
    )
    model.adapter.close()

    contract = build_field_resolution_contract(
        FieldResolutionContractContext(
            query_view=query_view,
            atoms=atoms,
            candidates=candidates,
        )
    )
    expected_schema = render_wire_schema(contract, profile)
    assert len(requests) == 1
    body = requests[0]
    assert body["max_tokens"] == 512
    assert "guided_json" not in body
    assert "structured_outputs" not in body
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "wb08r-field-resolution-v2",
            "schema": expected_schema,
            "strict": True,
        },
    }
    assert outcome.execution_state is FieldResolutionExecutionState.SUCCEEDED
    assert outcome.contract_sha256 == contract.contract_sha256
    assert outcome.capability_profile_sha256 == profile.profile_sha256
    assert outcome.schema_sha256 == canonical_sha256(expected_schema)


def test_actual_adapter_400_propagates_as_request_rejected_without_retry() -> (
    None
):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "code": "json_schema_invalid",
                    "param": "response_format",
                    "message": "private request content",
                }
            },
        )

    model = _model(handler)
    request, query_view, atoms, candidates = _context()
    outcome = model.resolve_fields(
        request,
        candidates,
        query_view=query_view,
        atoms=atoms,
    )
    model.adapter.close()

    assert len(requests) == 1
    assert outcome.resolutions == ()
    assert outcome.execution_state is (
        FieldResolutionExecutionState.REQUEST_REJECTED
    )
    assert outcome.failure_category == "json_schema_invalid"
    assert len(outcome.calls) == 1
    diagnostics = dict(outcome.calls[0].transport_diagnostics)
    assert diagnostics["provider_error_code"] == "json_schema_invalid"
    assert diagnostics["provider_error_param"] == "response_format"
    assert "private request content" not in outcome.calls[0].model_dump_json()


def test_explicit_context_limit_is_request_rejected_not_transport_failure() -> (
    None
):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "private request content",
                }
            },
        )

    model = _model(handler)
    request, query_view, atoms, candidates = _context()
    outcome = model.resolve_fields(
        request,
        candidates,
        query_view=query_view,
        atoms=atoms,
    )
    model.adapter.close()

    assert outcome.execution_state is (
        FieldResolutionExecutionState.REQUEST_REJECTED
    )
    assert outcome.failure_category == "context_length_exceeded"


def test_unqualified_actual_adapter_fails_before_http() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    model = _model(handler, qualified=False)
    request, query_view, atoms, candidates = _context()
    outcome = model.resolve_fields(
        request,
        candidates,
        query_view=query_view,
        atoms=atoms,
    )
    model.adapter.close()

    assert calls == 0
    assert outcome.execution_state is (
        FieldResolutionExecutionState.REQUEST_REJECTED
    )
    assert outcome.reason_code == "FIELD_RESOLUTION_CAPABILITY_UNQUALIFIED"


def test_max_shape_has_explicit_non_planner_output_budget() -> None:
    """离线仅验证合同上界；部署 tokenizer 资格由真实 D3 单独签发。"""
    profile = _profile(output_tokens=1024)
    assert profile.field_resolution_output_tokens == 1024
    assert profile.field_resolution_output_tokens != 160
    assert (
        KnowledgeBaseModelSettings().field_resolution_max_output_tokens == 1280
    )
