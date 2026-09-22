"""部门影子观察不得改变主检索合同。"""

from __future__ import annotations

from pathlib import Path

from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.identifiers import deterministic_id
from rag_app.core.models import DocumentRef, KnowledgeBaseScope, SearchRequest
from rag_app.core.ports import (
    DepartmentShadowObservation,
    DepartmentShadowRequest,
)
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.support.grounded_fixture_generator import GroundedFixtureGenerator

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class _CapturingObserver:
    def __init__(self) -> None:
        self.requests: list[DepartmentShadowRequest] = []

    def observe(
        self, request: DepartmentShadowRequest
    ) -> DepartmentShadowObservation:
        self.requests.append(request)
        return DepartmentShadowObservation(
            route_revision="wanshitong-department-shadow-v1",
            profile_revision=None,
            resolved_root_query_sha256="1" * 64,
            context_mode=request.context_mode,
            scope_digest=request.scope_digest,
            actual_scope_kind=request.actual_scope_kind,
            top1_department_key=None,
            top1_score_bucket=None,
            top2_department_key=None,
            top2_score_bucket=None,
            confidence="LOW",
            recommended_scope="GLOBAL",
            final_cited_department_keys=(),
            department_filter_applied=False,
            embedding_reused=False,
            extra_provider_calls=0,
            status="FALLBACK",
            reason_codes=("PROFILE_UNAVAILABLE",),
            elapsed_ms=0,
        )


def _public_contract(result: object) -> dict[str, object]:
    return result.model_dump(  # type: ignore[union-attr]
        mode="json", exclude={"trace_id", "diagnostics"}
    )


def test_shadow_ab_preserves_result_cache_and_provider_calls(
    tmp_path: Path,
) -> None:
    project_id = deterministic_id("prj", "department-shadow-ab")
    knowledge_base_id = deterministic_id(
        "kb", project_id, "department-shadow-ab"
    )
    document = DocumentRef(
        project_id=project_id,
        knowledge_base_id=knowledge_base_id,
        document_id=deterministic_id("doc", "department-shadow-ab"),
        display_name="department-shadow.docx",
    )
    observer = _CapturingObserver()
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        service = runtime.retrieval.with_generation(
            GroundedFixtureGenerator(),
            serving_identity="unit-department-shadow-v1",
        )
        runtime.persistence.control.put_project(project_id, "Shadow Project")
        runtime.persistence.control.put_knowledge_base(
            knowledge_base_id,
            project_id,
            "Shadow KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
            documents=(
                IngestionDocument(
                    document=document,
                    content=build_docx(
                        "<w:p><w:r><w:t>订单 ABC-123 由科研部负责。</w:t>"
                        "</w:r></w:p>"
                    ),
                    media_type=_MEDIA_TYPE,
                ),
            ),
            idempotency_key="department-shadow-ab",
            budgets=runtime.persistence.default_budgets(),
        )
        scope = KnowledgeBaseScope(
            project_id=project_id, knowledge_base_id=knowledge_base_id
        )
        baseline_request = SearchRequest(
            scope=scope,
            text="订单 ABC-123 由谁负责？",
            metadata_filters={"role": "text"},
            trace_id="trace_" + "1" * 32,
        )
        shadow_request = baseline_request.model_copy(
            update={"trace_id": "trace_" + "2" * 32}
        )
        baseline = service.search_and_answer(
            baseline_request, cache_result=False
        )
        observed = service.with_department_shadow(observer).search_and_answer(
            shadow_request, cache_result=False
        )
        baseline_events = runtime.persistence.components.trace_sink.events(
            baseline.trace_id
        )
        observed_events = runtime.persistence.components.trace_sink.events(
            observed.trace_id
        )

        cached_source = service.search_and_answer(baseline_request)
        cached = service.with_department_shadow(observer).search_and_answer(
            shadow_request
        )

    assert _public_contract(observed) == _public_contract(baseline)
    assert observed.cache_key == baseline.cache_key
    assert baseline_request.metadata_filters == shadow_request.metadata_filters
    assert baseline.diagnostics is not None
    assert observed.diagnostics is not None
    assert (
        baseline.diagnostics.provider_calls
        == observed.diagnostics.provider_calls
    )
    assert not any(
        event.event_name == "retrieval.department_route_shadow"
        for event in baseline_events
    )
    shadow_event = next(
        event
        for event in observed_events
        if event.event_name == "retrieval.department_route_shadow"
    )
    assert dict(shadow_event.attributes)["department_filter_applied"] is False
    assert dict(shadow_event.attributes)["extra_provider_calls"] == 0
    assert "订单 ABC-123 由谁负责？" not in shadow_event.model_dump_json()
    assert cached.cache_hit is True
    assert cached.cache_key == cached_source.cache_key
    assert cached.answer == cached_source.answer
    assert cached.evidence == cached_source.evidence
    assert observer.requests[-1].root_vector is None
    assert cached.diagnostics is not None
    assert not any(
        item.operation in {"embedding.query", "generation"}
        and item.call_count > 0
        for item in cached.diagnostics.provider_calls
    )
