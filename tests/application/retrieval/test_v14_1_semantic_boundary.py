"""V14.1 从显式来源检索到 schema-aware 字段解析的因果契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_app.application.retrieval.adaptive import (
    AdaptivePlanOutcome,
    FieldResolutionExecutionState,
    FieldResolutionOutcome,
)
from rag_app.application.retrieval.service import (
    _combine_field_resolution_outcomes,
)
from rag_app.application.revision_builder import IngestionDocument
from rag_app.composition.p07_runtime import build_p07_runtime
from rag_app.core.identifiers import canonical_sha256, deterministic_id
from rag_app.core.models import (
    ActiveRevisionQuerySnapshot,
    ConfidenceStatus,
    DocumentRef,
    FieldCandidate,
    FieldResolution,
    FieldResolutionStatus,
    KnowledgeBaseScope,
    ProviderCall,
    ResolvedQueryView,
    SearchRequest,
    SourceDocumentIdentity,
)
from rag_app.core.models.query_plan import QueryAtom
from rag_app.core.ports.evidence_source import DocumentStructurePage
from tests.adapters.parsers.docx_fixtures import build_docx
from tests.support.grounded_fixture_generator import GroundedFixtureGenerator

_PROFILE = Path("configs/profiles/dev-p06-memory.json")
_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def test_field_resolution_batch_keeps_success_when_another_atom_fails() -> None:
    """批次终态暴露首个系统失败，但不能丢弃已验证的 Atom。"""
    success = FieldResolutionOutcome(
        resolutions=(
            FieldResolution(
                atom_id="A1",
                status=FieldResolutionStatus.SUPPORTED_PARAPHRASE,
                candidate_ids=("F1",),
                query_view_digest=canonical_sha256("query-view"),
                reason_code="SCHEMA_AWARE_INTERPRETATION_V2",
            ),
        ),
        calls=(
            ProviderCall(
                provider_id="unit",
                operation="query.interpret",
                call_count=1,
                retry_count=0,
                elapsed_ms=10,
            ),
        ),
        execution_state=FieldResolutionExecutionState.SUCCEEDED,
        reason_code="FIELD_RESOLUTION_ACCEPTED",
        attempted=True,
        input_tokens=100,
        output_tokens=20,
        finish_reason="stop",
        contract_sha256=canonical_sha256("contract-a1"),
    )
    failed = FieldResolutionOutcome(
        calls=(
            ProviderCall(
                provider_id="unit",
                operation="query.interpret",
                call_count=1,
                retry_count=0,
                elapsed_ms=12,
            ),
        ),
        execution_state=FieldResolutionExecutionState.TRANSPORT_FAILED,
        reason_code="FIELD_RESOLUTION_PROVIDER_UNAVAILABLE",
        attempted=True,
        failure_category="READ_TIMEOUT",
        response_content_sha256=canonical_sha256("invalid-response"),
        invalid_atom_id="A2",
    )

    combined = _combine_field_resolution_outcomes(
        [("A1", success), ("A2", failed)],
        latency_ms=25,
    )

    assert combined.resolutions == success.resolutions
    assert combined.execution_state is (
        FieldResolutionExecutionState.TRANSPORT_FAILED
    )
    assert combined.reason_code == "FIELD_RESOLUTION_PROVIDER_UNAVAILABLE"
    assert combined.failure_category == "READ_TIMEOUT"
    assert sum(call.call_count for call in combined.calls) == 2
    assert combined.invalid_atom_id == "A2"
    assert combined.response_content_sha256 == canonical_sha256(
        "invalid-response"
    )


def _paragraph(text: str) -> str:
    """构造一个最小 WordprocessingML 段落。"""
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _table_document(
    scope: KnowledgeBaseScope,
    title: str,
) -> IngestionDocument:
    """构造含真实竞争字段的 canonical 表格文档。"""
    body = (
        "<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/>"
        "</w:tblGrid><w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc>"
        + _paragraph("模式")
        + "</w:tc><w:tc>"
        + _paragraph("输入（业务团队 / 外部单位需提供）")
        + "</w:tc><w:tc>"
        + _paragraph("输出")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + _paragraph("需求快验")
        + "</w:tc><w:tc>"
        + _paragraph("需求功能点描述、验收标准")
        + "</w:tc><w:tc>"
        + _paragraph("快验结论")
        + "</w:tc></w:tr></w:tbl>"
    )
    return IngestionDocument(
        document=DocumentRef(
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            document_id=deterministic_id("doc", title),
            display_name=f"{title}.docx",
            metadata={
                "document_title": title,
                "source_relative_path": f"研发/{title}.docx",
            },
        ),
        content=build_docx(body),
        media_type=_MEDIA_TYPE,
    )


def test_explicit_source_defers_interpret_until_real_schema_exists(
    tmp_path: Path,
) -> None:
    """不得先无 schema 解释再补一次字段解释。"""
    title = "开发中心三种工作模式"
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "v14-1-schema-aware"),
        knowledge_base_id=deterministic_id("kb", "v14-1-schema-aware"),
    )

    class SchemaAwarePlanner:
        def __init__(self) -> None:
            self.plan_calls = 0
            self.field_calls = 0
            self.questions: list[str] = []
            self.candidates: tuple[FieldCandidate, ...] = ()

        def plan_adaptive(self, *_args: object) -> AdaptivePlanOutcome:
            self.plan_calls += 1
            raise AssertionError("显式来源路径不应执行前置无 schema 解释")

        def resolve_fields(
            self,
            request: SearchRequest,
            candidates: tuple[FieldCandidate, ...],
            *,
            query_view: ResolvedQueryView,
            atoms: tuple[QueryAtom, ...],
            timeout_seconds: float | None = None,
        ) -> FieldResolutionOutcome:
            del atoms, timeout_seconds
            self.field_calls += 1
            self.questions.append(request.text)
            self.candidates = candidates
            selected = next(
                item
                for item in candidates
                if item.field_label.startswith("输入")
            )
            span = "准备哪些材料"
            start = request.text.index(span)
            return FieldResolutionOutcome(
                resolutions=(
                    FieldResolution(
                        atom_id=selected.atom_id,
                        status=FieldResolutionStatus.SUPPORTED_PARAPHRASE,
                        candidate_ids=(selected.candidate_id,),
                        query_span_start=start,
                        query_span_end=start + len(span),
                        query_fragment=span,
                        query_view_digest=canonical_sha256(
                            query_view.model_dump(mode="json")
                        ),
                        reason_code="SCHEMA_AWARE_INTERPRETATION_V2",
                    ),
                ),
                reason_code="FIELD_RESOLUTION_ACCEPTED",
                attempted=True,
                execution_state=FieldResolutionExecutionState.SUCCEEDED,
            )

    planner = SchemaAwarePlanner()
    generator = GroundedFixtureGenerator()
    trace_events: tuple[tuple[str, object], ...] = ()
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.retrieval = runtime.retrieval.with_generation(
            generator,
            serving_identity="unit-v14-1-deterministic-answer",
        )
        runtime.persistence.control.put_project(scope.project_id, "V14.1")
        runtime.persistence.control.put_knowledge_base(
            scope.knowledge_base_id,
            scope.project_id,
            "V14.1 KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            documents=(_table_document(scope, title),),
            idempotency_key="v14-1-schema-aware",
            budgets=runtime.persistence.default_budgets(),
        )
        runtime.retrieval._adaptive_planner = planner  # type: ignore[assignment]
        request = SearchRequest(
            scope=scope,
            text=f"《{title}》中，需求快验开始前必须准备哪些材料？",
        )
        first = runtime.retrieval.search_and_answer(request)
        second = runtime.retrieval.search_and_answer(request)
        trace_events = tuple(
            (event.event_name, dict(event.attributes))
            for event in runtime.retrieval._trace.events(first.trace_id)  # type: ignore[attr-defined]
            if event.event_name
            in {
                "retrieval.atom_grounding",
                "retrieval.query_plan",
                "retrieval.scoped_document_structure",
                "retrieval.source_scope_candidate_ledger",
                "retrieval.structural",
                "retrieval.lexical",
                "retrieval.canonical_table_closure",
                "retrieval.generation_evidence",
                "retrieval.field_resolution",
            }
        )

    assert planner.plan_calls == 0
    assert planner.field_calls == 2, json.dumps(
        trace_events, ensure_ascii=False, indent=2
    )
    assert planner.questions == [
        "需求快验开始前必须准备哪些材料?",
        "需求快验开始前必须准备哪些材料?",
    ]
    assert {item.field_label for item in planner.candidates} == {
        "输入（业务团队 / 外部单位需提供）",
        "输出",
    }, json.dumps(trace_events, ensure_ascii=False, indent=2)
    assert all(item.value_preview for item in planner.candidates)
    events_by_name: dict[str, list[dict[str, object]]] = {}
    for event_name, attributes in trace_events:
        assert isinstance(attributes, dict)
        events_by_name.setdefault(event_name, []).append(attributes)
    assert "retrieval.structural" in events_by_name
    structure = events_by_name["retrieval.scoped_document_structure"][0]
    assert structure["complete"] is True
    ledgers = events_by_name["retrieval.source_scope_candidate_ledger"]
    assert {item["logical_channel"] for item in ledgers} >= {
        "lexical",
        "structural",
    }
    assert all(item["scope_pushdown_applied"] is True for item in ledgers)
    assert all(
        item["store_returned_count"]
        >= item["after_access_filter_count"]
        >= item["after_defense_count"]
        for item in ledgers
    )
    grounding = events_by_name["retrieval.atom_grounding"][0]
    coverage = grounding["answer_plan_coverage"]
    assert isinstance(coverage, list)
    assert coverage[0]["status"] == "PARTIAL"
    assert first.status is ConfidenceStatus.ANSWERABLE
    assert not first.generation_called_this_request
    assert generator.calls == 0
    assert first.answer is not None
    assert first.answer.startswith("现有来源未直接证明")
    assert "输入（业务团队 / 外部单位需提供）" in first.answer
    assert "需求功能点描述" in first.answer
    assert "验收标准" in first.answer
    assert "开始前必须准备" not in first.answer
    assert second.cache_hit
    assert second.cache_key == first.cache_key
    assert second.answer == first.answer


def test_incomplete_structure_never_claims_field_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录页未遍历完成时不得把未解析字段伪装成 NOT_FOUND。"""
    title = "开发中心三种工作模式"
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "v14-1-incomplete-schema"),
        knowledge_base_id=deterministic_id("kb", "v14-1-incomplete-schema"),
    )

    class CountingPlanner:
        def __init__(self) -> None:
            self.plan_calls = 0
            self.field_calls = 0

        def plan_adaptive(self, *_args: object) -> AdaptivePlanOutcome:
            self.plan_calls += 1
            raise AssertionError("显式来源路径不应执行前置解释")

        def resolve_fields(
            self,
            _request: SearchRequest,
            _candidates: tuple[FieldCandidate, ...],
            *,
            query_view: ResolvedQueryView,
            atoms: tuple[QueryAtom, ...],
            timeout_seconds: float | None = None,
        ) -> FieldResolutionOutcome:
            del query_view, atoms, timeout_seconds
            self.field_calls += 1
            raise AssertionError("不完整 schema 不应进入确定性字段解释")

    planner = CountingPlanner()
    field_trace: dict[str, object] = {}
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.persistence.control.put_project(scope.project_id, "V14.1")
        runtime.persistence.control.put_knowledge_base(
            scope.knowledge_base_id,
            scope.project_id,
            "V14.1 KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            documents=(_table_document(scope, title),),
            idempotency_key="v14-1-incomplete-schema",
            budgets=runtime.persistence.default_budgets(),
        )
        source_type = type(runtime.retrieval._source)  # type: ignore[attr-defined]
        original_loader = source_type.load_document_structure

        def incomplete_loader(
            source: object,
            snapshot: ActiveRevisionQuerySnapshot,
            *,
            allowed_documents: tuple[SourceDocumentIdentity, ...],
            cursor: int,
            limit: int,
        ) -> DocumentStructurePage:
            page = original_loader(
                source,
                snapshot,
                allowed_documents=allowed_documents,
                cursor=cursor,
                limit=limit,
            )
            return DocumentStructurePage(page.items, 1, False)

        monkeypatch.setattr(
            source_type, "load_document_structure", incomplete_loader
        )
        runtime.retrieval._adaptive_planner = planner  # type: ignore[assignment]
        result = runtime.retrieval.search_and_answer(
            SearchRequest(
                scope=scope,
                text=f"《{title}》中，需求快验准备哪些材料？",
            )
        )
        field_trace = next(
            dict(event.attributes)
            for event in runtime.retrieval._trace.events(result.trace_id)  # type: ignore[attr-defined]
            if event.event_name == "retrieval.field_resolution"
        )

    assert planner.plan_calls == 0
    assert planner.field_calls == 0
    assert field_trace["reason_code"] == "FIELD_SCHEMA_SCAN_INCOMPLETE"
    assert field_trace["schema_complete_atom_ids"] == []
    assert field_trace["resolutions"] == []
    assert field_trace["discarded_incomplete_schema_candidate_count"] >= 1


def test_ambiguous_schema_fields_do_not_fall_through_to_generation(
    tmp_path: Path,
) -> None:
    """字段仍歧义时不得让生成模型从候选中擅自挑一个。"""
    title = "开发中心三种工作模式"
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "v14-1-ambiguous-field"),
        knowledge_base_id=deterministic_id("kb", "v14-1-ambiguous-field"),
    )

    class AmbiguousPlanner:
        def __init__(self) -> None:
            self.field_calls = 0

        def plan_adaptive(self, *_args: object) -> AdaptivePlanOutcome:
            raise AssertionError("显式来源路径不应执行前置解释")

        def resolve_fields(
            self,
            request: SearchRequest,
            candidates: tuple[FieldCandidate, ...],
            *,
            query_view: ResolvedQueryView,
            atoms: tuple[QueryAtom, ...],
            timeout_seconds: float | None = None,
        ) -> FieldResolutionOutcome:
            del atoms, timeout_seconds
            self.field_calls += 1
            start = request.text.index("准备哪些材料")
            return FieldResolutionOutcome(
                resolutions=(
                    FieldResolution(
                        atom_id="A1",
                        status=FieldResolutionStatus.AMBIGUOUS,
                        candidate_ids=tuple(
                            item.candidate_id for item in candidates
                        ),
                        query_span_start=start,
                        query_span_end=start + len("准备哪些材料"),
                        query_fragment="准备哪些材料",
                        query_view_digest=canonical_sha256(
                            query_view.model_dump(mode="json")
                        ),
                        reason_code="SCHEMA_FIELDS_REMAIN_AMBIGUOUS",
                    ),
                ),
                reason_code="FIELD_RESOLUTION_ACCEPTED",
                attempted=True,
                execution_state=FieldResolutionExecutionState.SUCCEEDED,
            )

    planner = AmbiguousPlanner()
    generator = GroundedFixtureGenerator()
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.retrieval = runtime.retrieval.with_generation(
            generator,
            serving_identity="unit-v14-1-ambiguous-field",
        )
        runtime.persistence.control.put_project(scope.project_id, "V14.1")
        runtime.persistence.control.put_knowledge_base(
            scope.knowledge_base_id,
            scope.project_id,
            "V14.1 KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            documents=(_table_document(scope, title),),
            idempotency_key="v14-1-ambiguous-field",
            budgets=runtime.persistence.default_budgets(),
        )
        runtime.retrieval._adaptive_planner = planner  # type: ignore[assignment]
        result = runtime.retrieval.search_and_answer(
            SearchRequest(
                scope=scope,
                text=(f"《{title}》中，需求快验开始前必须准备哪些材料？"),
            )
        )

    assert planner.field_calls == 1
    assert generator.calls == 0
    assert result.status is ConfidenceStatus.INSUFFICIENT_EVIDENCE
    assert result.answer is None
    assert result.generation_reason_code == "FIELD_RESOLUTION_AMBIGUOUS"
    assert result.display_message is not None
    assert "无法确定唯一字段" in result.display_message


def test_field_provider_failure_is_not_rewritten_as_semantic_ambiguity(
    tmp_path: Path,
) -> None:
    """没有合法模型结果时保留系统失败，不能消费初始 AMBIGUOUS。"""
    title = "开发中心三种工作模式"
    scope = KnowledgeBaseScope(
        project_id=deterministic_id("prj", "v14-1-field-provider-failure"),
        knowledge_base_id=deterministic_id(
            "kb", "v14-1-field-provider-failure"
        ),
    )

    class RejectedPlanner:
        def __init__(self) -> None:
            self.field_calls = 0

        def plan_adaptive(self, *_args: object) -> AdaptivePlanOutcome:
            raise AssertionError("显式来源路径不应执行前置解释")

        def resolve_fields(
            self,
            _request: SearchRequest,
            _candidates: tuple[FieldCandidate, ...],
            *,
            query_view: ResolvedQueryView,
            atoms: tuple[QueryAtom, ...],
            timeout_seconds: float | None = None,
        ) -> FieldResolutionOutcome:
            del query_view, atoms, timeout_seconds
            self.field_calls += 1
            return FieldResolutionOutcome(
                reason_code="FIELD_RESOLUTION_PROVIDER_UNAVAILABLE",
                attempted=True,
                execution_state=(
                    FieldResolutionExecutionState.REQUEST_REJECTED
                ),
                failure_category="PROVIDER_REQUEST_REJECTED",
                schema_revision="wb08r-field-resolution-v2",
                schema_sha256=canonical_sha256("schema"),
                contract_sha256=canonical_sha256("contract"),
                capability_profile_sha256=canonical_sha256("profile"),
            )

    planner = RejectedPlanner()
    generator = GroundedFixtureGenerator()
    field_trace: dict[str, object] = {}
    with build_p07_runtime(_PROFILE, data_dir=tmp_path) as runtime:
        runtime.retrieval = runtime.retrieval.with_generation(
            generator,
            serving_identity="unit-v14-1-field-provider-failure",
        )
        runtime.persistence.control.put_project(scope.project_id, "V14.1")
        runtime.persistence.control.put_knowledge_base(
            scope.knowledge_base_id,
            scope.project_id,
            "V14.1 KB",
            profile_id="dev-p06-memory",
        )
        runtime.persistence.builder.build_and_activate(
            project_id=scope.project_id,
            knowledge_base_id=scope.knowledge_base_id,
            documents=(_table_document(scope, title),),
            idempotency_key="v14-1-field-provider-failure",
            budgets=runtime.persistence.default_budgets(),
        )
        runtime.retrieval._adaptive_planner = planner  # type: ignore[assignment]
        search_request = SearchRequest(
            scope=scope,
            text=(f"《{title}》中，需求快验开始前必须准备哪些材料？"),
        )
        result = runtime.retrieval.search_and_answer(search_request)
        repeated = runtime.retrieval.search_and_answer(search_request)
        field_trace = dict(
            next(
                event
                for event in runtime.retrieval._trace.events(result.trace_id)  # type: ignore[attr-defined]
                if event.event_name == "retrieval.field_resolution"
            ).attributes
        )

    assert planner.field_calls == 2
    assert generator.calls == 0
    assert result.status is ConfidenceStatus.PROVIDER_UNAVAILABLE
    assert result.answer is None
    assert repeated.result_origin == "fresh"
    assert result.generation_reason_code == (
        "FIELD_RESOLUTION_PROVIDER_UNAVAILABLE"
    )
    assert field_trace["execution_state"] == "REQUEST_REJECTED"
    assert field_trace["semantic_resolutions_consumed"] is False
    assert field_trace["pending_atom_ids"] == ["A1"]
    assert field_trace["resolutions"][0]["resolution_origin"] == (
        "PENDING_INITIAL"
    )
